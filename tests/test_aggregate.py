"""Multi-API aggregation: one server, several OpenAPI specs, one surface.

The composition layer paid gateways bill for. These tests pin the
merge/rename rules, per-API routing (including per-API auth), the
aggregated health report, lazy-mode search across APIs, and the
config-file CLI wiring.
"""

import http.server
import json
import threading

import pytest

from mcpify.aggregate import AggregatedServer, merge_entries
from mcpify.cli import main as cli_main
from mcpify.config import validate
from mcpify.http_client import ResponseCache
from mcpify.spec import load_spec
from mcpify.tools import AuthConfig, spec_to_tools

# ---------------------------------------------------------------------------
# two local upstream APIs with disjoint payloads
# ---------------------------------------------------------------------------

class Records(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/pets"):
            self.server.requests.append(("GET", self.path, self.headers.get("Authorization"),
                                         self.headers.get("X-Key")))
            self._reply(self.server.payload)
        else:
            self.server.requests.append(("GET", self.path, None, None))
            self._reply({"up": self.server.name}, status=self.server.root_status)


class Upstream(http.server.HTTPServer):
    def __init__(self, name, payload, root_status=200):
        super().__init__(("127.0.0.1", 0), Records)
        self.name = name
        self.payload = payload
        self.requests = []
        self.root_status = root_status


@pytest.fixture()
def api_a():
    server = Upstream("A", [{"id": 1, "from": "alpha"}])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture()
def api_b():
    server = Upstream("B", [{"id": 2, "from": "beta"}])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def pet_spec(base):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": base}]
    return spec


def entry(label, server, tools=None, auth=None, **overrides):
    spec = pet_spec(f"http://127.0.0.1:{server.server_port}/v1")
    data = {
        "label": label,
        "spec": spec,
        "base": f"http://127.0.0.1:{server.server_port}/v1",
        "auth": auth,
        "timeout": 10.0,
        "cache": None,
        "retry": 0,
        "retry_delay": 1.0,
        "wait_on_429": 0.0,
        "tools": tools if tools is not None else spec_to_tools(spec),
    }
    data.update(overrides)
    return data


def aggregate(a, b=None, **kwargs):
    entries = [a] if b is None else [a, b]
    agg = AggregatedServer(entries, **kwargs)
    agg.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    agg.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return agg


def call(server, name, arguments, request_id=9):
    return server.handle_message(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}}
    )


# ---------------------------------------------------------------------------
# merge / rename rules
# ---------------------------------------------------------------------------

def test_colliding_tools_prefixed_on_both_sides(api_a, api_b):
    merged, _ = merge_entries([entry("alpha", api_a), entry("beta", api_b)])
    names = [tool["name"] for tool in merged]
    assert "alpha_list_pets" in names
    assert "beta_list_pets" in names
    assert "list_pets" not in names  # neither API silently wins the bare name


def test_non_conflicting_names_stay_bare(api_a, api_b):
    # only petstore shared surface collides; give B a unique extra tool
    b = entry("beta", api_b)
    unique = dict(b["tools"][0])
    unique["name"] = "beta_only_thing"
    b["tools"] = [*b["tools"], unique]
    merged, _ = merge_entries([entry("alpha", api_a), b])
    names = [tool["name"] for tool in merged]
    assert "beta_only_thing" in names  # untouched


def test_leftover_collision_gets_suffix():
    def one(label, names):
        tools = [{"name": n, "description": f"[GET] {n}", "inputSchema":
                  {"type": "object", "properties": {}, "required": []},
                  "annotations": {}, "_meta": {"method": "GET", "path": "/" + n,
                  "parameters": [], "has_body": False, "deprecated": False,
                  "raw_body_content_type": None, "tags": []}} for n in names]
        return {"label": label, "tools": tools, "spec": {}, "base": "http://x",
                "auth": None, "timeout": 5, "cache": None, "retry": 0,
                "retry_delay": 1.0, "wait_on_429": 0.0}

    merged, _ = merge_entries([one("aa", ["x"]), one("bb", ["x", "aa_x"])])
    names = [tool["name"] for tool in merged]
    assert len(names) == len(set(names))  # unique by construction
    assert "aa_x_2" in names or "aa_x" in names


def test_tools_carry_api_label(api_a, api_b):
    merged, _ = merge_entries([entry("alpha", api_a), entry("beta", api_b)])
    labels = {tool["api"] for tool in merged}
    assert labels == {"alpha", "beta"}


def test_empty_entries_rejected():
    with pytest.raises(ValueError):
        AggregatedServer([])


# ---------------------------------------------------------------------------
# protocol surface + routing
# ---------------------------------------------------------------------------

@pytest.fixture()
def agg(api_a, api_b):
    return aggregate(entry("alpha", api_a), entry("beta", api_b))


def test_tools_list_is_union_with_labels(agg):
    response = agg.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = response["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert len(names) == len(set(names))
    assert "alpha_list_pets" in names and "beta_list_pets" in names
    labels = {tool.get("api") for tool in tools if tool["name"].endswith("list_pets")}
    assert labels == {"alpha", "beta"}
    assert names.count("mcpify_health") == 1  # meta tools stay global


def test_calls_route_to_the_owning_api(agg, api_a, api_b):
    response = call(agg, "alpha_list_pets", {"limit": 5})
    assert response["result"].get("isError") is not True
    assert api_a.requests and api_a.requests[-1][1].startswith("/v1/pets")
    assert not api_b.requests or "list_pets" not in str(api_b.requests[-1][1])


def test_unprefixed_call_unknown_in_aggregate(agg):
    response = call(agg, "list_pets", {})
    assert response["error"]["code"] == -32601


def test_per_api_auth_isolation(api_a, api_b, monkeypatch):
    monkeypatch.setenv("TOK_A", "tok-alpha")
    monkeypatch.setenv("KEY_B", "key-beta")
    a = entry("alpha", api_a, auth=AuthConfig("TOK_A", "bearer"))
    b = entry("beta", api_b, auth=AuthConfig("KEY_B", "header", name="X-Key"))
    agg = aggregate(a, b)
    call(agg, "alpha_list_pets", {})
    call(agg, "beta_list_pets", {})
    assert api_a.requests[-1][2] == "Bearer tok-alpha"
    assert api_b.requests[-1][3] == "key-beta"


def test_per_api_cache_isolation(api_a, api_b):
    a = entry("alpha", api_a, cache=ResponseCache(60))
    b = entry("beta", api_b)
    agg = aggregate(a, b)
    call(agg, "alpha_list_pets", {})
    call(agg, "alpha_list_pets", {})
    assert len(api_a.requests) == 1  # second call served from A's cache
    call(agg, "beta_list_pets", {})
    assert len(api_b.requests) == 1


def test_lazy_search_matches_api_label(api_a, api_b):
    """Un-prefixed tools are still findable by their API label."""
    agg = aggregate(entry("alpha", api_a), entry("beta", api_b), lazy=True)
    response = call(agg, "mcpify_search_tools", {"query": "beta"})
    text = response["result"]["content"][0]["text"]
    assert "beta" in text


def test_env_flag_applies_to_multi_api(tmp_path, serve_recorder, api_a, api_b, monkeypatch):
    monkeypatch.setenv("TOK_A", "tok-alpha")
    spec_a = tmp_path / "a.json"
    spec_a.write_text(json.dumps(pet_spec(f"http://127.0.0.1:{api_a.server_port}/v1")), encoding="utf-8")
    spec_b = tmp_path / "b.json"
    spec_b.write_text(json.dumps(pet_spec(f"http://127.0.0.1:{api_b.server_port}/v1")), encoding="utf-8")
    config = write_config(tmp_path, f"""
[envs.staging]
auth-env = 'TOK_A'

[apis.alpha]
spec = '{spec_a}'

[apis.beta]
spec = '{spec_b}'
""")
    cli_main(["serve", "--config", config, "--env", "staging", "--http", "8080"])
    server = serve_recorder[0]
    # [envs.NAME] sits on the [serve] layer, so every API inherits it;
    # a per-API section can still override it back off.
    for entry_dict in server.entries:
        assert entry_dict["auth"] is not None
        assert entry_dict["auth"].env_var == "TOK_A"


def test_lazy_aggregate_search_across_apis(api_a, api_b):
    agg = aggregate(entry("alpha", api_a), entry("beta", api_b), lazy=True)
    response = call(agg, "mcpify_search_tools", {"query": "pets"})
    assert response["result"].get("isError") is not True
    text = response["result"]["content"][0]["text"]
    assert "alpha" in text and "beta" in text
    listed = agg.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names == ["mcpify_search_tools", "mcpify_get_tool_schema", "mcpify_call_tool", "mcpify_health"]
    # and the lazy call path routes to the owner
    call(agg, "mcpify_call_tool", {"name": "beta_list_pets", "arguments": {}})
    assert api_b.requests[-1][1].startswith("/v1/pets")


def test_preview_routes_to_owner(api_a, api_b):
    agg = aggregate(entry("alpha", api_a), entry("beta", api_b), enable_preview=True)
    response = call(agg, "mcpify_preview_request", {"name": "beta_get_pet", "arguments": {"petId": 3}})
    text = response["result"]["content"][0]["text"]
    assert f"http://127.0.0.1:{api_b.server_port}/v1/pets/3" in text
    assert api_a.requests == [] and api_b.requests == []  # dry run


def test_aggregated_health_reports_each_api(api_a, api_b):
    agg = aggregate(entry("alpha", api_a), entry("beta", api_b))
    response = call(agg, "mcpify_health", {})
    report = json.loads(response["result"]["content"][0]["text"])
    assert report["all_reachable"] is True
    assert {item["api"] for item in report["apis"]} == {"alpha", "beta"}


def test_aggregated_health_names_dead_apis(api_a):
    gone = Upstream("G", [])
    threading.Thread(target=gone.serve_forever, daemon=True).start()
    gone.shutdown()  # socket no longer accepting -> probe gets connection refused
    gone.server_close()
    agg = aggregate(entry("alpha", api_a), entry("beta", gone))
    response = call(agg, "mcpify_health", {})
    report = json.loads(response["result"]["content"][0]["text"])
    assert report["all_reachable"] is False
    assert response["result"]["isError"] is True
    assert "beta" in report["hint"]


def test_direct_construction_still_works(api_a):
    """A single-entry aggregate behaves like a plain server (no renames)."""
    agg = aggregate(entry("solo", api_a))
    names = [tool["name"] for tool in agg.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert "list_pets" in names  # no collision -> no prefix


# ---------------------------------------------------------------------------
# config-file CLI wiring
# ---------------------------------------------------------------------------

@pytest.fixture()
def serve_recorder(monkeypatch):
    servers = []

    def fake_serve_http(server, host, port, token=None, max_body=None):
        servers.append(server)

    import mcpify.http_transport as transport

    monkeypatch.setattr(transport, "serve_http", fake_serve_http)
    return servers


def write_config(tmp_path, text):
    path = tmp_path / ".mcpify.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_config_validate_apis_errors():
    problems = validate({"apis": {"a": {"auth-env": "X"}, "b": "not-a-table",
                                  "c": {"spec": "s", "nonsense": 1}}})
    text = "\n".join(problems)
    assert "apis.a: missing required 'spec'" in text
    assert "apis.b: must be a table" in text
    assert "apis.c.nonsense: unknown key" in text
    assert validate({"apis": {"a": {"spec": "s", "auth-env": "X"}}}) == []


def test_cli_builds_aggregated_server(tmp_path, serve_recorder, api_a, api_b, monkeypatch):
    monkeypatch.setenv("TOK_A", "tok-alpha")
    spec_a = tmp_path / "a.json"
    spec_a.write_text(json.dumps(pet_spec(f"http://127.0.0.1:{api_a.server_port}/v1")), encoding="utf-8")
    spec_b = tmp_path / "b.json"
    spec_b.write_text(json.dumps(pet_spec(f"http://127.0.0.1:{api_b.server_port}/v1")), encoding="utf-8")
    write_config(tmp_path, f"""
[apis.alpha]
spec = '{spec_a}'
auth-env = 'TOK_A'

[apis.beta]
spec = '{spec_b}'
read-only = true
""")
    cli_main(["serve", "--config", write_config(tmp_path, (tmp_path / ".mcpify.toml").read_text(encoding="utf-8")),
              "--http", "8080"])
    server = serve_recorder[0]
    names = [tool["name"] for tool in server.public_tools()]
    assert "alpha_list_pets" in names and "beta_list_pets" in names
    assert "beta_delete_pet" not in names  # per-API read-only applied to beta only
    assert "delete_pet" in names  # alpha's delete no longer collides -> stays bare
    assert "create_pet" in names
    assert server.entries[0]["auth"].style == "bearer"
    assert server.entries[1]["auth"] is None


def test_cli_rejects_positional_spec_with_apis(tmp_path, capsys):
    write_config(tmp_path, "\n[apis.one]\nspec = 'x.json'\n")
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["serve", "some.json", "--config", str(tmp_path / ".mcpify.toml")])
    assert exit_info.value.code == 2
    assert "not both" in capsys.readouterr().err


def test_status_multi_reports_each_api(tmp_path, capsys, api_a, api_b):
    spec_a = tmp_path / "a.json"
    spec_a.write_text(json.dumps(pet_spec(f"http://127.0.0.1:{api_a.server_port}/v1")), encoding="utf-8")
    spec_b = tmp_path / "b.json"
    spec_b.write_text(json.dumps(pet_spec(f"http://127.0.0.1:{api_b.server_port}/v1")), encoding="utf-8")
    write_config(tmp_path, f"""
[apis.alpha]
spec = '{spec_a}'

[apis.beta]
spec = '{spec_b}'
""")
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["status", "--config", str(tmp_path / ".mcpify.toml"), "--timeout", "3"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "[alpha] reachable" in out and "[beta] reachable" in out


def test_status_multi_exit_two_when_one_down(tmp_path, api_a):
    gone = Upstream("G", [])
    threading.Thread(target=gone.serve_forever, daemon=True).start()
    gone.shutdown()
    gone.server_close()
    port = gone.server_port
    spec_a = tmp_path / "a.json"
    spec_a.write_text(json.dumps(pet_spec(f"http://127.0.0.1:{api_a.server_port}/v1")), encoding="utf-8")
    spec_b = tmp_path / "b.json"
    spec_b.write_text(json.dumps(pet_spec(f"http://127.0.0.1:{port}/v1")), encoding="utf-8")
    write_config(tmp_path, f"""
[apis.alpha]
spec = '{spec_a}'

[apis.beta]
spec = '{spec_b}'
""")
    with pytest.raises(SystemExit) as exit_info:
        cli_main(["status", "--config", str(tmp_path / ".mcpify.toml"), "--timeout", "3"])
    assert exit_info.value.code == 2


def test_try_multi_smoke(tmp_path, capsys, monkeypatch, api_a, api_b):
    import io

    spec_a = tmp_path / "a.json"
    spec_a.write_text(json.dumps(pet_spec(f"http://127.0.0.1:{api_a.server_port}/v1")), encoding="utf-8")
    spec_b = tmp_path / "b.json"
    spec_b.write_text(json.dumps(pet_spec(f"http://127.0.0.1:{api_b.server_port}/v1")), encoding="utf-8")
    write_config(tmp_path, f"""
[apis.alpha]
spec = '{spec_a}'

[apis.beta]
spec = '{spec_b}'
""")
    monkeypatch.setattr("sys.stdin", io.StringIO(":q\n"))
    cli_main(["try", "--config", str(tmp_path / ".mcpify.toml")])
    err = capsys.readouterr().err
    assert "2 APIs" in err
