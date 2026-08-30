"""v1.14.0: `--redact` response masking, `--rate-limit` courtesy throttle,
`doctor --probe` live pre-flight, and token costs in lazy search results."""

import http.server
import json
import threading
from pathlib import Path

import pytest

from mcpify.aggregate import AggregatedServer
from mcpify.api_server import ApiServer
from mcpify.cli import _doctor_probe, _pick_probe_operation
from mcpify.cli import main as cli_main
from mcpify.config import _API_KEYS, KNOWN_KEYS, _load_toml, apply_to_namespace, validate
from mcpify.http_client import RateLimiter, execute, redact_json
from mcpify.spec import load_spec
from mcpify.tools import spec_to_tools, tool_cost_tokens

# ---------------------------------------------------------------------------
# shared fixtures: an upstream whose payloads carry secrets
# ---------------------------------------------------------------------------

SPEC_DOC = {
    "openapi": "3.0.0",
    "info": {"title": "T", "version": "1"},
    "servers": [{"url": "http://api.example.com"}],
    "paths": {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets",
                          "parameters": [], "responses": {"200": {"description": "ok"}}}},
        "/session": {"get": {"operationId": "getSession", "summary": "Get session",
                             "parameters": [], "responses": {"200": {"description": "ok"}}}},
        "/missing": {"get": {"operationId": "getMissing", "summary": "Always 404",
                             "parameters": [], "responses": {"404": {"description": "gone"}}}},
        "/locked": {"get": {"operationId": "getLocked", "summary": "Has a required param",
                            "parameters": [{"name": "key", "in": "query", "required": True,
                                            "schema": {"type": "string"}}],
                            "responses": {"200": {"description": "ok"}}}},
    },
}


class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the test run
        pass

    def do_GET(self):
        if self.path.endswith("/missing"):
            body = {"message": "nope", "api_key": "sk-live-123"}
            status = 404
        elif self.path.endswith("/session"):
            body = {"user": {"Password": "h4x", "name": "admin"},
                    "client_secret": "cs-9", "ok": True}
            status = 200
        else:
            body = [{"id": 7, "name": "Pet7", "status": "ok",
                     "tag": {"id": 2, "name": "dogs", "kind": "species"}}]
            status = 200
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def upstream():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def spec_file(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(SPEC_DOC))
    return str(path)


def rpc(server, method, params=None, request_id=2):
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server.handle_message(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


# ---------------------------------------------------------------------------
# --redact
# ---------------------------------------------------------------------------

SECRETS = frozenset({"password", "token", "client_secret"})


def test_redact_masks_at_every_level():
    data = {"user": {"Password": "h4x", "name": "a", "tags": [{"token": "t1"}, "plain"]},
            "PASSWORD": None, "ok": 1}
    out = redact_json(data, SECRETS)
    assert out == {"user": {"Password": "***", "name": "a",
                            "tags": [{"token": "***"}, "plain"]},
                   "PASSWORD": None, "ok": 1}


def test_redact_is_noop_without_secrets():
    data = {"password": "x"}
    assert redact_json(data, frozenset()) is data


def test_redact_keeps_array_lengths():
    out = redact_json([{"password": "a"}, {"password": "b"}, {"password": "c"}], SECRETS)
    assert out == [{"password": "***"}] * 3  # masked in place, never filtered


def test_redact_over_live_call(upstream, spec_file):
    spec = load_spec(spec_file)
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream, redact=frozenset({"id", "status"}))
    r = rpc(server, "tools/call", {"name": "listpets", "arguments": {}}, 2)
    parsed = json.loads(r["result"]["content"][0]["text"])
    assert parsed[0]["id"] == "***" and parsed[0]["status"] == "***"
    assert parsed[0]["tag"]["id"] == "***"          # masked at every level
    assert parsed[0]["tag"]["name"] == "dogs"       # everything else untouched


def test_redact_masks_error_bodies_too(upstream, spec_file):
    spec = load_spec(spec_file)
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream, redact=frozenset({"api_key"}))
    r = rpc(server, "tools/call", {"name": "getmissing", "arguments": {}}, 2)
    assert r["result"]["isError"] is True
    text = r["result"]["content"][0]["text"]
    assert "sk-live-123" not in text          # the secret never reaches the model
    assert '"***"' in text and "nope" in text  # masked in place, message intact


def test_fields_then_redact_order(upstream, spec_file):
    spec = load_spec(spec_file)
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream,
                       fields=frozenset({"id", "tag", "user"}),
                       redact=frozenset({"id", "password"}))
    r = rpc(server, "tools/call", {"name": "listpets", "arguments": {}}, 2)
    parsed = json.loads(r["result"]["content"][0]["text"])
    # projection: `tag` is SELECTED, so its value survives verbatim; the
    # unselected scalars (name, status) drop. Redaction runs last: a field
    # you asked for by name is still masked when it is a secret — at every
    # level, including inside a verbatim-selected object
    assert parsed[0] == {"id": "***", "tag": {"id": "***", "name": "dogs", "kind": "species"}}


def test_server_context_carries_redact_and_limiter(upstream, spec_file):
    spec = load_spec(spec_file)
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream, redact=frozenset({"id"}), rate_limit=500.0)
    context = server._context_for(server.tools[0])
    assert context["redact"] == frozenset({"id"})
    assert context["rate_limiter"] is not None and context["rate_limiter"].rps == 500.0
    plain = ApiServer(spec, upstream)
    plain_context = plain._context_for(plain.tools[0])
    assert plain_context["redact"] is None and plain_context["rate_limiter"] is None


# ---------------------------------------------------------------------------
# --rate-limit
# ---------------------------------------------------------------------------

def test_rate_limiter_opens_slots_at_rps():
    now = [100.0]
    slept = []

    def clock():
        return now[0]

    def sleeper(delay):
        slept.append(delay)
        now[0] += delay

    rl = RateLimiter(2.0, clock=clock, sleeper=sleeper)
    assert rl.wait() == 0.0 and slept == []            # first call: free
    assert rl.wait() == 0.5                            # 2 rps -> 0.5s slot
    assert rl.wait() == 0.5
    assert slept == [0.5, 0.5]


def test_rate_limiter_rejects_nonpositive():
    with pytest.raises(ValueError):
        RateLimiter(0)
    with pytest.raises(ValueError):
        RateLimiter(-1.5)


def test_execute_throttles_every_attempt():
    class Counter:
        def __init__(self):
            self.calls = 0

        def wait(self):
            self.calls += 1

    limiter = Counter()
    request = {"method": "GET", "url": "http://127.0.0.1:1/no", "headers": {}, "body": None}
    execute(request, timeout=1, rate_limit=limiter)  # unreachable target -> status 0
    assert limiter.calls == 1
    execute(request, timeout=1, rate_limit=limiter, retry=2)
    assert limiter.calls == 4  # first attempt + 2 retries (+1 from the call above -> 3, plus this first = 4)


def test_rate_limit_live_calls(upstream, spec_file):
    spec = load_spec(spec_file)
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream, rate_limit=1000.0)
    for request_id in (2, 3, 4):
        r = rpc(server, "tools/call", {"name": "listpets", "arguments": {}}, request_id)
        assert "Pet7" in r["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# config keys
# ---------------------------------------------------------------------------

def test_config_accepts_redact_and_rate_limit(tmp_path):
    config = tmp_path / ".mcpify.toml"
    config.write_text(
        '[serve]\nredact = "password,token"\nrate-limit = 2.5\n'
        '[apis.a]\nspec = "s.json"\nredact = "api_key"\nrate-limit = 5\n'
        "[envs.prod]\nredact = \"password\"\nrate-limit = 1.5\n")
    data = _load_toml(config)
    problems = validate(data)
    assert problems == []


def test_config_maps_rate_limit_to_namespace(tmp_path):
    config = tmp_path / ".mcpify.toml"
    config.write_text('[serve]\nrate-limit = 2.5\nredact = "password"\n')

    class NS:
        rate_limit = None
        redact = None

    ns = NS()
    applied = apply_to_namespace(_load_toml(config)["serve"], ns)
    assert ns.rate_limit == 2.5 and ns.redact == "password"
    assert "rate-limit" in applied and "redact" in applied


def test_config_schema_parity():
    import json as _json

    schema = _json.loads(Path("mcpify/config-schema.json").read_text(encoding="utf-8"))
    assert set(schema["properties"]["serve"]["properties"]) == set(KNOWN_KEYS)
    assert set(schema["properties"]["apis"]["additionalProperties"]["properties"]) == set(_API_KEYS)
    assert set(schema["properties"]["envs"]["additionalProperties"]["properties"]) == \
        set(KNOWN_KEYS - {"default-env"})


# ---------------------------------------------------------------------------
# lazy search: the price of the pull is visible before pulling
# ---------------------------------------------------------------------------

def test_search_entries_carry_cost_tokens(upstream, spec_file):
    spec = load_spec(spec_file)
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream, lazy=True)
    r = rpc(server, "tools/call",
            {"name": "mcpify_search_tools", "arguments": {"query": "pets"}}, 2)
    lines = r["result"]["content"][0]["text"].split("\n", 1)
    entries = json.loads(lines[1])
    assert entries and all("cost_tokens" in entry for entry in entries)
    for entry in entries:
        assert entry["cost_tokens"] == tool_cost_tokens(server.by_name[entry["name"]])


def test_search_header_mentions_pull_cost(upstream, spec_file):
    spec = load_spec(spec_file)
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream, lazy=True)
    r = rpc(server, "tools/call",
            {"name": "mcpify_search_tools", "arguments": {"query": ""}}, 2)
    header = r["result"]["content"][0]["text"].split("\n", 1)[0]
    assert "tokens" in header and "get_tool_schema" in header
    assert "~" in header


# ---------------------------------------------------------------------------
# doctor --probe
# ---------------------------------------------------------------------------

def test_pick_probe_operation_prefers_argument_free_gets():
    picked = _pick_probe_operation(load_spec("examples/petstore.json"))
    assert picked is not None
    method, path = picked
    assert method == "GET" and "{" not in path


def test_pick_probe_operation_skips_required_params(spec_file):
    picked = _pick_probe_operation(load_spec(spec_file))
    assert picked == ("GET", "/pets")  # /locked has a required param


def test_probe_reachable(upstream, spec_file, capsys):
    spec = load_spec(spec_file)
    report = _doctor_probe(spec, upstream, 5.0)
    assert report["ok"] is True and report["status"] == 200
    assert report["path"] == "/pets" and report["latency_seconds"] >= 0


def test_probe_falls_back_to_base_root(spec_file):
    spec = {"openapi": "3.0.0", "info": {"title": "T", "version": "1"},
            "paths": {"/locked": {"get": {"operationId": "getLocked",
                                          "parameters": [{"name": "k", "in": "query",
                                                          "required": True,
                                                          "schema": {"type": "string"}}],
                                          "responses": {"200": {"description": "ok"}}}}}}
    report = _doctor_probe(spec, "http://127.0.0.1:1", 1.0)
    assert report["path"] == "/" and report["ok"] is False  # closed port


def test_probe_doctor_cli_reachable(upstream, spec_file, capsys):
    cli_main(["doctor", spec_file, "--probe", "--base-url", upstream])
    out = capsys.readouterr().out
    assert "probe:" in out and "200" in out and "reachable" in out


def test_probe_doctor_cli_success_does_not_exit(upstream, spec_file):
    # a green pre-flight is not an error: no SystemExit, just the report
    assert cli_main(["doctor", spec_file, "--probe", "--base-url", upstream]) is None


def test_probe_doctor_cli_unreachable_exits_1(spec_file, capsys):
    with pytest.raises(SystemExit) as exc:
        cli_main(["doctor", spec_file, "--probe", "--base-url", "http://127.0.0.1:9"])
    assert exc.value.code == 1


def test_probe_doctor_json_payload(upstream, spec_file, capsys):
    cli_main(["doctor", spec_file, "--json", "--probe", "--base-url", upstream])
    payload = json.loads(capsys.readouterr().out)
    assert payload["probe"]["status"] == 200
    assert payload["probe"]["ok"] is True
    assert payload["probe"]["path"] == "/pets"


def test_probe_without_base_url_fails(spec_file):
    spec = load_spec(spec_file)
    spec["servers"] = []
    report = _doctor_probe(spec, None, 5.0)
    assert report["ok"] is False and "base URL" in report["error"]


# ---------------------------------------------------------------------------
# aggregation: per-API redact + one limiter per upstream
# ---------------------------------------------------------------------------

def entry(label, server, **overrides):
    base = f"http://127.0.0.1:{server.server_port}/v1"
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": base}]
    data = {
        "label": label, "spec": spec, "base": base, "auth": None,
        "timeout": 10.0, "cache": None, "retry": 0, "retry_delay": 1.0,
        "wait_on_429": 0.0, "tools": spec_to_tools(spec),
    }
    data.update(overrides)
    return data


def test_aggregate_context_carries_per_entry_redact_and_limiter(upstream):
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        first = entry("a", type("S", (), {"server_port": int(upstream.rsplit(":", 1)[1].split("/")[0])})())
        other = entry("b", type("S", (), {"server_port": httpd.server_port})(),
                      redact=frozenset({"id"}), **{"rate-limit": 50.0})
        agg = AggregatedServer([first, other])
        agg.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        agg.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tool_a = next(t for t in agg.tools if t["name"] == "a_list_pets")
        tool_b = next(t for t in agg.tools if t["name"] == "b_list_pets")
        context_a = agg._context_for(tool_a)
        context_b = agg._context_for(tool_b)
        assert context_a["redact"] is None and context_a["rate_limiter"] is None
        assert context_b["redact"] == frozenset({"id"})
        assert context_b["rate_limiter"] is not None
        assert context_b["rate_limiter"] is not context_a["rate_limiter"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# multi-API preview: `mcpify list` over a config ([apis.*])
# ---------------------------------------------------------------------------

def test_list_multi_api_human_with_cost(tmp_path, capsys):
    spec_path = Path("examples/petstore.json").resolve().as_posix()  # TOML-safe on Windows
    config = tmp_path / ".mcpify.toml"
    config.write_text(
        f'[apis.a]\nspec = "{spec_path}"\n\n[apis.b]\nspec = "{spec_path}"\nrate-limit = 5\n')
    cli_main(["list", "--config", str(config), "--cost"])
    err_out = capsys.readouterr()
    out = err_out.out
    assert "2 APIs" in out and "[a]" in out and "[b]" in out
    assert "surface cost:" in out


def test_list_multi_api_json_rows(tmp_path, capsys):
    spec_path = Path("examples/petstore.json").resolve().as_posix()
    config = tmp_path / ".mcpify.toml"
    config.write_text(f'[apis.a]\nspec = "{spec_path}"\n\n[apis.b]\nspec = "{spec_path}"\n')
    cli_main(["list", "--config", str(config), "--json", "--cost"])
    rows = json.loads(capsys.readouterr().out)
    labels = {row["api"] for row in rows}
    assert labels == {"a", "b"}
    assert all("cost_tokens" in row for row in rows)


def test_list_multi_api_without_cost_still_lists(tmp_path, capsys):
    spec_path = Path("examples/petstore.json").resolve().as_posix()
    config = tmp_path / ".mcpify.toml"
    config.write_text(f'[apis.a]\nspec = "{spec_path}"\n')
    cli_main(["list", "--config", str(config)])
    out = capsys.readouterr().out
    assert "[a]" in out and "tools" in out and "surface cost:" not in out
