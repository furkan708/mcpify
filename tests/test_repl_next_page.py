"""`:next` in the REPL — follow the pagination hint without retyping.

The hint mcpify emits is already the instruction the model would follow;
the REPL applies exactly that line, so a human paging through results and
an agent doing the same cannot diverge.
"""

import http.server
import io
import json
import threading

import pytest

from mcpify.api_server import ApiServer
from mcpify.repl import _page_arguments, run


class Paged(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Paged.seen.append(self.path)
        cursor = "tok-2" if "cursor=" not in self.path else None
        body = json.dumps({"items": [{"id": 1}], "next_cursor": cursor} if cursor
                          else {"items": [{"id": 2}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def paged_server():
    Paged.seen = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Paged)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "paged", "version": "1"},
        "servers": [{"url": base}],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "list_items",
                    "summary": "List items",
                    "parameters": [
                        {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    api = ApiServer(spec, base)
    api.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    api.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    try:
        yield api, Paged.seen
    finally:
        httpd.shutdown()
        httpd.server_close()


def drive(api, lines):
    queue = list(lines)

    def read(_prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)

    out = io.StringIO()
    run(api, input_fn=read, output=out)
    return out.getvalue()


def test_next_is_advertised_in_help(paged_server):
    api, _seen = paged_server
    assert ":next" in drive(api, [":h", ":q"])


def test_next_without_a_call_says_so(paged_server):
    api, seen = paged_server
    out = drive(api, [":next", ":q"])
    assert "nothing to follow" in out
    assert seen == []  # nothing was fetched


def test_next_follows_the_cursor(paged_server):
    api, seen = paged_server
    out = drive(api, [':raw list_items {"limit": 5}', ":next", ":q"])
    assert "[pagination]" in out
    assert len(seen) == 2
    assert "cursor=tok-2" in seen[1]
    assert "limit=5" in seen[1]  # the human's own limit rides along


def test_next_stops_when_the_pages_run_out(paged_server):
    api, seen = paged_server
    out = drive(api, [':raw list_items {"limit": 5}', ":next", ":next", ":q"])
    # third call: the server sent no cursor, so :next has nothing to follow
    assert len(seen) == 2
    assert "nothing to follow" in out


def test_page_arguments_reads_the_hint_it_emitted():
    payload = {"content": [{"type": "text", "text":
        'body\n[pagination] more results available — call list_items with '
        '{"cursor": "tok-9", "limit": "5"} for the next page'}]}
    # hint values are strings (they came off a URL); they override the
    # previous call's values for the same keys
    assert _page_arguments(payload, {"limit": 5}) == {"limit": "5", "cursor": "tok-9"}


def test_page_arguments_ignores_results_without_a_hint():
    payload = {"content": [{"type": "text", "text": "[]"}]}
    assert _page_arguments(payload, {"limit": 5}) is None
