"""A redirect loop must end as a readable result, not a dead server.

urllib recurses once per hop to follow a chain; a URL that redirects to
itself raises ``RecursionError``, which is not an ``OSError``, so before
this it escaped every handler in ``_execute_once`` and killed whatever was
serving (stdio session or HTTP worker) with a traceback the caller could
not see. The hop budget keeps the loop finite and reports it as an HTTP
result, so the model gets a category and a remediation tip.
"""

import http.server
import threading

import pytest

from mcpify.http_client import MAX_REDIRECTS, execute


class _Redirecting(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/loop"):
            self._redirect("/loop")            # redirects to itself forever
        elif self.path.startswith("/chain/"):
            hop = int(self.path.rsplit("/", 1)[1])
            if hop > 0:
                self._redirect(f"/chain/{hop - 1}")
            else:
                body = b'{"done": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture()
def redirecting():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Redirecting)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(url):
    return execute({"method": "GET", "url": url, "headers": {}, "body": None}, timeout=10)


def test_redirect_loop_returns_a_result_instead_of_raising(redirecting):
    result = _get(f"{redirecting}/loop")
    assert result["status"] in (302, 0) or result["status"] >= 300
    assert "redirect" in result["body"].lower()


def test_redirect_loop_message_is_actionable(redirecting):
    result = _get(f"{redirecting}/loop")
    assert "could not be followed" in result["body"]
    assert "check the spec's server URL" in result["body"]
    # the hop budget is what made this finite; a loop that urllib's own
    # detector catches first (5 repeats) never reaches our ceiling
    assert MAX_REDIRECTS >= 5


def test_short_chains_still_succeed(redirecting):
    result = _get(f"{redirecting}/chain/3")
    assert result["status"] == 200
    assert result["json"] == {"done": True}


def test_hop_budget_resets_between_calls(redirecting):
    """A spent budget must not leak into the next request."""
    _get(f"{redirecting}/loop")
    assert _get(f"{redirecting}/chain/2")["status"] == 200
    assert _get(f"{redirecting}/chain/2")["status"] == 200


def test_hop_budget_is_per_thread(redirecting):
    """Concurrent callers each get their own budget (ContextVar, not global)."""
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(_get(f"{redirecting}/chain/4")))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert [r["status"] for r in results] == [200, 200, 200, 200]


# ---------------------------------------------------------------------------
# What the model sees for a redirect it cannot follow
# ---------------------------------------------------------------------------

def test_unfollowable_redirect_is_an_error_result_not_a_quiet_success(redirecting):
    """Before this, `HTTP 302 — success, no response body` was the answer."""
    from mcpify.api_server import ApiServer

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "r", "version": "1"},
        "servers": [{"url": redirecting}],
        "paths": {"/loop": {"get": {"operationId": "loop", "responses": {"200": {"description": "ok"}}}}},
    }
    server = ApiServer(spec, redirecting)
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    payload = server.run_tool("loop", {})
    assert payload.get("isError") is True
    text = payload["content"][0]["text"]
    assert text.startswith("retryable:")  # the taxonomy leads, then the detail
    assert "HTTP 302" in text
    assert "could not be followed" in text


def test_304_is_not_treated_as_a_broken_redirect():
    from mcpify.http_client import is_failure

    assert is_failure(304) is False
    assert is_failure(302) is True
    assert is_failure(200) is False
