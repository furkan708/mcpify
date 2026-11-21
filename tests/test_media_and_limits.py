"""What reaches the model when the response body lies about itself.

Three shapes that used to slip through the classifier silently:

- **A binary body wearing a JSON label.** ``200 image/png`` on a tool that
  declares ``outputSchema`` returned the "contents not shown" notice with
  no error flag: the model saw a success with no ``structuredContent`` and
  had no way to tell that the promise was broken.
- **A header spelled oddly.** ``Text/HTML`` or ``IMAGE/PNG`` did not match
  the lowercase comparisons, so an HTML login page and a PNG were both
  classified as "not HTML, not binary" and dumped as text.
- **An empty success.** ``204`` or ``200`` with nothing in it must say so
  instead of leaving a blank tool result.

The classifier itself is inspected directly (no sockets): these are
string-comparison bugs, and a unit test names the rule that broke.
"""

import pytest

from mcpify.api_server import ApiServer
from mcpify.http_client import media_type_of

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "media", "version": "1"},
    "servers": [{"url": "http://upstream.invalid"}],
    "paths": {
        "/typed": {
            "get": {
                "operationId": "typed",
                "summary": "Declares a JSON response schema",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
        "/untyped": {
            "get": {
                "operationId": "untyped",
                "summary": "Declares no response schema",
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def _server():
    server = ApiServer(SPEC, "http://upstream.invalid")
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def _payload(tool, status=200, content_type="application/json", body="", parsed=None):
    server = _server()
    found = {item["name"]: item for item in server.tools}[tool]
    result = {
        "status": status,
        "body": body,
        "json": parsed,
        "headers": {"Content-Type": content_type} if content_type else {},
    }
    return server._payload_for(found, result)


def _text(payload):
    return payload["content"][0]["text"]


# ---------------------------------------------------------------------------
# media-type classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("application/json", "application/json"),
        ("Application/JSON; Charset=UTF-8", "application/json"),
        ("  TEXT/HTML ; charset=utf-8", "text/html"),
        ("IMAGE/PNG", "image/png"),
        ("application/problem+json", "application/problem+json"),
        ("", ""),
        (None, ""),
    ],
)
def test_media_type_is_normalized(raw, expected):
    assert media_type_of(raw) == expected


# ---------------------------------------------------------------------------
# a declared outputSchema is a promise
# ---------------------------------------------------------------------------

def test_binary_body_on_declared_schema_is_an_error():
    """A "contents not shown" notice is not a structured response.

    Before the fix this returned is_error=False with no structuredContent:
    a success the model could not distinguish from a quiet failure.
    """
    payload = _payload("typed", content_type="image/png", body="\x89PNG\r\n\x1a\n")
    assert payload.get("isError") is True
    assert "non-JSON body" in _text(payload)
    assert "structuredContent" not in payload


def test_binary_body_without_declared_schema_is_still_named_not_dumped():
    payload = _payload("untyped", content_type="image/png", body="\x89PNG\r\n\x1a\n")
    assert not payload.get("isError")
    assert "[binary response: content-type image/png" in _text(payload)


def test_text_body_on_declared_schema_is_an_error():
    payload = _payload("typed", content_type="text/plain", body="plain text, not JSON")
    assert payload.get("isError") is True
    assert "non-JSON" in _text(payload)


def test_mixed_case_json_body_keeps_the_promise():
    payload = _payload(
        "typed",
        content_type="Application/Problem+JSON; charset=utf-8",
        body='{"type": "about:blank"}',
        parsed={"type": "about:blank"},
    )
    assert not payload.get("isError")
    assert payload["structuredContent"] == {"type": "about:blank"}


def test_mixed_case_json_is_not_named_binary():
    payload = _payload(
        "untyped",
        content_type="APPLICATION/VND.API+JSON",
        body='{"a": 1}',
        parsed={"a": 1},
    )
    text = _text(payload)
    assert "[binary response" not in text
    assert '"a"' in text


def test_mixed_case_html_is_still_flagged_as_html():
    payload = _payload("untyped", content_type="Text/HTML; charset=utf-8", body="<html>login</html>")
    assert "Response is HTML" in _text(payload)


# ---------------------------------------------------------------------------
# empty successes are not silence
# ---------------------------------------------------------------------------

def test_204_says_no_body():
    payload = _payload("untyped", status=204, content_type=None, body="", parsed=None)
    assert "no response body" in _text(payload)


def test_empty_non_text_200_says_no_body():
    payload = _payload("untyped", content_type="application/json", body="   ", parsed=None)
    assert "no response body" in _text(payload)
