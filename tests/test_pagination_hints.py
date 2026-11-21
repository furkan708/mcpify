"""Pagination hints: an actionable "there is more" note, never an auto-crawl.

The failure this targets is an agent that read page one, reported
"there are 20 items", and moved on — the API said ``Link: rel="next"`` and
nobody told the model. mcpify still makes exactly ONE request per tool
call; the hint only reports what the upstream already said, translated
into arguments this tool actually declares.
"""

import http.server
import json
import threading

import pytest

from mcpify.api_server import ApiServer
from mcpify.http_client import (
    next_page_url,
    page_argument,
    pagination_hint,
)

TOOL = {
    "name": "list_items",
    "_meta": {"parameters": [{"name": "cursor", "in": "query"}, {"name": "limit", "in": "query"}]},
}


@pytest.fixture()
def pager():
    """An API that pages with three different conventions, one per path."""

    class Handler(http.server.BaseHTTPRequestHandler):
        seen: list[str] = []

        def log_message(self, *args):
            pass

        def do_GET(self):
            Handler.seen.append(self.path)
            if self.path.startswith("/link"):
                body = json.dumps([{"id": 1}, {"id": 2}]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Link", '</link?page=2>; rel="next", </link?page=1>; rel="prev"')
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/cursor"):
                body = json.dumps({"items": [{"id": 1}], "next_cursor": "tok-2"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/hostile"):
                body = json.dumps({"items": [], "next": "javascript:alert(document.cookie)"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                body = json.dumps([{"id": 1}]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    Handler.seen = []
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", Handler.seen
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# the four conventions
# ---------------------------------------------------------------------------

def test_link_header_next_is_understood():
    headers = {"Link": '<https://api.example/items?page=3>; rel="next", <https://api.example/items?page=1>; rel="prev"'}
    assert next_page_url(headers, "", "https://api.example/items?page=2") == (
        "https://api.example/items?page=3"
    )


def test_link_header_is_case_insensitive_and_tolerates_spacing():
    headers = {"link": ' <https://api.example/items?page=3> ; rel="next" '}
    assert next_page_url(headers, "", "https://api.example/items") == (
        "https://api.example/items?page=3"
    )


def test_relative_next_url_is_resolved():
    headers = {"Link": '</items?page=2>; rel="next"'}
    assert next_page_url(headers, "", "https://api.example/items?page=1") == (
        "https://api.example/items?page=2"
    )


def test_next_url_field_in_body():
    body = json.dumps({"items": [], "next": "https://api.example/items?page=2"})
    assert next_page_url({}, body, "https://api.example/items?page=1") == (
        "https://api.example/items?page=2"
    )


def test_next_cursor_is_mapped_onto_a_declared_parameter():
    body = json.dumps({"items": [], "next_cursor": "tok-2"})
    url = next_page_url({}, body, "https://api.example/items?limit=10", frozenset({"cursor", "limit"}))
    assert url == "https://api.example/items?limit=10&cursor=tok-2"


def test_cursor_without_a_declared_parameter_is_not_a_hint():
    body = json.dumps({"items": [], "next_cursor": "tok-2"})
    assert next_page_url({}, body, "https://api.example/items?limit=10") is None


# ---------------------------------------------------------------------------
# hostile / useless next values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"next": "javascript:alert(document.cookie)"}),
        json.dumps({"next": "data:text/html,<script>x</script>"}),
        json.dumps({"next": "https://user:secret@api.example/items?page=2"}),
        json.dumps({"next": "not a url"}),
    ],
)
def test_hostile_next_values_never_become_a_hint(body):
    assert next_page_url({}, body, "https://api.example/items") is None


def test_credential_bearing_link_header_is_refused():
    headers = {"Link": '<https://user:pw@api.example/items?page=2>; rel="next"'}
    assert next_page_url(headers, "", "https://api.example/items") is None


def test_javascript_cursor_token_is_refused():
    body = json.dumps({"next_cursor": "javascript:alert(1)"})
    assert next_page_url({}, body, "https://api.example/x", frozenset({"next_cursor"})) is None


def test_non_200_has_no_hint():
    result = {"status": 500, "headers": {"Link": '<https://x/y>; rel="next"'}, "body": ""}
    assert pagination_hint(TOOL, result, "https://x/y") == ""


def test_no_signal_no_hint():
    result = {"status": 200, "headers": {}, "body": json.dumps([{"id": 1}])}
    assert pagination_hint(TOOL, result, "https://x/y") == ""


# ---------------------------------------------------------------------------
# the argument translation
# ---------------------------------------------------------------------------

def test_page_argument_only_passes_declared_query_parameters():
    arguments = page_argument(TOOL, "https://api.example/items?cursor=abc&limit=10&secret=x")
    assert arguments == {"cursor": "abc", "limit": "10"}


def test_page_argument_is_none_when_nothing_matches():
    assert page_argument({"name": "t", "_meta": {"parameters": []}}, "https://x/y?page=2") is None


def test_hint_names_the_tool_and_the_arguments():
    result = {
        "status": 200,
        "headers": {"Link": '<https://api.example/items?cursor=abc&limit=10>; rel="next"'},
        "body": "{}",
    }
    hint = pagination_hint(TOOL, result, "https://api.example/items?limit=10")
    assert hint.startswith("[pagination]")
    assert "list_items" in hint
    assert '"cursor": "abc"' in hint
    assert "nothing was fetched automatically" in hint


def test_different_path_hint_admits_it_cannot_be_called_directly():
    result = {
        "status": 200,
        "headers": {"Link": '<https://api.example/other?cursor=abc>; rel="next"'},
        "body": "{}",
    }
    hint = pagination_hint({"name": "t", "_meta": {"parameters": []}}, result, "https://api.example/items")
    assert "different path" in hint


# ---------------------------------------------------------------------------
# end to end through the server
# ---------------------------------------------------------------------------

def _server(base, path, *, hints=True):
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "paged", "version": "1"},
        "servers": [{"url": base}],
        "paths": {
            path: {
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
    server = ApiServer(spec, base, paginate_hints=hints)
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def test_end_to_end_cursor_hint(pager):
    base, _seen = pager
    payload = _server(base, "/cursor").run_tool("list_items", {"limit": 5, "cursor": "tok-1"})
    text = payload["content"][0]["text"]
    assert "[pagination] more results available" in text
    assert '"cursor": "tok-2"' in text


def test_end_to_end_one_request_only(pager):
    """The hint is advice, not a crawl: exactly one upstream request."""
    base, seen = pager
    _server(base, "/cursor").run_tool("list_items", {"limit": 5, "cursor": "tok-1"})
    assert len(seen) == 1
    assert seen[0].startswith("/cursor?")
    assert "cursor=tok-1" in seen[0]


def test_hints_can_be_turned_off(pager):
    base, _seen = pager
    payload = _server(base, "/cursor", hints=False).run_tool("list_items", {"limit": 5})
    assert "[pagination]" not in payload["content"][0]["text"]


def test_hostile_signal_does_not_become_an_instruction(pager):
    """The body is echoed as data; the HINT never quotes a script URL.

    The upstream's own field is the API talking, and dropping it would hide
    data. What must not happen is mcpify turning it into advice: no hint, no
    "call this next", nothing clickable.
    """
    base, _seen = pager
    payload = _server(base, "/hostile").run_tool("list_items", {})
    text = payload["content"][0]["text"]
    assert "[pagination]" not in text
    assert "next page" not in text


def test_structured_content_is_untouched_by_the_hint(pager):
    base, _seen = pager
    server = _server(base, "/cursor")
    server.by_name["list_items"]["outputSchema"] = {"type": "object"}
    payload = server.run_tool("list_items", {"limit": 5})
    assert payload["structuredContent"] == {"items": [{"id": 1}], "next_cursor": "tok-2"}
    assert "[pagination]" in payload["content"][0]["text"]


def test_hint_starts_on_its_own_line_after_compact_json(pager):
    """A body without a trailing newline used to run into the note.

    Found by installing the published wheel and reading the tool result:
    ``…"tok-2"}[pagination] more results…`` — a broken token boundary.
    """
    base, _seen = pager
    text = _server(base, "/cursor").run_tool("list_items", {"limit": 5})["content"][0]["text"]
    assert '}[pagination]' not in text
    hint_lines = [line for line in text.splitlines() if line.startswith("[pagination]")]
    assert len(hint_lines) == 1
