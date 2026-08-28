"""Checklist-driven lifecycle and hygiene tests.

Covers: MCP initialization enforcement, server version sync, stdout
cleanliness (JSON-RPC stream must never be polluted), and doctor's
auth guidance.
"""

import json

from mcpify import __version__
from mcpify.api_server import ApiServer
from mcpify.cli import main
from mcpify.spec import load_spec


def make_server():
    return ApiServer(load_spec("examples/petstore.json"), "https://api.example.com/v1")


def rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


# ---------- MCP lifecycle ----------

def test_tools_rejected_before_initialization():
    server = make_server()
    response = server.handle_message(rpc("tools/list"))
    assert response["error"]["code"] == -32002
    assert "not initialized" in response["error"]["message"]


def test_tool_call_rejected_before_initialization():
    server = make_server()
    response = server.handle_message(rpc("tools/call", {"name": "list_pets"}))
    assert response["error"]["code"] == -32002


def test_full_handshake_unlocks_tools():
    server = make_server()
    assert server.handle_message(rpc("initialize"))["result"]["serverInfo"]
    # notifications have no id and produce no response
    assert server.handle_message(rpc("notifications/initialized")) is None
    listing = server.handle_message(rpc("tools/list"))
    assert "tools" in listing["result"]


def test_unknown_methods_still_protocol_errors():
    server = make_server()
    server.handle_message(rpc("initialize"))
    server.handle_message(rpc("notifications/initialized"))
    assert server.handle_message(rpc("no/such"))["error"]["code"] == -32601


# ---------- version hygiene ----------

def test_server_version_matches_package():
    server = make_server()
    info = server.handle_message(rpc("initialize"))["result"]["serverInfo"]
    assert info["version"] == __version__


# ---------- stdout cleanliness (critical for stdio servers) ----------

def test_debug_logging_goes_to_stderr_never_stdout(monkeypatch, capsys):
    import mcpify.http_client as hc

    monkeypatch.setattr(hc, "_DEBUG", True)
    hc.execute({"url": "http://127.0.0.1:1/x?q=secret", "headers": {}, "method": "GET"})
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is reserved for JSON-RPC
    assert "ERROR" in captured.err  # uppercase level on stderr
    assert "secret" not in captured.err  # query credentials never logged


def test_serve_writes_only_jsonrpc_to_stdout():
    import io

    server = make_server()
    inp = io.StringIO(
        json.dumps(rpc("initialize", {}, request_id=1)) + "\n"
        + json.dumps(rpc("notifications/initialized"), ) + "\n"
        + json.dumps(rpc("tools/list", request_id=2)) + "\n"
    )
    out = io.StringIO()
    server.serve(stdin=inp, stdout=out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(lines) == 2  # only the two requests got responses
    assert all("result" in line for line in lines)


# ---------- doctor auth guidance ----------

def test_doctor_warns_about_security_schemes(tmp_path, capsys):
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Secured", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {"/x": {"get": {"operationId": "x",
                                 "summary": "X",
                                 "responses": {"200": {"description": "ok"}}}}},
        "components": {"securitySchemes": {"bearerAuth": {"type": "http",
                                                          "scheme": "bearer"}}},
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    main(["doctor", str(spec_path)])
    captured = capsys.readouterr()
    assert "security schemes" in captured.out
    assert "--auth-env" in captured.out
