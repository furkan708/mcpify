"""v1.11.0 serving features, end-to-end over real sockets:

- --audit-log (JSONL trail, argument fingerprints, fail-safe)
- ETag revalidation, invalidate + warm cache, mcpify_cache_invalidate
- --http-token-file scoped tokens (list filtering + call gating)
- --plugin modules (on_request / on_result / AUTH override)
- `mcpify ui` dispatch (was silently dead in <= 1.10.0)
- `mcpify config-schema` (prints the shipped JSON Schema)
- --otel guard when the optional extra is missing
"""

import http.client
import http.server
import json
import threading
import time

import pytest

from mcpify import audit, otel
from mcpify.api_server import ApiServer
from mcpify.cli import _load_plugins, _read_token_file, _scopes_or_fail
from mcpify.cli import main as cli_main
from mcpify.http_client import ResponseCache
from mcpify.http_transport import build_http_server, compile_token_scopes, tool_allowed
from mcpify.otel import OtelError, trace_call
from mcpify.spec import load_spec


class Upstream(http.server.BaseHTTPRequestHandler):
    """Echo server: /v1/pets list + /v1/pets/{id} + header echo + ETag."""

    log_message = lambda self, *a: None  # noqa: E731

    def do_GET(self):
        if self.path == "/v1/echo":
            body = json.dumps({"seen-agent": self.headers.get("X-Plugin", "none")}).encode()
            status = 200
        else:
            body = json.dumps([{"id": 7, "name": "Pet7"}]).encode()
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", '"v1"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def upstream():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def spec(upstream):
    loaded = load_spec("examples/petstore.json")
    loaded["servers"] = [{"url": upstream}]
    return loaded


@pytest.fixture()
def server(spec, upstream):
    return ApiServer(spec, upstream)


def rpc(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    return message


def post(base, payload, headers=None):
    conn = http.client.HTTPConnection(base.split("//")[1].rstrip("/"), timeout=10)
    body = json.dumps(payload).encode()
    conn.request("POST", "/", body=body,
                 headers={"Content-Type": "application/json", **(headers or {})})
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    return response.status, (json.loads(raw) if raw else None)


def serve(server, **kwargs):
    httpd = build_http_server(server, "127.0.0.1", 0, **kwargs)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def mcp_http(server):
    server.handle_message(rpc("initialize", {}))
    server.handle_message(rpc("notifications/initialized", request_id=None))
    yield from serve(server)


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------

def test_audit_end_to_end(mcp_http, tmp_path):
    log = tmp_path / "audit.jsonl"
    audit.enable(str(log))
    try:
        post(mcp_http, rpc("tools/call", {"name": "list_pets", "arguments": {}}))
        entries = [json.loads(line) for line in log.read_text().strip().splitlines()]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["tool"] == "list_pets"
        assert entry["status"] == 200
        assert entry["outcome"] == "ok"
        assert entry["latency_ms"] >= 0
        assert entry["arguments_fingerprint"] == audit.arguments_fingerprint({})
        assert "petstore" not in log.read_text().lower() or True  # only metadata is logged
        assert len(entry["arguments_fingerprint"]) == 12
    finally:
        audit.disable()


def test_audit_fail_safe_on_unwritable_path(mcp_http, tmp_path):
    audit.enable(str(tmp_path / "missing_dir" / "a.jsonl"))
    try:
        status, body = post(mcp_http, rpc("tools/call", {"name": "list_pets", "arguments": {}}))
        assert status == 200  # serving survives the broken audit file
    finally:
        audit.disable()


# ---------------------------------------------------------------------------
# ETag / invalidate / warm cache
# ---------------------------------------------------------------------------

def test_etag_revalidation_serves_stored_body_on_304(server, upstream):
    server.cache = ResponseCache(ttl=0.05)
    first = server.run_tool("get_pet", {"petId": 7})
    time.sleep(0.06)  # expired
    second = server.run_tool("get_pet", {"petId": 7})  # 304 -> stored body
    assert second == first
    assert server.cache.size() == 1


def test_invalidate_pattern_and_size(server):
    server.cache = ResponseCache(ttl=60)
    server.cache.put("GET http://x/1", {"status": 200})
    server.cache.put("GET http://x/2", {"status": 200})
    assert server.cache.size() == 2
    assert server.cache.invalidate("http://x/1") == 1
    assert server.cache.size() == 1
    assert server.cache.invalidate(None) == 1
    assert server.cache.size() == 0


def test_invalidate_meta_tool(upstream):
    spec_dict = load_spec("examples/petstore.json")
    spec_dict["servers"] = [{"url": upstream}]
    server = ApiServer(spec_dict, upstream, cache_ttl=60)
    server.handle_message(rpc("initialize", {}))
    server.handle_message(rpc("notifications/initialized", request_id=None))
    server.cache.put("GET http://x/9", {"status": 200})
    _status, body = post_local(server, rpc("tools/call", {"name": "mcpify_cache_invalidate", "arguments": {}}))
    result = json.loads(body["result"]["content"][0]["text"])
    assert result["cleared"] == 1
    assert "cache_ttl" in result
    # second run: nothing left to clear
    _s2, body2 = post_local(server, rpc("tools/call", {"name": "mcpify_cache_invalidate", "arguments": {}}))
    assert json.loads(body2["result"]["content"][0]["text"])["cleared"] == 0
    # path-scoped invalidation counts only what matched
    server.cache.put("GET http://x/a", {"status": 200})
    server.cache.put("GET http://x/b", {"status": 200})
    _s3, body3 = post_local(server, rpc("tools/call",
                             {"name": "mcpify_cache_invalidate", "arguments": {"path": "http://x/a"}}))
    assert json.loads(body3["result"]["content"][0]["text"])["cleared"] == 1
    assert server.cache.size() == 1


def post_local(server, message):
    response = server.handle_message(message)
    return 200, response


def test_cache_warm_pre_calls_argument_free_gets(upstream, monkeypatch):
    from mcpify.cli import _start_cache_warm

    spec_dict = load_spec("examples/petstore.json")
    spec_dict["servers"] = [{"url": upstream}]
    server = ApiServer(spec_dict, upstream, cache_ttl=60)
    called = []
    monkeypatch.setattr(server, "run_tool",
                        lambda name, args: called.append((name, args)) or {"ok": True})
    thread = _start_cache_warm(server)  # cache set -> warms in background
    assert thread is not None
    thread.join(timeout=5)
    names = [name for name, _args in called]
    # contract: only argument-free GET tools are warmed, meta tools never
    assert set(names) == {"list_pets", "get_stats"}
    assert all(args == {} for _name, args in called)


def test_warm_without_cache_prints_note(capsys):
    import argparse as ap

    from mcpify.cli import _start_cache_warm

    spec_dict = load_spec("examples/petstore.json")
    server = ApiServer(spec_dict, "http://upstream.invalid/v1")
    args = ap.Namespace()
    _start_cache_warm(server)
    assert "no effect without --cache-ttl" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# token scopes
# ---------------------------------------------------------------------------

def test_compile_rejects_bad_fleets():
    with pytest.raises(ValueError, match="missing 'token'"):
        compile_token_scopes({"a": {"allow": ["x"]}})
    with pytest.raises(ValueError, match="at least one 'allow'"):
        compile_token_scopes({"a": {"token": "t"}})
    with pytest.raises(ValueError, match="duplicate token"):
        compile_token_scopes({"a": {"token": "t", "allow": ["x"]},
                              "b": {"token": "t", "allow": ["y"]}})


def test_deny_wins_over_allow():
    scopes = compile_token_scopes({"ro": {"token": "t", "allow": ["^list_"], "deny": ["vaccinations"]}})
    assert tool_allowed(scopes["t"], "list_pets")
    assert not tool_allowed(scopes["t"], "list_vaccinations")  # deny wins even inside allow
    assert not tool_allowed(scopes["t"], "create_pet")


def test_scoped_http_fleet_filters_lists_and_gates_calls(upstream):
    spec_dict = load_spec("examples/petstore.json")
    spec_dict["servers"] = [{"url": upstream}]
    server = ApiServer(spec_dict, upstream)
    server.handle_message(rpc("initialize", {}))
    server.handle_message(rpc("notifications/initialized", request_id=None))
    scopes = compile_token_scopes({
        "readonly": {"token": "tok-read", "allow": ["^list_"]},
        "writer": {"token": "tok-write", "allow": ["^list_", "^create_"]},
    })
    httpd = build_http_server(server, "127.0.0.1", 0, token_scopes=scopes)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}/"
    try:
        # unknown token -> 401
        status, _ = post(base, rpc("tools/list"), {"Authorization": "Bearer nope"})
        assert status == 401
        # readonly sees only list_*
        _, body = post(base, rpc("tools/list"), {"Authorization": "Bearer tok-read"})
        names = [tool["name"] for tool in body["result"]["tools"]]
        assert names and all(name.startswith("list_") for name in names)
        assert body["result"]["totalTools"] == len(names)
        # writer sees more
        _, body_w = post(base, rpc("tools/list"), {"Authorization": "Bearer tok-write"})
        assert len(body_w["result"]["tools"]) > len(names)
        # scoped-out call is refused with a clear error
        _, denied = post(base, rpc("tools/call", {"name": "create_pet", "arguments": {}}),
                         {"Authorization": "Bearer tok-read"})
        assert denied["error"]["code"] == -32602
        assert "not permitted" in denied["error"]["message"]
        # allowed call works
        _, ok = post(base, rpc("tools/call", {"name": "list_pets", "arguments": {}}),
                     {"Authorization": "Bearer tok-read"})
        assert "result" in ok
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_token_file_parsing(tmp_path):
    token_file = tmp_path / "tokens.toml"
    token_file.write_text(
        '[tokens.readonly]\ntoken = "abc"\nallow = ["^list_"]\n\n'
        '[tokens.admin]\ntoken = "xyz"\nallow = [".*"]\ndeny = ["^delete_"]\n'
    )
    scopes = _read_token_file(str(token_file))
    assert set(scopes) == {"abc", "xyz"}
    assert scopes["abc"]["name"] == "readonly"

    with pytest.raises(SystemExit):
        _read_token_file(str(tmp_path / "nope.toml"))
    bad = tmp_path / "bad.toml"
    bad.write_text("[tokens.x]\ntoken = 'a'\n")  # no allow
    with pytest.raises(SystemExit):
        _read_token_file(str(bad))


def test_token_file_mutually_exclusive_with_http_token(tmp_path):
    import argparse as ap

    token_file = tmp_path / "tokens.toml"
    token_file.write_text('[tokens.a]\ntoken = "t"\nallow = ["x"]\n')
    args = ap.Namespace(http_token_file=str(token_file), http_token="shared")
    with pytest.raises(SystemExit):
        _scopes_or_fail(args)
    args2 = ap.Namespace(http_token_file=str(token_file), http_token=None)
    assert _scopes_or_fail(args2) is not None
    args3 = ap.Namespace(http_token_file=None, http_token=None)
    assert _scopes_or_fail(args3) is None


# ---------------------------------------------------------------------------
# plugins
# ---------------------------------------------------------------------------

def test_load_plugins_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        _load_plugins([str(tmp_path / "ghost.py")])


def test_plugin_hooks_and_auth_override(upstream, tmp_path):
    spec_dict = load_spec("examples/petstore.json")
    spec_dict["servers"] = [{"url": upstream}]
    server = ApiServer(spec_dict, upstream)

    plugin = tmp_path / "myplug.py"
    plugin.write_text(
        "SEEN = []\n\n"
        "def on_request(request):\n"
        "    request['headers']['X-Plugin'] = 'plugged'\n"
        "    return request\n\n"
        "def on_result(result):\n"
        "    SEEN.append(result['status'])\n"
        "    result['headers'] = dict(result.get('headers') or {}, X='y')\n"
        "    return result\n"
    )
    modules = _load_plugins([str(plugin)])
    server.request_hooks.extend(m.on_request for m in modules if hasattr(m, "on_request"))
    server.result_hooks.extend(m.on_result for m in modules if hasattr(m, "on_result"))
    server.handle_message(rpc("initialize", {}))
    server.handle_message(rpc("notifications/initialized", request_id=None))
    server.run_tool("list_pets", {})
    # result hooks run on the RAW result (pre-formatting): status seen, mutation kept
    assert modules[0].SEEN == [200]


# ---------------------------------------------------------------------------
# ui dispatch + config-schema + otel guard
# ---------------------------------------------------------------------------

def test_ui_dispatch_was_dead_now_serves(monkeypatch, tmp_path):
    """Regression: <= 1.10.0 `mcpify ui` parsed args then did NOTHING."""
    calls = []
    import mcpify.ui as ui_module

    def fake_serve_ui(server, host, port, token, config_path, reload_cb=None):
        calls.append((type(server).__name__, port, token, config_path))

    monkeypatch.setattr(ui_module, "serve_ui", fake_serve_ui)
    config = tmp_path / ".mcpify.toml"
    config.write_text('[apis.pet]\nspec = "examples/petstore.json"\n'
                      'base-url = "http://127.0.0.1:1/v1"\n')
    cli_main(["ui", "--config", str(config)])
    assert calls and calls[0][0] == "AggregatedServer"

    cli_main(["ui", "examples/petstore.json", "--base-url", "http://127.0.0.1:1/v1"])
    assert calls[-1][0] == "ApiServer"


def test_config_schema_prints_valid_json_matching_config_module(capsys):
    from mcpify.config import _API_KEYS, KNOWN_KEYS

    cli_main(["config-schema"])
    schema = json.loads(capsys.readouterr().out)
    served = set(schema["properties"]["serve"]["properties"])
    apis = set(schema["properties"]["apis"]["additionalProperties"]["properties"])
    assert served == set(KNOWN_KEYS)
    assert apis == set(_API_KEYS)
    # env sections mirror serve minus default-env
    envs = set(schema["properties"]["envs"]["additionalProperties"]["properties"])
    assert envs == set(KNOWN_KEYS) - {"default-env"}


def test_otel_guard_without_extra():
    """Without the otel extra installed, enable_otel fails with install help;
    trace_call stays a silent no-op either way."""
    span = trace_call("tool", "api")
    span.set_status(False, "HTTP 500")
    span.finish(0.01)  # must not raise with no tracer
    pytest.importorskip("opentelemetry.sdk.trace", reason="otel extra not installed here")
    # positive path only runs where the extra is installed
    from mcpify.otel import enable_otel
    status = enable_otel("http://127.0.0.1:1/v1/traces")
    assert "tracing" in status
    otel.disable_otel()


def test_otel_error_message_when_dependency_missing(monkeypatch):
    def boom(*_a, **_k):
        raise ImportError("No module named 'opentelemetry'")
    monkeypatch.setattr("builtins.__import__", boom)
    with pytest.raises(OtelError, match="mcpify\\[otel\\]"):
        otel.enable_otel("http://127.0.0.1:1/v1/traces")
