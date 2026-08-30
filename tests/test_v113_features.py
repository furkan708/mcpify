"""v1.13.0: the token-budget release — `list --cost`, `--fields` projection,
SSE POST responses (Streamable HTTP), and the OAuth2 write-credential split."""

import http.client
import http.server
import json
import threading

import pytest

from mcpify.api_server import ApiServer
from mcpify.cli import _resolve_write_auth
from mcpify.cli import main as cli_main
from mcpify.config import _API_KEYS, KNOWN_KEYS, _load_toml, validate
from mcpify.http_client import parse_fields, project_json
from mcpify.http_transport import build_http_server
from mcpify.spec import load_spec
from mcpify.tools import surface_cost_tokens, tool_cost_tokens

# ---------------------------------------------------------------------------
# list --cost
# ---------------------------------------------------------------------------

SPEC_DOC = {
    "openapi": "3.0.0",
    "info": {"title": "T", "version": "1"},
    "servers": [{"url": "http://api.example.com"}],
    "paths": {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets",
                          "parameters": [], "responses": {"200": {"description": "ok"}}}},
    },
}


@pytest.fixture()
def spec_file(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(SPEC_DOC))
    return str(path)


def test_tool_cost_positive_and_additive():
    tool = {"name": "list_pets", "description": "List pets.",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}}
    cost = tool_cost_tokens(tool)
    assert cost > 0
    assert surface_cost_tokens([tool, tool]) == cost * 2


def test_list_cost_json_includes_per_tool_tokens(spec_file, capsys):
    cli_main(["list", spec_file, "--json", "--cost"])
    rows = json.loads(capsys.readouterr().out)
    assert rows and rows[0]["cost_tokens"] > 0


def test_list_cost_human_prints_surface_summary(spec_file, capsys):
    cli_main(["list", spec_file, "--cost"])
    out = capsys.readouterr().out
    assert "surface cost:" in out
    assert "tokens" in out and "--lazy" in out  # the cut-it hint


def test_list_without_cost_prints_no_cost_line(spec_file, capsys):
    cli_main(["list", spec_file])
    assert "surface cost:" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --fields projection
# ---------------------------------------------------------------------------

def test_parse_fields_tolerates_spaces_and_empties():
    assert parse_fields("id, name ,,") == frozenset({"id", "name"})
    assert parse_fields(None) == frozenset()


def test_project_json_is_top_level_only():
    data = {"id": 1, "name": "Rex", "secret": "x", "meta": {"id": 9, "keep": "me"}}
    assert project_json(data, frozenset({"id", "name"})) == {"id": 1, "name": "Rex"}
    items = project_json([{"id": 1, "drop": 2}, {"id": 3, "drop": 4}], frozenset({"id"}))
    assert items == [{"id": 1}, {"id": 3}]
    assert project_json("plain", frozenset({"id"})) == "plain"


class Upstream(http.server.BaseHTTPRequestHandler):
    log_message = lambda self, *a: None  # noqa: E731

    def do_GET(self):
        body = json.dumps([
            {"id": 7, "name": "Pet7", "status": "available", "tag": {"id": 2, "name": "dogs"}},
        ]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
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


def rpc(server, method, params=None, request_id=1):
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server.handle_message(
        {"jsonrpc": "2.0", "id": request_id, "method": "method-ignored" and method,
         "params": params})


def test_fields_projection_over_live_call(upstream):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream, fields=frozenset({"id", "name"}))
    r = rpc(server, "tools/call", {"name": "list_pets", "arguments": {}}, 2)
    parsed = json.loads(r["result"]["content"][0]["text"])
    assert parsed == [{"id": 7, "name": "Pet7"}]  # nested `tag` dropped at TOP level only


def test_fields_none_keeps_full_body(upstream):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream)
    r = rpc(server, "tools/call", {"name": "list_pets", "arguments": {}}, 2)
    parsed = json.loads(r["result"]["content"][0]["text"])
    assert parsed[0]["status"] == "available" and parsed[0]["tag"]["name"] == "dogs"


def test_config_accepts_fields_key(tmp_path):
    good = tmp_path / "good.toml"
    good.write_text('[apis.pet]\nspec = "s.json"\nbase-url = "http://x"\nfields = "id,name"\n')
    assert validate(_load_toml(good)) == []
    typo = tmp_path / "typo.toml"
    typo.write_text('[apis.pet]\nspec = "s.json"\nfield = "id"\n')
    assert any("unknown key" in p for p in validate(_load_toml(typo)))


# ---------------------------------------------------------------------------
# SSE POST responses (Streamable HTTP)
# ---------------------------------------------------------------------------

@pytest.fixture()
def mcp_http(upstream):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream)
    server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    httpd = build_http_server(server, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def post(base, payload, accept="application/json"):
    conn = http.client.HTTPConnection(base, timeout=10)
    conn.request("POST", "/", body=json.dumps(payload).encode(),
                 headers={"Content-Type": "application/json", "Accept": accept})
    response = conn.getresponse()
    raw = response.read().decode()
    conn.close()
    return response.status, response.getheader("Content-Type"), raw


def test_sse_response_for_accepting_clients(mcp_http):
    status, ctype, raw = post(
        mcp_http, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        accept="application/json, text/event-stream")
    assert status == 200 and ctype.startswith("text/event-stream")
    assert raw.startswith("event: message\ndata: ")
    assert raw.endswith("\n\n")
    data = next(line for line in raw.split("\n") if line.startswith("data: "))[6:]
    message = json.loads(data)
    assert message["result"]["tools"]


def test_json_response_for_json_only_clients(mcp_http):
    status, ctype, raw = post(mcp_http, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200 and ctype == "application/json"
    assert json.loads(raw)["result"]["tools"]


def test_sse_call_carries_full_result(mcp_http):
    _status, ctype, raw = post(
        mcp_http,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "list_pets", "arguments": {}}},
        accept="text/event-stream")
    assert ctype.startswith("text/event-stream")
    data = next(line for line in raw.split("\n") if line.startswith("data: "))[6:]
    message = json.loads(data)
    assert "result" in message


# ---------------------------------------------------------------------------
# OAuth2 write split (--write-oauth2-*)
# ---------------------------------------------------------------------------

def static_args(**kw):

    defaults = {
        "write_auth_env": None, "write_auth_style": None, "write_auth_name": None,
        "write_oauth2_token_url": None, "write_oauth2_client_id_env": None,
        "write_oauth2_client_secret_env": None, "write_oauth2_scope": None,
        "write_oauth2_client_auth": "basic", "timeout": 30.0,
    }
    defaults.update(kw)
    import argparse as ap

    return ap.Namespace(**defaults)


def test_oauth2_write_flow_builds_client_credentials():
    from mcpify.http_client import OAuth2ClientCredentials

    write = _resolve_write_auth(
        static_args(write_oauth2_token_url="https://auth.example.com/token",
                    write_oauth2_client_id_env="W_CLIENT"),
        None,
    )
    assert isinstance(write, OAuth2ClientCredentials)


def test_static_and_oauth2_write_kinds_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        _resolve_write_auth(
            static_args(write_auth_env="W_KEY",
                        write_oauth2_token_url="https://auth.example.com/token"), None)


def test_oauth2_write_requires_client_id_env():
    with pytest.raises(SystemExit):
        _resolve_write_auth(static_args(write_oauth2_token_url="https://auth.example.com/token"), None)


def test_oauth2_main_with_static_write_now_allowed():
    from mcpify.http_client import OAuth2ClientCredentials

    main = OAuth2ClientCredentials("https://auth.example.com/token", "CLIENT_ID_ENV")
    write = _resolve_write_auth(static_args(write_auth_env="W_KEY"), main)
    assert write is not None and write.env_var == "W_KEY"


def test_config_accepts_write_oauth2_keys(tmp_path):
    good = tmp_path / "good.toml"
    good.write_text('[apis.pet]\nspec = "s.json"\nbase-url = "http://x"\n'
                    'write-oauth2-token-url = "https://auth.example.com/token"\n'
                    'write-oauth2-client-id-env = "W_CLIENT"\n')
    assert validate(_load_toml(good)) == []
    typo = tmp_path / "typo.toml"
    typo.write_text('[apis.pet]\nspec = "s.json"\nwrite-oauth = "x"\n')
    assert any("unknown key" in p for p in validate(_load_toml(typo)))


def test_config_schema_covers_v113_keys(capsys):
    cli_main(["config-schema"])
    schema = json.loads(capsys.readouterr().out)
    served = set(schema["properties"]["serve"]["properties"])
    apis = set(schema["properties"]["apis"]["additionalProperties"]["properties"])
    assert served == set(KNOWN_KEYS)
    assert apis == set(_API_KEYS)
    assert "fields" in served and "write-oauth2-token-url" in served
