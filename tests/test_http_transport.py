"""MCP Streamable HTTP transport: protocol behavior over real sockets.

Covers the full error ladder (401/405/411/413/415, JSON-RPC parse and
batch rejections), the stateless lifecycle, bearer-token enforcement,
and the --http bind-string parser.
"""

import http.client
import http.server
import json
import socket
import threading

import pytest

from mcpify.api_server import ApiServer
from mcpify.http_transport import build_http_server, parse_http_bind
from mcpify.spec import load_spec

# ---------------------------------------------------------------------------
# a tiny upstream API (records Authorization, serves /v1/pets)
# ---------------------------------------------------------------------------

class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        payload = json.dumps([{"id": 7, "name": "Pet7", "kind": "cat"}]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def upstream():
    server = http.server.HTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


@pytest.fixture()
def mcp_http(upstream):
    """A live HTTP MCP server over the petstore spec."""
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream)
    server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    httpd = build_http_server(server, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()


def post(base, payload, headers=None, raw_body=None, method="POST"):
    conn = http.client.HTTPConnection(base.split("//")[1].rstrip("/"), timeout=10)
    body = raw_body if raw_body is not None else json.dumps(payload).encode()
    default_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        default_headers.update(headers)
    conn.request(method, "/", body=body, headers=default_headers)
    response = conn.getresponse()
    data = response.read()
    status = response.status
    response_headers = dict(response.getheaders())
    conn.close()
    parsed = None
    if data:
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            parsed = data.decode("utf-8", "replace")
    return status, parsed, response_headers


def rpc(method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if request_id is not None:
        message["id"] = request_id
    return message


# ---------------------------------------------------------------------------
# protocol happy path
# ---------------------------------------------------------------------------

def test_initialize_over_http(mcp_http):
    status, body, headers = post(mcp_http, rpc("initialize", {}))
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body["result"]["serverInfo"]["name"] == "mcpify"
    assert body["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_and_call_roundtrip(mcp_http):
    post(mcp_http, rpc("notifications/initialized", request_id=None))
    status, body, _ = post(mcp_http, rpc("tools/list", {}))
    assert status == 200
    names = [tool["name"] for tool in body["result"]["tools"]]
    assert "list_pets" in names
    assert all("_" not in name or not name.startswith("_") for name in names)

    status, body, _ = post(mcp_http, rpc("tools/call", {"name": "list_pets", "arguments": {}}))
    assert status == 200
    text = body["result"]["content"][0]["text"]
    assert "Pet7" in text


def test_uninitialized_request_rejected(mcp_http):
    # a brand-new server process: no initialize has happened
    spec = load_spec("examples/petstore.json")
    fresh = ApiServer(spec, "http://127.0.0.1:1/v1")
    httpd = build_http_server(fresh, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}/"
    try:
        status, body, _ = post(base, rpc("tools/list", {}))
        assert status == 200
        assert body["error"]["code"] == -32002
    finally:
        httpd.shutdown()


def test_stateless_request_carries_version_in_meta(mcp_http):
    # MCP 2026-07-28 style: no handshake, version rides in _meta
    status, body, _ = post(
        mcp_http,
        rpc("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}),
    )
    assert status == 200
    assert "tools" in body["result"]


def test_notification_returns_202_empty(mcp_http):
    status, body, headers = post(mcp_http, rpc("notifications/initialized", request_id=None))
    assert status == 202
    assert body is None


# ---------------------------------------------------------------------------
# transport-level errors (HTTP status codes)
# ---------------------------------------------------------------------------

def test_get_returns_405(mcp_http):
    status, body, headers = post(mcp_http, None, method="GET", raw_body=b"")
    assert status == 405
    assert "POST" in headers["Allow"]


def test_delete_returns_405(mcp_http):
    status, _, headers = post(mcp_http, None, method="DELETE", raw_body=b"")
    assert status == 405


def test_options_returns_204_with_allow(mcp_http):
    status, _, headers = post(mcp_http, None, method="OPTIONS", raw_body=b"")
    assert status == 204
    assert "POST" in headers["Allow"]


def test_wrong_content_type_415(mcp_http):
    status, body, _ = post(
        mcp_http, rpc("initialize", {}), headers={"Content-Type": "text/plain"}
    )
    assert status == 415
    assert body["error"]["code"] == -32600


def test_missing_content_length_411(mcp_http):
    host_port = mcp_http.split("//")[1].rstrip("/")
    raw = (
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        b"Connection: close\r\n\r\n{}"
    )
    with socket.create_connection((host_port.split(":")[0], int(host_port.split(":")[1])), timeout=5) as sock:
        sock.sendall(raw)
        response = sock.recv(4096).decode("latin1")
    assert response.startswith("HTTP/1.1 411")


def test_oversized_body_413(mcp_http):
    spec = load_spec("examples/petstore.json")
    server = ApiServer(spec, "http://127.0.0.1:1/v1")
    httpd = build_http_server(server, "127.0.0.1", 0, max_body=64)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}/"
    try:
        status, body, _ = post(base, rpc("initialize", {}), raw_body=b"x" * 200)
        assert status == 413
        assert body["error"]["code"] == -32600
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# JSON-RPC-level errors (bodies, not status codes)
# ---------------------------------------------------------------------------

def test_parse_error_400_with_32700(mcp_http):
    status, body, _ = post(mcp_http, None, raw_body=b"{not json")
    assert status == 400
    assert body["error"]["code"] == -32700


def test_batched_messages_rejected_32600(mcp_http):
    status, body, _ = post(mcp_http, [rpc("initialize", {}), rpc("tools/list", {})])
    assert status == 400
    assert body["error"]["code"] == -32600


def test_non_object_rejected_32600(mcp_http):
    status, body, _ = post(mcp_http, 42)
    assert status == 400
    assert body["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# bearer auth
# ---------------------------------------------------------------------------

@pytest.fixture()
def protected_http(upstream):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": upstream}]
    server = ApiServer(spec, upstream)
    server.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    httpd = build_http_server(server, "127.0.0.1", 0, token="sekret")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()


def test_missing_token_401_with_www_authenticate(protected_http):
    status, body, headers = post(protected_http, rpc("initialize", {}))
    assert status == 401
    assert headers.get("WWW-Authenticate") == "Bearer"
    assert "unauthorized" in body["error"]


def test_wrong_token_401(protected_http):
    status, _, _ = post(protected_http, rpc("initialize", {}),
                        headers={"Authorization": "Bearer wrong"})
    assert status == 401


def test_correct_token_passes(protected_http):
    status, body, _ = post(protected_http, rpc("initialize", {}),
                           headers={"Authorization": "Bearer sekret"})
    assert status == 200
    assert body["result"]["serverInfo"]["name"] == "mcpify"


# ---------------------------------------------------------------------------
# --http bind-string parser
# ---------------------------------------------------------------------------

def test_parse_http_bind_variants():
    assert parse_http_bind("8080") == ("127.0.0.1", 8080)
    assert parse_http_bind("0.0.0.0:9000") == ("0.0.0.0", 9000)
    assert parse_http_bind(":8080") == ("0.0.0.0", 8080)
    assert parse_http_bind("*:8080") == ("0.0.0.0", 8080)
    assert parse_http_bind("localhost:1") == ("localhost", 1)


def test_parse_http_bind_rejects_garbage():
    with pytest.raises(ValueError):
        parse_http_bind("host:abc")
    with pytest.raises(ValueError):
        parse_http_bind("host:0")
    with pytest.raises(ValueError):
        parse_http_bind("host:99999")
    with pytest.raises(ValueError):
        parse_http_bind("bad host!:8080")
