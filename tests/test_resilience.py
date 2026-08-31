"""Session resilience: upstream failures must become tool errors, never
dead connections (the FastMCP #1753 failure mode — a timeout/disconnect
kills the whole server and the client sees "connection closed").

Covers the three ways an upstream can fail mid-call: read timeout,
connection refused/reset, and an unexpected exception inside a tool.
Plus doctor's untyped-parameter visibility and the YAML fast path.
"""

import http.server
import json
import threading
import time

import pytest

from mcpify.api_server import ApiServer
from mcpify.spec import load_spec

SPEC = "examples/petstore.json"


def rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


class SlowAPI(http.server.BaseHTTPRequestHandler):
    """Every GET sleeps past the client timeout — the read-phase case."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        time.sleep(1.5)
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def slow_api():
    server = http.server.HTTPServer(("127.0.0.1", 0), SlowAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    server.server_close()


def _initialized(spec, base, **kwargs):
    instance = ApiServer(load_spec(spec), base, **kwargs)
    instance.handle_message(rpc("initialize"))
    instance.handle_message(rpc("notifications/initialized"))
    return instance


def _text(response):
    return response["result"]["content"][0]["text"]


# ---------------------------------------------------------------------------
# read timeout: the call that used to kill the server
# ---------------------------------------------------------------------------

def test_read_timeout_returns_tool_error_not_crash(slow_api, tmp_path):
    spec_path = tmp_path / "spec.json"
    spec = load_spec(SPEC)
    spec["servers"] = [{"url": slow_api}]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    server = _initialized(str(spec_path), slow_api, timeout=0.3, retry=0)

    started = time.monotonic()
    response = server.handle_message(rpc("tools/call", {"name": "list_pets", "arguments": {}}))
    elapsed = time.monotonic() - started

    assert response["result"]["isError"] is True
    assert "timeout" in _text(response).lower()
    assert "--timeout" in _text(response)  # the remediation names the lever
    assert elapsed < 1.4  # it failed at ~0.3s, it did not wait out the server


def test_session_survives_timeout(slow_api, tmp_path):
    spec_path = tmp_path / "spec.json"
    spec = load_spec(SPEC)
    spec["servers"] = [{"url": slow_api}]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    server = _initialized(str(spec_path), slow_api, timeout=0.3, retry=0)

    server.handle_message(rpc("tools/call", {"name": "list_pets", "arguments": {}}))

    listing = server.handle_message(rpc("tools/list"))  # same session, still answering
    assert "list_pets" in {t["name"] for t in listing["result"]["tools"]}


# ---------------------------------------------------------------------------
# connection refused: base URL points at a closed port
# ---------------------------------------------------------------------------

def test_connection_refused_is_clean_tool_error(tmp_path):
    dead_port = "http://127.0.0.1:1/v1"  # nothing listens on port 1
    server = _initialized(SPEC, dead_port, retry=0)

    response = server.handle_message(rpc("tools/call", {"name": "list_pets", "arguments": {}}))
    assert response["result"]["isError"] is True
    text = _text(response)
    assert "connection failed" in text.lower()
    assert "base URL" in text  # remediation tells the agent what to check

    listing = server.handle_message(rpc("tools/list"))
    assert listing["result"]["tools"]  # session alive after the failure


# ---------------------------------------------------------------------------
# unknown bug inside a tool: error result, not a dead stdio loop
# ---------------------------------------------------------------------------

def test_unexpected_exception_becomes_tool_error(tmp_path, monkeypatch):
    server = _initialized(SPEC, "https://api.example.com/v1")

    def explode(self, name, arguments):
        raise RuntimeError("boom inside the tool")

    monkeypatch.setattr(ApiServer, "run_tool", explode)
    response = server.handle_message(rpc("tools/call", {"name": "list_pets", "arguments": {}}))
    assert response["result"]["isError"] is True
    assert "internal error" in _text(response)
    assert "RuntimeError" in _text(response)
    monkeypatch.undo()

    listing = server.handle_message(rpc("tools/list"))
    assert listing["result"]["tools"]


# ---------------------------------------------------------------------------
# doctor: parameters without schema/type are counted and reported
# ---------------------------------------------------------------------------

UNTYPED_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Untyped", "version": "1.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/orders/{id}": {
            "delete": {
                "operationId": "deleteOrder",
                "summary": "Delete an order",
                "parameters": [
                    {"name": "id", "in": "path", "required": True},  # no schema
                ],
                "responses": {"204": {"description": "gone"}},
            }
        },
        "/search": {
            "get": {
                "operationId": "search",
                "summary": "Search",
                "parameters": [
                    {"name": "q", "in": "query"},  # no schema
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


@pytest.fixture()
def untyped_spec(tmp_path):
    path = tmp_path / "untyped.json"
    path.write_text(json.dumps(UNTYPED_SPEC), encoding="utf-8")
    return path


def test_doctor_reports_untyped_parameters_json(untyped_spec, capsys):
    from mcpify.cli import main as cli_main

    with pytest.raises(SystemExit) as exit_info:
        cli_main(["doctor", "--json", str(untyped_spec)])
    assert exit_info.value.code == 1  # doctor --json's own CI-gate convention: warnings -> exit 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["untyped_parameters"] == 2
    assert any("no schema/type" in warning for warning in payload["warnings"])


def test_doctor_reports_untyped_parameters_human(untyped_spec, capsys):
    from mcpify.cli import main as cli_main

    cli_main(["doctor", str(untyped_spec)])  # human mode warns, exits 0 (JSON mode is the CI gate)
    err = capsys.readouterr().out
    assert "2 parameter(s) have no schema/type" in err


def test_doctor_clean_when_every_param_typed(tmp_path, capsys):
    spec = json.loads(json.dumps(UNTYPED_SPEC))
    for operation in (spec["paths"]["/orders/{id}"]["delete"], spec["paths"]["/search"]["get"]):
        for param in operation.get("parameters", []):
            param.setdefault("schema", {"type": "string"})
    path = tmp_path / "typed.json"
    path.write_text(json.dumps(spec), encoding="utf-8")

    from mcpify.cli import main as cli_main

    cli_main(["doctor", "--json", str(path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["untyped_parameters"] == 0
    assert not any("schema/type" in warning for warning in payload["warnings"])


# ---------------------------------------------------------------------------
# CLI regressions: `mock` was a silent no-op; URL specs crashed status/diff
# with UnboundLocalError (a function-local `from urllib.parse import urlparse`
# shadowed the module import for the whole of main())
# ---------------------------------------------------------------------------

def test_mock_command_serves_the_fake_api(tmp_path):
    import socket
    import subprocess
    import sys
    import urllib.request

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcpify", "mock", SPEC, "--http", str(free_port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        banner = proc.stderr.readline()
        assert "mcpify mock: fake API" in banner  # it must announce itself...
        with urllib.request.urlopen(f"http://127.0.0.1:{free_port}/pets", timeout=5) as response:
            assert response.status == 200  # ...and actually answer
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_mock_rejects_bad_bind_cleanly(capsys):
    from mcpify.cli import main as cli_main

    with pytest.raises(SystemExit) as exit_info:
        cli_main(["mock", SPEC, "--http", "not-a-port"])
    assert exit_info.value.code == 2
    assert "--http" in capsys.readouterr().err


def test_status_with_url_spec_fails_cleanly(capsys):
    """Regression: any URL spec used to crash status with a traceback."""
    from mcpify.cli import main as cli_main

    with pytest.raises(SystemExit) as exit_info:
        cli_main(["status", "http://127.0.0.1:1/spec.json"])  # nothing listens there
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "could not fetch" in err
    assert "Traceback" not in err and "UnboundLocalError" not in err


def test_diff_with_url_spec_fails_cleanly(capsys):
    """Regression: URL specs used to crash diff the same way."""
    from mcpify.cli import main as cli_main

    with pytest.raises(SystemExit) as exit_info:
        cli_main(["diff", "http://127.0.0.1:1/spec.json", SPEC])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "could not fetch" in err
    assert "Traceback" not in err and "UnboundLocalError" not in err


# ---------------------------------------------------------------------------
# YAML fast path: the C loader (libyaml) parses exactly what SafeLoader parses
# ---------------------------------------------------------------------------

YAML_SPEC = """\
openapi: 3.0.0
info:
  title: Yaml API
  version: "1.0"
servers:
  - url: https://api.example.com
paths:
  /pets:
    get:
      operationId: listPets
      summary: List pets
      parameters:
        - name: limit
          in: query
          schema:
            type: integer
      responses:
        "200":
          description: ok
"""


yaml = pytest.importorskip("yaml")


def test_yaml_spec_parses_with_default_loader(tmp_path):
    path = tmp_path / "spec.yaml"
    path.write_text(YAML_SPEC, encoding="utf-8")
    spec = load_spec(str(path))
    assert spec["info"]["title"] == "Yaml API"
    assert spec["paths"]["/pets"]["get"]["operationId"] == "listPets"


def test_yaml_pure_python_fallback_parses_identically(tmp_path, monkeypatch):
    """No libyaml in the environment -> the SafeLoader path still works."""
    import mcpify.spec as spec_mod

    monkeypatch.setattr(spec_mod, "_yaml_loader", lambda: yaml.SafeLoader)
    path = tmp_path / "spec.yaml"
    path.write_text(YAML_SPEC, encoding="utf-8")

    via_fallback = load_spec(str(path))
    monkeypatch.undo()
    via_default = load_spec(str(path))
    assert via_fallback == via_default
