"""Regression tests for redirects, cache isolation and XML parsing."""

import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcpify.convert import convert
from mcpify.http_client import (
    ResponseCache,
    _CrossHostSanitizingRedirectHandler,
    _retry_after_seconds,
    execute,
)


@pytest.mark.parametrize("destination", ["https://other.example/path", "http://api.example/path"])
def test_cross_origin_redirect_strips_custom_headers(destination):
    request = urllib.request.Request(
        "https://api.example/start",
        headers={"Authorization": "Bearer secret", "X-Custom-Token": "secret", "Accept": "application/json"},
    )
    redirected = _CrossHostSanitizingRedirectHandler().redirect_request(
        request, None, 302, "Found", {}, destination
    )
    assert redirected is not None
    assert redirected.get_header("Authorization") is None
    assert redirected.get_header("X-custom-token") is None
    assert redirected.get_header("Accept") is None
    assert request.get_header("X-custom-token") == "secret"


def test_same_origin_redirect_keeps_custom_headers():
    request = urllib.request.Request("https://api.example/start", headers={"X-Custom-Token": "secret"})
    redirected = _CrossHostSanitizingRedirectHandler().redirect_request(
        request, None, 302, "Found", {}, "https://api.example/next"
    )
    assert redirected is not None
    assert redirected.get_header("X-custom-token") == "secret"


@pytest.fixture
def caching_server():
    class Handler(BaseHTTPRequestHandler):
        requests = []

        def do_GET(self):
            token = self.headers.get("X-Tenant", "")
            self.requests.append((token, self.headers.get("If-None-Match")))
            if self.headers.get("If-None-Match") == f'"{token}"':
                self.send_response(304)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("ETag", f'"{token}"')
            self.end_headers()
            self.wfile.write(token.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/data", Handler.requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_cache_separates_request_headers(caching_server):
    url, requests = caching_server
    cache = ResponseCache(60)
    def call(tenant):
        return execute({"method": "GET", "url": url, "headers": {"X-Tenant": tenant}, "body": None}, cache=cache)
    assert call("one")["body"] == "one"
    assert call("two")["body"] == "two"
    assert call("one")["body"] == "one"
    assert len(requests) == 2
    assert cache.invalidate(url) == 2


def test_expired_cache_entry_is_revalidated(caching_server):
    url, requests = caching_server
    cache = ResponseCache(0)
    request = {"method": "GET", "url": url, "headers": {"X-Tenant": "one"}, "body": None}
    assert execute(request, cache=cache)["body"] == "one"
    assert execute(request, cache=cache)["body"] == "one"
    assert requests == [("one", None), ("one", '"one"')]


def test_retry_after_header_is_case_insensitive():
    assert _retry_after_seconds({"headers": {"retry-after": "3"}}, 1) == 3


@pytest.mark.parametrize("fmt", ["auto", "xml"])
def test_xml_dtd_after_long_prolog_is_never_parsed(fmt):
    body = "<?xml version='1.0'?>" + " " * 2100 + '<!DOCTYPE root [<!ENTITY e "expanded">]><root>&e;</root>'
    text, structured = convert(body, None, "application/xml", fmt)
    assert structured is None
    assert text.startswith(body)
