"""v1.10.0 operations features: Prometheus metrics, the dashboard,
`mcpify mock`, and hot reload. Every behavior is pinned end-to-end
against real local servers — the same bar as the rest of the suite."""

import argparse
import http.server
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from mcpify import metrics
from mcpify.aggregate import AggregatedServer
from mcpify.api_server import ApiServer
from mcpify.cli import _reload_once, _spec_mtime
from mcpify.cli import main as cli_main
from mcpify.http_client import register_log_sink
from mcpify.mock import build_mock_server, generate_from_schema
from mcpify.spec import load_spec
from mcpify.ui import build_ui_server

# ---------------------------------------------------------------------------
# fixtures: one live upstream API + servers around it
# ---------------------------------------------------------------------------


class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps([{"id": 1, "name": "Rex"}]).encode()
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


@pytest.fixture()
def clean_metrics():
    metrics.disable()
    yield
    metrics.disable()


def fresh_server(base):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": base}]
    return ApiServer(spec, base, enable_preview=True)


def get(url, data=None, headers=None, method=None):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)  # noqa: S310 -- testler yalnizca localhost
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310 -- testler yalnizca localhost
            return response.status, response.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------


def test_metrics_disabled_is_a_noop(clean_metrics):
    metrics.inc("mcpify_tool_calls_total", {"tool": "x"})
    assert metrics.render_prometheus() == ""
    assert metrics.snapshot()["enabled"] is False


def test_metrics_counter_and_histogram_format(clean_metrics):
    metrics.enable()
    metrics.inc("mcpify_tool_calls_total", {"tool": "get_pet", "api": "a", "outcome": "ok"}, 2)
    metrics.inc("mcpify_tool_calls_total", {"tool": "get_pet", "api": "a", "outcome": "error"})
    metrics.observe("mcpify_tool_latency_seconds", {"tool": "get_pet", "api": "a"}, 0.03)
    text = metrics.render_prometheus()
    assert '# TYPE mcpify_tool_calls_total counter' in text
    assert 'mcpify_tool_calls_total{api="a",outcome="ok",tool="get_pet"} 2' in text
    assert 'mcpify_tool_calls_total{api="a",outcome="error",tool="get_pet"} 1' in text
    assert '# TYPE mcpify_tool_latency_seconds histogram' in text
    assert 'mcpify_tool_latency_seconds_bucket{api="a",le="0.05",tool="get_pet"} 1' in text  # tam sirali
    assert 'mcpify_tool_latency_seconds_bucket{api="a",le="+Inf",tool="get_pet"} 1' in text
    assert 'mcpify_tool_latency_seconds_count{api="a",tool="get_pet"} 1' in text


def test_metrics_endpoint_serves_text_format(clean_metrics):
    metrics.enable()
    metrics.inc("mcpify_tool_calls_total", {"tool": "x", "outcome": "ok"}, 5)
    httpd, _thread = metrics.start_metrics_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        status, body = get(f"http://127.0.0.1:{httpd.server_address[1]}/metrics")
        assert status == 200 and "mcpify_tool_calls_total" in body
        status, body = get(f"http://127.0.0.1:{httpd.server_address[1]}/other")
        assert status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_tool_calls_are_counted_with_latency_and_outcome(clean_metrics, upstream):
    metrics.enable()
    server = fresh_server(upstream)
    result = server.run_tool("list_pets", {})
    assert result.get("isError") is not True
    text = metrics.render_prometheus()
    assert 'mcpify_tool_calls_total{api="mcpify",outcome="ok",tool="list_pets"} 1' in text
    assert "mcpify_tool_latency_seconds_count" in text


def test_error_calls_are_labeled_error(clean_metrics):
    metrics.enable()
    server = fresh_server("http://127.0.0.1:1/v1")  # dead port
    server.run_tool("list_pets", {})
    text = metrics.render_prometheus()
    assert 'outcome="error"' in text


def test_cache_hit_and_miss_are_counted(clean_metrics, upstream):
    metrics.enable()
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": upstream}]

    server = ApiServer(spec, upstream, cache_ttl=60)
    server.run_tool("list_pets", {})
    server.run_tool("list_pets", {})
    text = metrics.render_prometheus()
    assert 'mcpify_cache_requests_total{result="miss"} 1' in text
    assert 'mcpify_cache_requests_total{result="hit"} 1' in text


def test_health_probe_sets_api_gauge(clean_metrics):
    metrics.enable()
    server = fresh_server("http://127.0.0.1:1/v1")
    server.run_health_check()
    text = metrics.render_prometheus()
    assert 'mcpify_api_health{api="mcpify"} 0' in text


# ---------------------------------------------------------------------------
# the dashboard
# ---------------------------------------------------------------------------


@pytest.fixture()
def dashboard(upstream):
    server = fresh_server(upstream)
    httpd, _started = build_ui_server(server, "127.0.0.1", 0, None, None)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_dashboard_serves_self_contained_html(dashboard):
    status, body = get(dashboard + "/")
    assert status == 200
    assert "mcpify" in body and "api/state" in body
    assert "http://cdn" not in body and "<script src=" not in body  # zero external resources


def test_state_reports_tools_and_metrics(dashboard):
    status, body = get(dashboard + "/api/state")
    state = json.loads(body)
    assert status == 200
    names = [tool["name"] for tool in state["tools"]]
    assert "list_pets" in names and "mcpify_health" in names
    row = next(tool for tool in state["tools"] if tool["name"] == "get_pet")
    assert row["method"] == "GET" and row["path"] == "/pets/{petId}" and row["readOnly"] is True
    assert row["hasSchema"] is True and row["schema"].get("type") == "object"
    assert state["metrics"]["enabled"] is True  # ui turns recording on
    assert state["config_path"] is None


def test_preview_endpoint_is_masked_and_dry(dashboard):
    status, body = get(
        dashboard + "/api/preview",
        data=json.dumps({"name": "get_pet", "arguments": {"petId": 3}}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    assert status == 200
    text = json.loads(body)["content"][0]["text"]
    assert "GET http" in text and "/pets/3" in text


def test_preview_bad_input_is_a_400(dashboard):
    status, _body = get(
        dashboard + "/api/preview",
        data=json.dumps({"name": "nope"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    assert status == 400


def test_health_endpoint_builds_history(dashboard):
    status, body = get(dashboard + "/api/health", data=b"{}", method="POST")
    report = json.loads(body)
    assert status == 200 and report["api_reachable"] is True  # upstream her GET'e 200 doner
    status, body = get(dashboard + "/api/state")
    state = json.loads(body)
    assert state["last_health"] is not None
    assert "mcpify" in state["health_history"]


def test_token_protects_every_route(upstream):
    server = fresh_server(upstream)
    httpd, _ = build_ui_server(server, "127.0.0.1", 0, "sekret", None)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        assert get(base + "/api/state")[0] == 401
        assert get(base + "/")[0] == 401
        assert get(base + "/api/state?token=wrong")[0] == 401
        assert get(base + "/api/state?token=sekret")[0] == 200
        assert get(base + "/api/state", headers={"Authorization": "Bearer sekret"})[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_config_form_writes_valid_toml(dashboard, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    status, body = get(
        dashboard + "/api/config",
        data=json.dumps({
            "spec": "openapi.json", "base-url": "https://x.io", "auth-env": "TOK",
            "format": "auto", "cache-ttl": 30, "retry": 2, "timeout": 15,
            "read-only": True, "lazy": False,
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    assert status == 200
    assert json.loads(body)["path"] == ".mcpify.toml"
    text = (tmp_path / ".mcpify.toml").read_text(encoding="utf-8")
    assert "[serve]" in text and "spec = 'openapi.json'" in text and "read-only = true" in text
    from mcpify.config import load_config, validate

    _path, data = load_config(str(tmp_path / ".mcpify.toml"))
    assert validate(data) == []


def test_config_form_rejects_unknown_keys(dashboard):
    status, _body = get(
        dashboard + "/api/config",
        data=json.dumps({"spec": "x", "evil-key": 1}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    assert status == 400


def test_log_ring_receives_masked_lines(upstream):
    lines: list[str] = []
    register_log_sink(lines.append)
    server = fresh_server("http://127.0.0.1:1/v1")
    server.run_tool("list_pets", {})
    assert lines, "sink must see log lines"
    assert all("authorization" not in line.lower() or "***" in line for line in lines)


# ---------------------------------------------------------------------------
# mcpify mock
# ---------------------------------------------------------------------------

MOCK_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/users/{userId}": {
            "get": {
                "operationId": "getUser",
                "responses": {
                    "200": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}
                    }
                },
            }
        },
        "/ping": {
            "get": {
                "operationId": "ping",
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["pong", "bang"]},
                        "checkedAt": {"type": "string", "format": "date-time"},
                        "contact": {"type": "string", "format": "email"},
                        "tries": {"type": "integer"},
                    },
                }}}}},
            }
        },
    },
    "components": {"schemas": {"User": {
        "type": "object",
        "required": ["id", "name", "active"],
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "active": {"type": "boolean"},
        },
    }}},
}


def test_generate_examples_beat_everything():
    schema = {"type": "integer", "examples": [7]}
    assert generate_from_schema(schema, {}) == 7


def test_generate_enum_format_and_type_fallbacks():
    spec = {"components": {"schemas": {}}}
    value = generate_from_schema(MOCK_SPEC["paths"]["/ping"]["get"]["responses"]["200"]
                                 ["content"]["application/json"]["schema"], spec)
    assert value == {
        "status": "pong",  # enum[0]
        "checkedAt": "2026-01-01T00:00:00Z",
        "contact": "user@example.com",
        "tries": 1,
    }


def test_generate_resolves_refs_and_required_only():
    value = generate_from_schema({"$ref": "#/components/schemas/User"}, MOCK_SPEC)
    assert value == {"id": "1", "name": "Mock name", "active": False}


def test_mock_server_serves_schema_shaped_responses():
    httpd = build_mock_server(MOCK_SPEC, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        status, body = get(base + "/users/42")
        assert status == 200 and json.loads(body) == {"id": "1", "name": "Mock name", "active": False}
        status, body = get(base + "/ping")
        assert status == 200 and json.loads(body)["status"] == "pong"
        status, body = get(base + "/unknown")
        assert status == 404 and "known_routes" in json.loads(body)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_mock_rejects_config_flag(capsys):
    with pytest.raises(SystemExit):
        cli_main(["mock", "x.json", "--config", "c.toml"])
    assert "unrecognized arguments" in capsys.readouterr().err  # argparse net reddeder


# ---------------------------------------------------------------------------
# hot reload
# ---------------------------------------------------------------------------


def write_spec(path, operation_id):
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "https://x.io"}],
        "paths": {"/thing": {"get": {"operationId": operation_id, "responses": {"200": {"description": "ok"}}}}},
    }
    path.write_text(json.dumps(spec), encoding="utf-8")


def test_reload_once_rebuilds_on_mtime_change(tmp_path, upstream):
    spec_path = tmp_path / "spec.json"
    write_spec(spec_path, "oldName")
    spec_payload = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_payload["servers"] = [{"url": upstream}]
    spec_path.write_text(json.dumps(spec_payload), encoding="utf-8")

    server = fresh_server(upstream)
    args = argparse_namespace(str(spec_path))
    last = {str(spec_path): _spec_mtime(str(spec_path))}

    def apply():
        server.reload_tools(_rebuild_single_tools_public(args))

    # no change -> no rebuild
    assert _reload_once([str(spec_path)], last, apply) == last
    assert "oldName" not in [tool["name"] for tool in server.tools]  # still the petstore surface

    # change -> rebuild onto the SAME server object (distinct mtime guaranteed)
    write_spec(spec_path, "brandNewTool")
    stamp = time.time() + 30
    os.utime(spec_path, (stamp, stamp))
    new_last = _reload_once([str(spec_path)], last, apply)
    assert new_last != last
    assert "brandnewtool" in [tool["name"] for tool in server.tools]  # operationId sluglasir
    assert server.by_name["brandnewtool"]  # lookup map swapped too


def test_reload_keeps_previous_surface_on_broken_spec(tmp_path):
    spec_path = tmp_path / "spec.json"
    write_spec(spec_path, "oldName")
    server = fresh_server("https://x.io")
    args = argparse_namespace(str(spec_path))
    last = {str(spec_path): _spec_mtime(str(spec_path))}
    write_spec(spec_path, "newName")
    spec_path.write_text("{ not json", encoding="utf-8")  # broken
    _reload_once([str(spec_path)], last, lambda: server.reload_tools(_rebuild_single_tools_public(args)))
    assert "brandnewtool" not in [tool["name"] for tool in server.tools]
    assert server.tools  # old surface intact


def test_aggregated_reload_swaps_entries_and_owners(upstream):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": upstream}]
    entry = {
        "label": "one", "spec": spec, "spec_path": "a.json", "base": upstream,
        "auth": None, "timeout": 5.0, "cache": None, "retry": 0,
        "retry_delay": 1.0, "wait_on_429": 0.0, "tools": _tools_for(spec),
    }
    agg = AggregatedServer([entry])
    assert "list_pets" in [tool["name"] for tool in agg.tools]
    fresh = dict(entry)
    fresh["label"] = "two"
    fresh["tools"] = _tools_for(spec)
    agg.reload_entries([fresh])
    names = [tool["name"] for tool in agg.tools]
    assert "list_pets" in names  # single entry, no collision -> bare names
    assert agg._owners and all(index == 0 for index in agg._owners.values())


def test_spec_mtime_ignores_urls():
    assert _spec_mtime("https://example.com/openapi.json") is None
    assert _spec_mtime("/definitely/missing.json") is None


# -- helpers ---------------------------------------------------------------


def argparse_namespace(spec):

    return argparse.Namespace(spec=spec, strict=False, read_only=False, tag=None,
                              include=None, exclude=None, allow=None, deny=None)


def _rebuild_single_tools_public(args):
    from mcpify.cli import _rebuild_single_tools

    return _rebuild_single_tools(args)


def _tools_for(spec):
    from mcpify.tools import spec_to_tools

    return spec_to_tools(spec)
