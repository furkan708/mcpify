"""`--wait-on-429`: honoring Retry-After once for idempotent calls.

Paid MCP gateways sell rate-limit handling; this is the free, opt-in
version. The design keeps the project's honesty rules: nothing waits or
retries unless asked, POST/PATCH are never auto-retried, and a wait
longer than the cap returns the 429 untouched.
"""

import http.server
import json
import threading
import time

import pytest

from mcpify.api_server import ApiServer
from mcpify.http_client import execute
from mcpify.spec import load_spec


class RateLimitedAPI(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append(("GET", self.path, time.monotonic()))
        if len([r for r in self.server.requests if r[2] >= self.server.window_start]) == 1:
            payload = b'{"error": "slow down"}'
            self.send_response(429)
            self.send_header("Retry-After", str(self.server.retry_after))
        else:
            payload = json.dumps([{"ok": True}]).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self.server.requests.append(("POST", self.path, time.monotonic()))
        payload = b'{"error": "slow down"}'
        self.send_response(429)
        self.send_header("Retry-After", "1")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RateServer(http.server.HTTPServer):
    def __init__(self, retry_after=1):
        super().__init__(("127.0.0.1", 0), RateLimitedAPI)
        self.requests = []
        self.retry_after = retry_after
        self.window_start = 0.0


@pytest.fixture()
def rate_api():
    server = RateServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def make_server(base, **kwargs):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": f"{base}/v1"}]
    api = ApiServer(spec, f"{base}/v1", **kwargs)
    api.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    api.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return api


def call(server, name, arguments, request_id=9):
    return server.handle_message(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}}
    )


# ---------------------------------------------------------------------------
# execute()-level
# ---------------------------------------------------------------------------

def test_honors_retry_after_once(rate_api):
    request = {"method": "GET", "url": f"http://127.0.0.1:{rate_api.server_port}/v1/pets",
               "headers": {"Accept": "application/json"}, "body": None}
    started = time.monotonic()
    result = execute(request, timeout=5, wait_on_429=5)
    elapsed = time.monotonic() - started
    assert result["status"] == 200
    assert len(rate_api.requests) == 2
    assert elapsed >= 1.0  # actually waited the Retry-After second


def test_cap_returns_429_without_waiting(rate_api):
    rate_api.retry_after = 100  # far beyond the cap
    request = {"method": "GET", "url": f"http://127.0.0.1:{rate_api.server_port}/v1/pets",
               "headers": {"Accept": "application/json"}, "body": None}
    started = time.monotonic()
    result = execute(request, timeout=5, wait_on_429=2)
    elapsed = time.monotonic() - started
    assert result["status"] == 429
    assert len(rate_api.requests) == 1
    assert elapsed < 1.0


def test_disabled_by_default(rate_api):
    request = {"method": "GET", "url": f"http://127.0.0.1:{rate_api.server_port}/v1/pets",
               "headers": {"Accept": "application/json"}, "body": None}
    result = execute(request, timeout=5)
    assert result["status"] == 429
    assert len(rate_api.requests) == 1


def test_missing_retry_after_uses_retry_delay(rate_api, monkeypatch):
    # strip the header: the fallback wait is the retry delay
    rate_api.retry_after = 1  # handler sends "1"; we remove the header below
    real = RateLimitedAPI.send_header

    def no_retry_after(self, keyword, value):
        if keyword == "Retry-After" and self.command == "GET":
            return  # drop it
        real(self, keyword, value)

    RateLimitedAPI.send_header = no_retry_after
    try:
        request = {"method": "GET", "url": f"http://127.0.0.1:{rate_api.server_port}/v1/pets",
                   "headers": {"Accept": "application/json"}, "body": None}
        started = time.monotonic()
        result = execute(request, timeout=5, wait_on_429=5, retry_delay=0.2)
        elapsed = time.monotonic() - started
        assert result["status"] == 200
        assert 0.15 <= elapsed < 0.9  # waited the 0.2s fallback, not 1s
    finally:
        RateLimitedAPI.send_header = real


def test_http_date_retry_after_never_waits(rate_api, monkeypatch):
    real = RateLimitedAPI.send_header

    def date_retry_after(self, keyword, value):
        if keyword == "Retry-After" and self.command == "GET":
            return real(self, keyword, "Wed, 21 Oct 2026 07:28:00 GMT")
        return real(self, keyword, value)

    RateLimitedAPI.send_header = date_retry_after
    try:
        request = {"method": "GET", "url": f"http://127.0.0.1:{rate_api.server_port}/v1/pets",
                   "headers": {"Accept": "application/json"}, "body": None}
        started = time.monotonic()
        result = execute(request, timeout=5, wait_on_429=60)
        elapsed = time.monotonic() - started
        assert result["status"] == 429
        assert len(rate_api.requests) == 1
        assert elapsed < 1.0
    finally:
        RateLimitedAPI.send_header = real


# ---------------------------------------------------------------------------
# ApiServer wiring
# ---------------------------------------------------------------------------

def test_server_passes_wait_on_429(rate_api):
    rate_api.retry_after = 1
    server = make_server(f"http://127.0.0.1:{rate_api.server_port}", wait_on_429=5)
    response = call(server, "list_pets", {})
    assert response["result"].get("isError") is not True
    assert len(rate_api.requests) == 2


def test_post_429_never_waited(rate_api):
    """A 429 explicitly asks for a delay, but writes stay untouched: the
    agent decides what to do with its own POST."""
    rate_api.retry_after = 1
    server = make_server(f"http://127.0.0.1:{rate_api.server_port}", wait_on_429=30)
    started = time.monotonic()
    response = call(server, "create_pet", {"body": {"name": "Rex", "kind": "dog"}})
    elapsed = time.monotonic() - started
    assert response["result"]["isError"] is True
    assert len(rate_api.requests) == 1
    assert elapsed < 1.0


def test_second_429_not_waited_again(rate_api, monkeypatch):
    """Only one courtesy wait: if the API 429s even after waiting, the
    result comes back so the agent can back off itself."""
    real_do_GET = RateLimitedAPI.do_GET

    def always_429(self):
        self.server.requests.append(("GET", self.path, time.monotonic()))
        payload = b'{"error": "slow down"}'
        self.send_response(429)
        self.send_header("Retry-After", "1")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    RateLimitedAPI.do_GET = always_429
    try:
        server = make_server(f"http://127.0.0.1:{rate_api.server_port}", wait_on_429=5)
        started = time.monotonic()
        response = call(server, "list_pets", {})
        elapsed = time.monotonic() - started
        assert response["result"]["isError"] is True
        assert len(rate_api.requests) == 2
        assert 1.0 <= elapsed < 2.5  # exactly one 1s wait, no more
    finally:
        RateLimitedAPI.do_GET = real_do_GET


def test_cli_flag_wiring(tmp_path, monkeypatch):
    """--wait-on-429 reaches the server's execute path (constructor arg)."""
    calls = {}
    captured_kwargs = {}

    def fake_serve_http(server, host, port, token=None, max_body=None):
        calls["server"] = server
        captured_kwargs["wait"] = server.wait_on_429

    import mcpify.http_transport as transport

    monkeypatch.setattr(transport, "serve_http", fake_serve_http)
    spec = tmp_path / "spec.json"
    spec.write_text((load_spec.__module__ and json.dumps({
        "openapi": "3.0.3", "info": {"title": "T", "version": "1"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {"/p": {"get": {"operationId": "p", "responses": {"200": {"description": "ok"}}}}},
    })), encoding="utf-8")
    cli = __import__("mcpify.cli", fromlist=["main"]).main
    cli(["serve", str(spec), "--http", "8080", "--wait-on-429", "12"])
    assert captured_kwargs["wait"] == 12.0
