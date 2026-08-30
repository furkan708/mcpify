"""End-to-end: MCP protocol over stdio against a real local HTTP API."""

import http.server
import json
import threading

import pytest

from mcpify.api_server import ApiServer
from mcpify.spec import load_spec

# ---------------------------------------------------------------------------
# a fake pet API
# ---------------------------------------------------------------------------

class FakePetAPI(http.server.BaseHTTPRequestHandler):
    seen = []  # class-level request log

    def log_message(self, *args):
        pass

    def _record(self, body=None):
        FakePetAPI.seen.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record()
        if self.path.startswith("/v1/pets/"):
            pet_id = int(self.path[len("/v1/pets/"):])
            self._send(200, {"id": pet_id, "name": f"Pet{pet_id}", "kind": "cat"})
        elif self.path.startswith("/v1/pets"):
            self._send(200, [{"id": 1, "name": "Rex", "kind": "dog"}])
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw or b"{}")
        self._record(payload)
        self._send(201, {"id": 42, **payload})


@pytest.fixture()
def api():
    FakePetAPI.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), FakePetAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def server(api, tmp_path):
    spec_path = tmp_path / "spec.json"
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": api}]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    server = ApiServer(load_spec(str(spec_path)), api, auth=None)
    server.handle_message(rpc("initialize"))
    server.handle_message(rpc("notifications/initialized"))
    return server


def rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_full_flow_get(server):
    assert server.handle_message(rpc("initialize", {}))["result"]["serverInfo"]["name"] == "mcpify"

    listing = server.handle_message(rpc("tools/list"))
    names = {t["name"] for t in listing["result"]["tools"]}
    assert "list_pets" in names and "get_pet" in names and "create_pet" in names

    response = server.handle_message(rpc("tools/call", {"name": "list_pets", "arguments": {"limit": "5"}}))
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload[0]["name"] == "Rex"


def test_path_parameter_call(server):
    response = server.handle_message(rpc("tools/call", {"name": "get_pet", "arguments": {"petId": 9}}))
    pet = json.loads(response["result"]["content"][0]["text"])
    assert pet == {"id": 9, "name": "Pet9", "kind": "cat"}


def test_post_with_body(server):
    response = server.handle_message(
        rpc("tools/call", {"name": "create_pet", "arguments": {"body": {"name": "Milo", "kind": "cat"}}})
    )
    pet = json.loads(response["result"]["content"][0]["text"])
    assert pet["id"] == 42 and pet["name"] == "Milo"
    # the fake API must have received a proper JSON POST
    post = next(r for r in FakePetAPI.seen if r["method"] == "POST")
    assert post["path"] == "/v1/pets"
    assert post["body"] == {"name": "Milo", "kind": "cat"}


def test_http_404_is_tool_error_not_crash(server):
    response = server.handle_message(rpc("tools/call", {"name": "get_stats", "arguments": {}}))
    assert response["result"]["isError"] is True
    assert "404" in response["result"]["content"][0]["text"]


def test_missing_required_argument_is_tool_error(server):
    response = server.handle_message(rpc("tools/call", {"name": "get_pet", "arguments": {}}))
    assert response["result"]["isError"] is True
    assert "petId" in response["result"]["content"][0]["text"]


def test_unknown_argument_is_tool_error(server):
    response = server.handle_message(
        rpc("tools/call", {"name": "list_pets", "arguments": {"nonsense": "1"}})
    )
    assert response["result"]["isError"] is True


def test_unknown_tool_is_protocol_error(server):
    response = server.handle_message(rpc("tools/call", {"name": "nope"}))
    assert response["error"]["code"] == -32601


def test_bearer_auth_reaches_the_api(api, tmp_path):
    from mcpify.tools import AuthConfig

    spec_path = tmp_path / "spec.json"
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": api}]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    server = ApiServer(load_spec(str(spec_path)), api, auth=AuthConfig("TEST_TOKEN", "bearer"))
    server.handle_message(rpc("initialize"))
    server.handle_message(rpc("notifications/initialized"))

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("TEST_TOKEN", "super-secret")
    try:
        response = server.handle_message(
            rpc("tools/call", {"name": "create_pet", "arguments": {"body": {"name": "A", "kind": "dog"}}})
        )
    finally:
        monkeypatch.undo()
    assert response["result"].get("isError") is not True
    post = next(r for r in FakePetAPI.seen if r["method"] == "POST")
    assert post["authorization"] == "Bearer super-secret"


def test_serve_over_real_streams(server):
    import io

    messages = [
        json.dumps(rpc("initialize", {}, request_id=1)),
        json.dumps(rpc("notifications/initialized")),
        json.dumps(rpc("tools/list", request_id=2)),
        json.dumps(rpc("tools/call", {"name": "get_pet", "arguments": {"petId": 3}}, request_id=3)),
    ]
    out = io.StringIO()
    server.serve(stdin=iter(messages), stdout=out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(lines) == 3  # notification is not answered
    pet = json.loads(lines[2]["result"]["content"][0]["text"])
    assert pet["id"] == 3
