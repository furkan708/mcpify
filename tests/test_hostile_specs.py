"""Hostile-spec regression suite.

Every case here maps to a documented real-world MCP/OpenAPI pain point
(sources: arXiv 2507.16044 REST->MCP study; truefoundry & digitalapi
conversion guides). These scenarios crashed or misbehaved in v1.0.2 and
are locked in so they stay fixed.
"""


import pytest

from mcpify.cli import _base_url
from mcpify.http_client import format_result
from mcpify.spec import SpecError
from mcpify.tools import build_request, spec_to_tools


def spec_with(paths, components=None, servers=None, openapi="3.0.0"):
    return {
        "openapi": openapi,
        "info": {"title": "hostile", "version": "1"},
        "servers": servers or [{"url": "https://api.example.com"}],
        "paths": paths,
        **({"components": components} if components else {}),
    }


# ---------- A) spec structure ----------

def test_request_body_ref_resolves_into_tool_schema():
    spec = spec_with(
        {"/pets": {"post": {
            "operationId": "createPet", "summary": "Create",
            "requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Pet"}}}},
            "responses": {"200": {"description": "ok"}}}}},
        {"schemas": {"Pet": {"type": "object", "properties": {
            "name": {"type": "string"}}}}},
    )
    body = spec_to_tools(spec)[0]["inputSchema"]["properties"]["body"]
    assert list(body["properties"]) == ["name"]


def test_circular_body_ref_degrades_instead_of_crashing():
    spec = spec_with(
        {"/node": {"post": {
            "operationId": "makeNode", "summary": "Make",
            "requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Node"}}}},
            "responses": {"200": {"description": "ok"}}}}},
        {"schemas": {"Node": {"type": "object", "properties": {
            "child": {"$ref": "#/components/schemas/Node"}}}}},
    )
    body = spec_to_tools(spec)[0]["inputSchema"]["properties"]["body"]
    assert body["type"] == "object"
    assert "could not be fully resolved" in body["description"]


def test_multipart_only_body_becomes_raw_string_arg():
    spec = spec_with(
        {"/upload": {"post": {
            "operationId": "uploadFile", "summary": "Upload",
            "requestBody": {"content": {"multipart/form-data": {
                "schema": {"type": "object", "properties": {
                    "file": {"type": "string", "format": "binary"}}}}}},
            "responses": {"200": {"description": "ok"}}}}},
    )
    tool = spec_to_tools(spec)[0]
    body = tool["inputSchema"]["properties"]["body"]
    assert body["type"] == "string"
    assert "multipart/form-data" in body["description"]
    # and build_request sends it raw with the right content type
    req = build_request("https://api.example.com", tool["_meta"],
                        {"body": "RAW"})
    assert req["body"] == b"RAW"
    assert req["headers"]["Content-Type"] == "multipart/form-data"


def test_allof_members_merge_into_body_schema():
    spec = spec_with(
        {"/pets": {"post": {
            "operationId": "createPet", "summary": "Create",
            "requestBody": {"content": {"application/json": {"schema": {"allOf": [
                {"$ref": "#/components/schemas/Base"},
                {"type": "object", "properties": {"extra": {"type": "string"}}}]}}}},
            "responses": {"200": {"description": "ok"}}}}},
        {"schemas": {"Base": {"type": "object", "required": ["name"],
                              "properties": {"name": {"type": "string"}}}}},
    )
    body = spec_to_tools(spec)[0]["inputSchema"]["properties"]["body"]
    assert set(body["properties"]) == {"name", "extra"}
    assert body["required"] == ["name"]


def test_oas31_type_arrays_do_not_crash():
    spec = spec_with(
        {"/x": {"get": {"operationId": "getX", "summary": "Get",
                        "parameters": [{"name": "q", "in": "query",
                                        "schema": {"type": ["string", "null"]}}],
                        "responses": {"200": {"description": "ok"}}}}},
        openapi="3.1.0",
    )
    props = spec_to_tools(spec)[0]["inputSchema"]["properties"]
    assert props["q"]["type"] == ["string", "null"]


# ---------- B) server URL (arXiv failure category B) ----------

def test_server_variable_defaults_are_substituted():
    spec = spec_with(
        {"/x": {"get": {"operationId": "x", "responses": {"200": {"description": "ok"}}}}},
        servers=[{"url": "https://{host}/v2",
                  "variables": {"host": {"default": "api.example.com"}}}],
    )
    assert _base_url(spec, None) == "https://api.example.com/v2"


def test_server_variable_without_default_fails_cleanly():
    spec = spec_with(
        {"/x": {"get": {"operationId": "x", "responses": {"200": {"description": "ok"}}}}},
        servers=[{"url": "https://{region}.api.io",
                  "variables": {"region": {"enum": ["eu", "us"]}}}],
    )
    with pytest.raises(SpecError, match="--base-url"):
        _base_url(spec, None)


def test_relative_server_url_fails_cleanly():
    spec = spec_with(
        {"/x": {"get": {"operationId": "x", "responses": {"200": {"description": "ok"}}}}},
        servers=[{"url": "/api/v3"}],
    )
    with pytest.raises(SpecError, match="relative"):
        _base_url(spec, None)


# ---------- C) tool surface ----------

def test_useless_methods_are_not_exposed():
    spec = spec_with({"/x": {
        "get": {"operationId": "getX", "summary": "g", "responses": {"200": {"description": "ok"}}},
        "head": {"operationId": "headX", "summary": "h", "responses": {"200": {"description": "ok"}}},
        "options": {"operationId": "optionsX", "summary": "o", "responses": {"200": {"description": "ok"}}},
        "trace": {"operationId": "traceX", "summary": "t", "responses": {"200": {"description": "ok"}}},
        "patch": {"operationId": "patchX", "summary": "p", "responses": {"200": {"description": "ok"}}},
    }})
    names = {t["name"] for t in spec_to_tools(spec)}
    assert {"getx", "patchx"} <= names
    assert not (names & {"headx", "optionsx", "tracex"})


# ---------- D) runtime ----------

def test_500_operations_convert_fast():
    spec = spec_with({f"/res{i}": {"get": {"operationId": f"get{i}", "summary": f"s{i}",
                                           "responses": {"200": {"description": "ok"}}}}
                      for i in range(500)})
    tools = spec_to_tools(spec)
    assert len(tools) == 500


def test_oversized_response_is_truncated():
    big = "x" * 300_000
    text, is_error = format_result({"status": 200, "body": big, "json": None})
    assert not is_error
    assert len(text) < 60_000
    assert "truncated" in text
    assert "260,000 more characters" in text  # mentions the cut size
