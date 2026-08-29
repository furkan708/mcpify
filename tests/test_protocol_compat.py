"""Protocol-version compatibility: 2025-06-18 handshake AND 2026-07-28 stateless.

The 2026-07-28 spec removed the initialize handshake; every request now
carries io.modelcontextprotocol/protocolVersion in params._meta. Legacy
clients keep the handshake. Both generations must work against the same
server — and a legacy client with neither mechanism must still be
rejected with -32002.
"""

import http.server
import json
import threading

import pytest

from mcpify.api_server import ApiServer

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Compat", "version": "1.0"},
    "servers": [{"url": "http://placeholder"}],
    "paths": {
        "/dogs/{dogId}": {
            "get": {
                "operationId": "get_dog",
                "summary": "Get one dog",
                "parameters": [
                    {"name": "dogId", "in": "path", "required": True, "schema": {"type": "integer"}}
                ],
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                    "required": ["id", "name"],
                }}}}},
            }
        }
    },
}


def stateless_server(base):
    return ApiServer(SPEC, base)


class _API(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        dog_id = int(self.path.rsplit("/", 1)[1])
        body = json.dumps({"id": dog_id, "name": "Rex"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def base():
    server = http.server.HTTPServer(("127.0.0.1", 0), _API)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_stateless_request_without_handshake_is_accepted(base):
    server = stateless_server(base)
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
    })
    assert "error" not in response
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "get_dog" in names


def test_stateless_tool_call_executes_end_to_end(base):
    server = stateless_server(base)
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "get_dog", "arguments": {"dogId": 3},
            "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
        },
    })
    assert response["result"]["structuredContent"] == {"id": 3, "name": "Rex"}


def test_legacy_client_without_handshake_still_rejected(base):
    server = stateless_server(base)
    response = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
    assert response["error"]["code"] == -32002


def test_legacy_handshake_path_still_works(base):
    server = stateless_server(base)
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    response = server.handle_message({"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}})
    assert "error" not in response


def test_stateless_version_value_is_not_validated_too_hard(base):
    """Any declared version string unlocks the server: forward compatibility."""
    server = stateless_server(base)
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 5, "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2027-01-01"}},
    })
    assert "error" not in response
