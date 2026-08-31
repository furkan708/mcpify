"""Agent-grade surface: annotations, structured output, remediation, lazy mode, preview.

Each feature is pinned at the protocol level (handle_message is the same
code path the stdio loop serves) against a real local HTTP API.
"""

import http.server
import json
import threading

import pytest

from mcpify.api_server import (
    CALL_TOOL,
    HEALTH_TOOL,
    PREVIEW_TOOL,
    SCHEMA_TOOL,
    SEARCH_TOOL,
    ApiServer,
)
from mcpify.tools import AuthConfig

# ---------------------------------------------------------------------------
# a fake dog API engineered to exercise every surface
# ---------------------------------------------------------------------------

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Dogs", "version": "1.0"},
    "servers": [{"url": "http://placeholder"}],
    "paths": {
        "/dogs/{dogId}": {
            "get": {
                "operationId": "get_dog",
                "summary": "Get one dog",
                "parameters": [{"name": "dogId", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                            "required": ["id", "name"],
                        }}},
                    }
                },
            },
            "delete": {
                "operationId": "delete_dog",
                "summary": "Remove a dog",
                "parameters": [{"name": "dogId", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"204": {"description": "gone"}},
            },
            "put": {
                "operationId": "replace_dog",
                "summary": "Replace a dog",
                "parameters": [{"name": "dogId", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "ok"}},
            },
        },
        "/dogs": {
            "get": {
                "operationId": "list_dogs",
                "summary": "List all the dogs in the kennel",
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                    "type": "array", "items": {"type": "object"}}}}}},
            },
            "post": {
                "operationId": "create_dog",
                "summary": "Create a dog",
                "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"201": {"description": "created"}},
            },
        },
        "/dogs/search": {
            "get": {
                "operationId": "search_dogs",
                "summary": "Search dogs by kind",
                "parameters": [{"name": "kind", "in": "query", "schema": {"type": "string", "enum": ["cat", "dog"]}}],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/broken-json": {
            "get": {
                "operationId": "broken_json",
                "summary": "Promises JSON, returns garbage",
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"type": "object"}}}}},
            }
        },
        "/circular-response": {
            "get": {
                "operationId": "circular_response",
                "summary": "Documented with a circular response schema",
                "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}},
            }
        },
        "/validate": {
            "get": {
                "operationId": "validate_me",
                "summary": "Validated endpoint",
                "responses": {"422": {"description": "validation"}},
            }
        },
        "/secret": {"get": {"operationId": "secret", "summary": "Needs auth", "responses": {"401": {"description": "nope"}}}},
        "/limited": {"get": {"operationId": "limited", "summary": "Rate limited", "responses": {"429": {"description": "slow down"}}}},
        "/boom": {"get": {"operationId": "boom", "summary": "Explodes", "responses": {"500": {"description": "dead"}}}},
        "/mcpify_call_tool": {"get": {"operationId": "mcpify_call_tool", "summary": "Names collide with meta tools", "responses": {"200": {"description": "ok"}}}},
    },
    "components": {"schemas": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/components/schemas/Node"}}}}},
}


class DogAPI(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self):
        DogAPI.seen.append({"method": self.command, "path": self.path})

    def do_GET(self):
        self._record()
        if self.path.startswith("/dogs/search"):
            self._send_json(200, [{"name": "Rex", "kind": "dog"}])
        elif self.path.startswith("/dogs/"):
            self._send_json(200, {"id": int(self.path.rsplit("/", 1)[1]), "name": "Rex"})
        elif self.path == "/dogs":
            self._send_json(200, [{"name": "Rex"}])
        elif self.path == "/broken-json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"<html>this is not json</html>")
        elif self.path == "/circular-response":
            self._send_json(200, {"child": {"child": None}})
        elif self.path == "/validate":
            self._send_json(422, {"detail": [
                {"loc": ["query", "kind"], "msg": "value is not a valid enumeration", "type": "enum"}]})
        elif self.path == "/secret":
            self._send_json(401, {"message": "missing or invalid credentials"})
        elif self.path == "/limited":
            self._send_json(429, {"message": "slow down"}, extra_headers={"Retry-After": "7"})
        elif self.path == "/boom":
            self._send_json(500, {"message": "upstream melted"})
        elif self.path == "/mcpify_call_tool":
            self._send_json(200, {"collided": True})
        else:
            self._send_json(404, {"message": "nope"})

    def do_DELETE(self):
        self._record()
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        self._record()
        self._send_json(201, {"created": True})


@pytest.fixture()
def base():
    DogAPI.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), DogAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def make(base, **kwargs):
    server = ApiServer(SPEC, base, **kwargs)
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def rpc(method, params=None, request_id=2):
    return {"jsonrpc": "2.0", "id": request_id, "method": "method", "params": params or {}} | {
        "method": method
    }


def call(server, name, arguments=None, request_id=2):
    return server.handle_message(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    )


def listing(server):
    result = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]
    return {tool["name"]: tool for tool in result["tools"]}


# ---------------------------------------------------------------------------
# 1. annotations: HTTP semantics -> approval hints
# ---------------------------------------------------------------------------

def test_get_tool_is_annotated_read_only(base):
    tool = listing(make(base))["get_dog"]
    ann = tool["annotations"]
    assert ann["readOnlyHint"] is True
    assert ann["destructiveHint"] is False
    assert ann["idempotentHint"] is True
    assert ann["openWorldHint"] is True
    assert ann["title"] == "Get one dog"


def test_delete_is_destructive_and_put_is_idempotent(base):
    tools = listing(make(base))
    assert tools["delete_dog"]["annotations"]["destructiveHint"] is True
    assert tools["replace_dog"]["annotations"]["idempotentHint"] is True


def test_post_makes_no_idempotence_claim(base):
    assert listing(make(base))["create_dog"]["annotations"]["idempotentHint"] is False


def test_read_only_mode_only_lists_read_only_annotations(base):
    from mcpify.tools import spec_to_tools

    tools = spec_to_tools(SPEC)
    tools = [t for t in tools if t["_meta"]["method"] == "GET"]
    server = ApiServer(SPEC, base, tools=tools)
    server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    for tool in listing(server).values():
        assert tool["annotations"]["readOnlyHint"] is True


# ---------------------------------------------------------------------------
# 2. structured output: outputSchema promise, structuredContent delivery
# ---------------------------------------------------------------------------

def test_output_schema_declared_from_2xx_json_response(base):
    tool = listing(make(base))["get_dog"]
    assert tool["outputSchema"]["type"] == "object"
    assert "id" in tool["outputSchema"]["properties"]


def test_tools_without_json_response_declare_nothing(base):
    tools = listing(make(base))
    assert "outputSchema" not in tools["delete_dog"]  # 204, no JSON body
    assert "outputSchema" not in tools["limited"]  # only a 4xx documented


def test_successful_call_returns_structured_content_plus_text(base):
    response = call(make(base), "get_dog", {"dogId": 7})
    result = response["result"]
    assert result["structuredContent"] == {"id": 7, "name": "Rex"}
    assert json.loads(result["content"][0]["text"]) == {"id": 7, "name": "Rex"}


def test_non_json_body_against_declared_schema_is_tool_error(base):
    response = call(make(base), "broken_json")
    result = response["result"]
    assert result["isError"] is True
    assert "non-JSON" in result["content"][0]["text"]
    assert "structuredContent" not in result


def test_circular_response_schema_degrades_to_no_declaration(base):
    tools = listing(make(base))
    assert "outputSchema" not in tools["circular_response"]
    response = call(make(base), "circular_response")
    assert "structuredContent" not in response["result"]


def test_error_results_carry_taxonomy_not_output_data_even_with_output_schema(base):
    # Since the error-taxonomy release, error results DO carry
    # structuredContent — the machine-readable category (the 2025-06-18 spec
    # exempts isError results from output validation, so this is safe). What
    # must NOT happen is schema-shaped DATA posing as a successful output.
    dead = ApiServer(SPEC, "http://127.0.0.1:1")
    dead.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    dead.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    response = call(dead, "get_dog", {"dogId": 1})
    result = response["result"]
    assert result["isError"] is True
    assert "Connection failed" in result["content"][0]["text"]
    taxonomy = result["structuredContent"]
    assert taxonomy["error_category"] == "retryable"
    assert taxonomy["retryable"] is True
    assert taxonomy["http_status"] == 0
    # and the taxonomy object is not the tool's declared output schema shape
    declared = next((t for t in dead.public_tools() if t["name"] == "get_dog"), {}).get("outputSchema")
    if declared:
        assert set(taxonomy) != set(declared.get("properties", {}))


# ---------------------------------------------------------------------------
# 3. remediation: errors that teach the next call
# ---------------------------------------------------------------------------

def test_422_fastapi_detail_becomes_validation_line(base):
    text = call(make(base), "validate_me")["result"]["content"][0]["text"]
    assert "API validation:" in text
    assert "query.kind" in text
    assert "value is not a valid enumeration" in text


def test_401_points_at_auth_flags(base):
    text = call(make(base), "secret")["result"]["content"][0]["text"]
    assert "--auth-env" in text
    assert "missing or invalid credentials" in text


def test_429_carries_retry_after_and_no_retry_policy(base):
    text = call(make(base), "limited")["result"]["content"][0]["text"]
    assert "7s" in text
    assert "never retries automatically" in text


def test_5xx_blames_upstream_not_mcpify(base):
    text = call(make(base), "boom")["result"]["content"][0]["text"]
    assert "upstream API failed" in text
    assert "not an mcpify error" in text


def test_plain_error_without_advice_gets_no_remediation_block(base):
    from mcpify.http_client import remediation

    result = {"status": 409, "body": "{}", "json": {}, "headers": {}}
    assert remediation(result, None, None) == ""


def test_remediation_surfaces_structured_api_error_messages():
    """API'nin {"errors":[{"message":...}]} formati ipucuna tasinir."""
    from mcpify.http_client import remediation

    result = {
        "status": 422, "headers": {},
        "json": {"errors": [{"message": "currency must be USD"}]},
        "body": '{"errors":[{"message":"currency must be USD"}]}',
    }
    text = remediation(result, None, None)
    assert "currency must be USD" in text


def test_remediation_404_suggests_closest_paths_directly(base):
    from mcpify.http_client import remediation

    result = {"status": 404, "body": "{}", "json": {}, "headers": {}}
    tool = {"_meta": {"path": "/dogs/{dogId}x"}}
    text = remediation(result, tool, ["/dogs/{dogId}", "/dogs", "/secret"])
    assert "Closest known paths" in text
    assert "/dogs/{dogId}" in text


# ---------------------------------------------------------------------------
# 4. lazy mode: search -> schema -> call
# ---------------------------------------------------------------------------

def test_lazy_listing_is_three_core_meta_tools_plus_health(base):
    names = set(listing(make(base, lazy=True)))
    assert names == {SEARCH_TOOL, SCHEMA_TOOL, CALL_TOOL, HEALTH_TOOL}


def test_preview_absent_without_flag_and_present_with_it(base):
    assert PREVIEW_TOOL not in listing(make(base))
    assert PREVIEW_TOOL in listing(make(base, enable_preview=True))
    assert PREVIEW_TOOL in listing(make(base, lazy=True, enable_preview=True))


def test_lazy_search_finds_by_keyword_with_compact_entries(base):
    server = make(base, lazy=True)
    response = call(server, SEARCH_TOOL, {"query": "dog kennel"})
    text = response["result"]["content"][0]["text"]
    first_line, payload = text.split("\n", 1)
    entries = json.loads(payload)
    assert entries, first_line
    entry = next(e for e in entries if e["name"] == "list_dogs")
    assert entry["method"] == "GET" and entry["path"] == "/dogs"
    assert entry["readOnly"] is True
    assert "inputSchema" not in entry  # compact: no schemas in search results


def test_lazy_search_tag_filter_and_limit(base):
    server = make(base, lazy=True)
    response = call(server, SEARCH_TOOL, {"limit": 2})
    text = response["result"]["content"][0]["text"]
    entries = json.loads(text.split("\n", 1)[1])
    assert len(entries) <= 2
    response = call(server, SEARCH_TOOL, {"query": "replace"}, request_id=9)
    assert "replace_dog" in response["result"]["content"][0]["text"]


def test_lazy_bad_limit_is_tool_error(base):
    response = call(make(base, lazy=True), SEARCH_TOOL, {"limit": "many"})
    assert response["result"]["isError"] is True


def test_lazy_get_schema_returns_full_definition(base):
    server = make(base, lazy=True)
    response = call(server, SCHEMA_TOOL, {"name": "get_dog"})
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["inputSchema"]["properties"]["dogId"]["type"] == "integer"
    assert payload["annotations"]["readOnlyHint"] is True


def test_lazy_get_schema_unknown_name_suggests_alternatives(base):
    response = call(make(base, lazy=True), SCHEMA_TOOL, {"name": "get_dogz"})
    text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is True
    assert "Did you mean" in text and "get_dog" in text


def test_lazy_call_executes_the_real_request(base):
    server = make(base, lazy=True)
    response = call(server, CALL_TOOL, {"name": "get_dog", "arguments": {"dogId": 5}})
    assert response["result"]["structuredContent"] == {"id": 5, "name": "Rex"}
    assert any(entry["path"].endswith("/5") for entry in DogAPI.seen)


def test_lazy_call_inner_errors_surface_with_remediation(base):
    server = make(base, lazy=True)
    response = call(server, CALL_TOOL, {"name": "limited"})
    assert response["result"]["isError"] is True
    assert "never retries" in response["result"]["content"][0]["text"]


def test_lazy_direct_call_of_unlisted_tool_is_rejected_with_guidance(base):
    server = make(base, lazy=True)
    response = call(server, "get_dog", {"dogId": 1})
    text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is True
    assert CALL_TOOL in text


def test_meta_tools_cannot_nest(base):
    server = make(base, lazy=True)
    response = call(server, CALL_TOOL, {"name": SEARCH_TOOL})
    assert response["result"]["isError"] is True


def test_meta_names_never_collide_with_spec_operations(base):
    # direct mode: the spec's own mcpify_call_tool operation yields to the
    # reserved meta name and takes the usual _2 suffix — still listed, callable
    direct = make(base)
    assert "mcpify_call_tool_2" in listing(direct)
    response = call(direct, "mcpify_call_tool_2")
    assert "collided" in response["result"]["content"][0]["text"]

    # lazy mode: meta tools listed, renamed op absent but reachable via meta
    lazy = make(base, lazy=True)
    lazy_names = listing(lazy)
    assert CALL_TOOL in lazy_names and "mcpify_call_tool_2" not in lazy_names
    response = call(lazy, CALL_TOOL, {"name": "mcpify_call_tool_2"})
    assert "collided" in response["result"]["content"][0]["text"]
    response = call(lazy, CALL_TOOL, {"name": "get_dog", "arguments": {"dogId": 2}}, request_id=8)
    assert response["result"]["structuredContent"] == {"id": 2, "name": "Rex"}


# ---------------------------------------------------------------------------
# 5. preview: auditable dry runs, masked credentials
# ---------------------------------------------------------------------------

def test_preview_shows_request_and_masks_bearer(base, monkeypatch):
    monkeypatch.setenv("DOGS_TOKEN", "super-secret-value")
    auth = AuthConfig("DOGS_TOKEN", "bearer")
    server = make(base, enable_preview=True, auth=auth)
    response = call(server, PREVIEW_TOOL, {"name": "get_dog", "arguments": {"dogId": 4}})
    text = response["result"]["content"][0]["text"]
    assert text.startswith("GET ")
    assert "/dogs/4" in text
    assert "Bearer ***" in text
    assert "super-secret-value" not in text
    assert "dry run" in text
    assert DogAPI.seen == []  # nothing was sent


def test_preview_masks_header_and_query_credentials(base, monkeypatch):
    monkeypatch.setenv("DOGS_TOKEN", "top-secret-token")
    header_auth = AuthConfig("DOGS_TOKEN", "header", "X-Key")
    server = make(base, enable_preview=True, auth=header_auth)
    text = call(server, PREVIEW_TOOL, {"name": "get_dog", "arguments": {"dogId": 1}})["result"]["content"][0]["text"]
    assert "X-Key: ***" in text and "top-secret-token" not in text

    query_auth = AuthConfig("DOGS_TOKEN", "query", "api_key")
    server = make(base, enable_preview=True, auth=query_auth)
    text = call(server, PREVIEW_TOOL, {"name": "get_dog", "arguments": {"dogId": 1}}, request_id=6)["result"]["content"][0]["text"]
    assert "api_key=***" in text and "top-secret-token" not in text


def test_preview_respects_truncation_for_huge_bodies(base):
    server = make(base, enable_preview=True)
    big = {"body": {"blob": "x" * 5000}}
    response = call(server, PREVIEW_TOOL, {"name": "create_dog", "arguments": big})
    assert "truncated" in response["result"]["content"][0]["text"]
