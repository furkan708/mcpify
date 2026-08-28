"""Tests for operation -> tool translation and request building."""

import json

import pytest

from mcpify.spec import load_spec
from mcpify.tools import (
    BODY_ARG,
    AuthConfig,
    RequestError,
    build_request,
    operation_id,
    spec_to_tools,
)

PETSTORE = "examples/petstore.json"


@pytest.fixture(scope="module")
def spec():
    return load_spec(PETSTORE)


@pytest.fixture(scope="module")
def tools(spec):
    return spec_to_tools(spec)


def by_name(tools, name):
    return next(t for t in tools if t["name"] == name)


def test_every_operation_becomes_a_tool(tools, spec):
    from mcpify.spec import iter_operations

    assert len(tools) == sum(1 for _ in iter_operations(spec))


def test_operation_ids_become_tool_names(tools):
    names = {t["name"] for t in tools}
    assert {"list_pets", "create_pet", "get_pet", "delete_pet", "get_stats"} <= names


def test_tool_descriptor_shape(tools):
    tool = by_name(tools, "get_pet")
    assert tool["description"].startswith("[GET]")
    assert tool["inputSchema"]["type"] == "object"
    assert "petId" in tool["inputSchema"]["properties"]
    assert tool["inputSchema"]["required"] == ["petId"]
    assert "_meta" in tool and "_meta" not in tool["inputSchema"]


def test_body_ref_is_resolved(tools):
    tool = by_name(tools, "create_pet")
    body_schema = tool["inputSchema"]["properties"][BODY_ARG]
    # 'kind' comes from NewPet via $ref; must be resolved, not a $ref dict
    assert "kind" in body_schema.get("properties", {})
    assert "$ref" not in json.dumps(body_schema)
    assert body_schema["required"] == ["name", "kind"]


def test_duplicate_ids_are_suffixed():
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/a": {"get": {"operationId": "same", "summary": "s"}},
            "/b": {"get": {"operationId": "same", "summary": "s"}},
        },
    }
    tools = spec_to_tools(spec)
    assert [t["name"] for t in tools] == ["same", "same_2"]


def test_missing_ids_fall_back_to_method_path():
    spec = {"openapi": "3.0.0", "paths": {"/pets/{petId}": {"get": {"summary": "s"}}}}
    tool = spec_to_tools(spec)[0]
    assert tool["name"] == "get_pets_petid"


def test_operation_id_sanitized():
    assert operation_id("GET", "/x", {"operationId": "weird id!"}) == "weird_id"


# ---------------------------------------------------------------------------
# request building
# ---------------------------------------------------------------------------

def test_build_request_path_and_query():
    meta = {
        "method": "GET",
        "path": "/pets/{petId}",
        "parameters": [
            {"name": "petId", "in": "path"},
            {"name": "verbose", "in": "query"},
        ],
        "has_body": False,
    }
    request = build_request(
        "https://api.test/v1", meta, {"petId": 7, "verbose": "yes"}
    )
    assert request["method"] == "GET"
    assert request["url"] == "https://api.test/v1/pets/7?verbose=yes"
    assert request["body"] is None


def test_build_request_missing_path_param_raises():
    meta = {"method": "GET", "path": "/pets/{petId}", "parameters": [], "has_body": False}
    with pytest.raises(RequestError):
        build_request("https://api.test", meta, {})


def test_build_request_path_param_is_url_encoded():
    meta = {
        "method": "GET",
        "path": "/files/{name}",
        "parameters": [{"name": "name", "in": "path"}],
        "has_body": False,
    }
    request = build_request("https://api.test", meta, {"name": "a b/c"})
    assert "/files/a%20b%2Fc" in request["url"]


def test_build_request_body_and_headers():
    meta = {
        "method": "POST",
        "path": "/pets",
        "parameters": [{"name": "X-Trace", "in": "header"}],
        "has_body": True,
    }
    request = build_request(
        "https://api.test",
        meta,
        {BODY_ARG: {"name": "Rex"}, "header:X-Trace": "abc"},
    )
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["X-Trace"] == "abc"
    assert json.loads(request["body"]) == {"name": "Rex"}


def test_build_request_missing_body_raises():
    meta = {"method": "POST", "path": "/pets", "parameters": [], "has_body": True}
    with pytest.raises(RequestError):
        build_request("https://api.test", meta, {})


def test_build_request_unknown_arguments_rejected():
    meta = {"method": "GET", "path": "/pets", "parameters": [], "has_body": False}
    with pytest.raises(RequestError) as exc:
        build_request("https://api.test", meta, {"hacker": "yes"})
    assert "hacker" in str(exc.value)


def test_authorization_header_param_is_ignored():
    schema_like = spec_to_tools(
        {
            "openapi": "3.0.0",
            "paths": {
                "/x": {
                    "get": {
                        "operationId": "x",
                        "parameters": [
                            {"name": "Authorization", "in": "header", "schema": {"type": "string"}}
                        ],
                    }
                }
            },
        }
    )[0]
    assert not any(k.startswith("header:Authorization") for k in schema_like["inputSchema"]["properties"])


def test_bearer_auth_from_env(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sekrit-token")
    auth = AuthConfig("MY_KEY", "bearer")
    request = build_request("https://api.test", {"method": "GET", "path": "/", "parameters": [], "has_body": False}, {}, auth)
    assert request["headers"]["Authorization"] == "Bearer sekrit-token"


def test_header_auth_custom_name(monkeypatch):
    monkeypatch.setenv("MY_KEY", "abc123")
    auth = AuthConfig("MY_KEY", "header", "X-API-Key")
    request = build_request("https://api.test", {"method": "GET", "path": "/", "parameters": [], "has_body": False}, {}, auth)
    assert request["headers"]["X-API-Key"] == "abc123"


def test_query_auth_appended_to_url(monkeypatch):
    monkeypatch.setenv("MY_KEY", "abc123")
    auth = AuthConfig("MY_KEY", "query", "api_key")
    url = auth.apply_query("https://api.test/v1/pets?limit=5")
    assert url == "https://api.test/v1/pets?limit=5&api_key=abc123"


def test_missing_env_var_is_request_error(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    auth = AuthConfig("MISSING_KEY", "bearer")
    with pytest.raises(RequestError):
        auth.headers()


def test_unresolvable_body_ref_degrades_during_conversion():
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/a": {
                "post": {
                    "operationId": "a",
                    "requestBody": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Ghost"}}}
                    },
                }
            }
        },
    }
    tools = spec_to_tools(spec)
    body = tools[0]["inputSchema"]["properties"]["body"]
    assert body["type"] == "object"
    assert "could not be fully resolved" in body["description"]


def _old_raises_block_removed():
        spec_to_tools(spec)
