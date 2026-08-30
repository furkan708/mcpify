"""OAuth2 client-credentials: token fetch, cache, expiry, 401 self-heal.

Every failure mode the flow can hit is a regression test: missing env
vars, token-endpoint errors (HTTP, non-JSON, unreachable), and the
one-shot retry after a mid-flight 401.
"""

import base64
import http.server
import json
import threading
import urllib.parse

import pytest

from mcpify.api_server import ApiServer
from mcpify.http_client import OAuth2ClientCredentials
from mcpify.spec import load_spec
from mcpify.tools import RequestError

# ---------------------------------------------------------------------------
# fakes: a token endpoint and a tiny API that records Authorization headers
# ---------------------------------------------------------------------------

class TokenHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        form = dict(urllib.parse.parse_qsl(raw.decode()))
        self.server.requests.append(
            {"authorization": self.headers.get("Authorization"), "form": form}
        )
        reply = self.server.next_reply.pop(0) if self.server.next_reply else (200, {"access_token": "T-OK", "expires_in": 3600})
        status, payload = reply
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TokenServer(http.server.HTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), TokenHandler)
        self.requests = []
        self.next_reply = []


class RecordingAPI(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.auth_seen.append(self.headers.get("Authorization"))
        if getattr(self.server, "reject_token", None) == self.headers.get("Authorization"):
            payload = b'{"error": "expired"}'
            self.send_response(401)
        else:
            payload = json.dumps([{"ok": True}]).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class APIServer(http.server.HTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), RecordingAPI)
        self.auth_seen = []


@pytest.fixture()
def token_server():
    server = TokenServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture()
def api():
    server = APIServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_oauth(token_server, clock=None, **kwargs):
    return OAuth2ClientCredentials(
        f"http://127.0.0.1:{token_server.server_port}/token",
        client_id_env="OAUTH2_ID",
        client_secret_env="OAUTH2_SECRET",
        clock=clock,
        **kwargs,
    )


def make_server(base, auth, tmp_path):
    spec = load_spec("examples/petstore.json")
    spec["servers"] = [{"url": f"{base}/v1"}]
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    server = ApiServer(load_spec(str(spec_path)), f"{base}/v1", auth=auth)
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def call(server, name, arguments, request_id=9):
    return server.handle_message(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}}
    )


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_token_fetched_once_and_reused(token_server, api, tmp_path, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    auth = make_oauth(token_server)
    server = make_server(f"http://127.0.0.1:{api.server_port}", auth, tmp_path)
    first = call(server, "list_pets", {}, 2)
    second = call(server, "list_pets", {}, 3)
    assert first["result"].get("isError") is not True
    assert second["result"].get("isError") is not True
    assert len(token_server.requests) == 1  # second call hit the cache
    assert api.auth_seen == ["Bearer T-OK", "Bearer T-OK"]


def test_basic_auth_by_default(token_server, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    auth = make_oauth(token_server)
    auth.headers()
    sent = token_server.requests[0]
    expected = base64.b64encode(b"client-a:shhh").decode()
    assert sent["authorization"] == f"Basic {expected}"
    assert sent["form"]["grant_type"] == "client_credentials"
    assert "client_id" not in sent["form"]


def test_body_auth_mode(token_server, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    auth = make_oauth(token_server, client_auth="body")
    auth.headers()
    sent = token_server.requests[0]
    assert sent["authorization"] is None  # no Basic header in body mode
    assert sent["form"]["client_id"] == "client-a"
    assert sent["form"]["client_secret"] == "shhh"


def test_public_client_body_mode_sends_id_without_secret(token_server, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "public-app")
    auth = OAuth2ClientCredentials(
        f"http://127.0.0.1:{token_server.server_port}/token",
        client_id_env="OAUTH2_ID",
        client_auth="body",
    )
    auth.headers()
    sent = token_server.requests[0]
    assert sent["form"]["client_id"] == "public-app"
    assert "client_secret" not in sent["form"]


def test_scope_sent_when_given(token_server, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    auth = make_oauth(token_server, scope="read write")
    auth.headers()
    assert token_server.requests[0]["form"]["scope"] == "read write"


def test_invalid_client_auth_style_rejected():
    with pytest.raises(ValueError):
        OAuth2ClientCredentials("http://x/token", "ID", client_auth="magic")


# ---------------------------------------------------------------------------
# expiry / caching (fake clock, no sleeps)
# ---------------------------------------------------------------------------

def test_refresh_after_expiry_margin(token_server, api, tmp_path, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    clock = FakeClock()
    token_server.next_reply = [(200, {"access_token": "T1", "expires_in": 100})]
    auth = make_oauth(token_server, clock=clock)
    server = make_server(f"http://127.0.0.1:{api.server_port}", auth, tmp_path)

    call(server, "list_pets", {}, 2)
    clock.advance(50)  # 50 < 100-30: still valid
    call(server, "list_pets", {}, 3)
    assert len(token_server.requests) == 1
    assert api.auth_seen[-1] == "Bearer T1"

    clock.advance(30)  # 80 > 70: inside the refresh margin
    token_server.next_reply = [(200, {"access_token": "T2", "expires_in": 100})]
    call(server, "list_pets", {}, 4)
    assert len(token_server.requests) == 2
    assert api.auth_seen[-1] == "Bearer T2"


def test_missing_expires_in_defaults_to_3600(token_server, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    clock = FakeClock()
    token_server.next_reply = [(200, {"access_token": "LONG"})]
    auth = make_oauth(token_server, clock=clock)
    auth.headers()
    clock.advance(3500)  # inside the assumed 3600s window
    auth.headers()
    assert len(token_server.requests) == 1
    clock.advance(200)  # past it
    auth.headers()
    assert len(token_server.requests) == 2


def test_non_integer_expires_in_falls_back(token_server, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    clock = FakeClock()
    token_server.next_reply = [(200, {"access_token": "X", "expires_in": "soon"})]
    auth = make_oauth(token_server, clock=clock)
    auth.headers()
    clock.advance(3601)
    auth.headers()
    assert len(token_server.requests) == 2


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------

def test_missing_client_id_env_names_it(token_server, monkeypatch):
    monkeypatch.delenv("OAUTH2_ID", raising=False)
    auth = make_oauth(token_server)
    with pytest.raises(RequestError) as err:
        auth.headers()
    assert "OAUTH2_ID" in str(err.value)


def test_missing_secret_env_names_it(token_server, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.delenv("OAUTH2_SECRET", raising=False)
    auth = make_oauth(token_server)
    with pytest.raises(RequestError) as err:
        auth.headers()
    assert "OAUTH2_SECRET" in str(err.value)


def test_token_endpoint_400_surfaces_server_error(token_server, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "wrong")
    token_server.next_reply = [(400, {"error": "invalid_client", "error_description": "bad secret"})]
    auth = make_oauth(token_server)
    with pytest.raises(RequestError) as err:
        auth.headers()
    assert "HTTP 400" in str(err.value)
    assert "invalid_client" in str(err.value) or "bad secret" in str(err.value)


def test_token_endpoint_non_json_body(token_server, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    token_server.next_reply = [(200, None)]  # handler will dump `null`
    auth = make_oauth(token_server)
    with pytest.raises(RequestError):
        auth.headers()


def test_token_endpoint_unreachable(monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    auth = OAuth2ClientCredentials(
        "http://127.0.0.1:1/token",  # nothing listens here
        client_id_env="OAUTH2_ID",
        client_secret_env="OAUTH2_SECRET",
        timeout=2.0,
    )
    with pytest.raises(RequestError) as err:
        auth.headers()
    assert "unreachable" in str(err.value)


# ---------------------------------------------------------------------------
# 401 self-heal through the full ApiServer path
# ---------------------------------------------------------------------------

def test_expired_token_401_triggers_single_refresh(token_server, api, tmp_path, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    auth = make_oauth(token_server)
    api.reject_token = "Bearer T1"  # the first token is revoked upstream
    token_server.next_reply = [
        (200, {"access_token": "T1", "expires_in": 3600}),
        (200, {"access_token": "T2", "expires_in": 3600}),
    ]
    server = make_server(f"http://127.0.0.1:{api.server_port}", auth, tmp_path)
    response = call(server, "list_pets", {}, 2)
    assert response["result"].get("isError") is not True
    assert len(token_server.requests) == 2  # initial + self-heal
    assert api.auth_seen[0] == "Bearer T1"
    assert api.auth_seen[1] == "Bearer T2"  # fresh token succeeded


def test_permanent_401_reported_not_raised(token_server, api, tmp_path, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    auth = make_oauth(token_server)
    server = make_server(f"http://127.0.0.1:{api.server_port}", auth, tmp_path)
    api.reject_token = "Bearer T-OK"  # every issued token is rejected
    response = call(server, "list_pets", {}, 2)
    # one self-heal attempt, then the 401 is returned as an honest error
    assert len(token_server.requests) == 2
    assert response["result"]["isError"] is True


def test_describe_shape(token_server, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.delenv("OAUTH2_SECRET", raising=False)
    auth = make_oauth(token_server)
    report = auth.describe()
    assert report["style"] == "oauth2-client-credentials"
    assert report["client_id_env"] == "OAUTH2_ID"
    assert report["client_id_env_set"] is True
    assert report["client_secret_env"] == "OAUTH2_SECRET"
    assert report["client_secret_env_set"] is False
    assert report["token_url"].endswith("/token")


def test_health_tool_reports_oauth2(token_server, tmp_path, monkeypatch):
    monkeypatch.setenv("OAUTH2_ID", "client-a")
    monkeypatch.setenv("OAUTH2_SECRET", "shhh")
    auth = make_oauth(token_server)
    server = ApiServer(load_spec("examples/petstore.json"), "http://127.0.0.1:1", auth=auth)
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    response = call(server, "mcpify_health", {}, 2)
    report = json.loads(response["result"]["content"][0]["text"])
    assert report["auth"]["style"] == "oauth2-client-credentials"
    assert report["auth"]["client_id_env_set"] is True
