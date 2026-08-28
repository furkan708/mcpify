"""Regression tests for real-world specs with $ref'd parameter schemas.

Found against the live api.weather.gov OpenAPI document: parameter schemas
referencing #/components/schemas/... used to crash tool generation because
an empty spec was passed to the resolver.
"""

import argparse

import pytest

from mcpify.cli import filter_tools
from mcpify.spec import load_spec
from mcpify.tools import spec_to_tools

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Ref'd Params", "version": "1.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/alerts": {
            "get": {
                "operationId": "listAlerts",
                "summary": "List alerts",
                "parameters": [
                    {"name": "region", "in": "query",
                     "description": "Marine region code",
                     "schema": {"$ref": "#/components/schemas/RegionCode"}},
                    {"name": "status", "in": "query",
                     "schema": {"type": "string", "enum": ["actual", "exercise"]}},
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/broken": {
            "get": {
                "operationId": "brokenParam",
                "summary": "Parameter ref pointing nowhere",
                "parameters": [
                    {"name": "oops", "in": "query",
                     "schema": {"$ref": "#/components/schemas/DoesNotExist"}},
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
    "components": {
        "schemas": {
            "RegionCode": {"type": "string",
                           "enum": ["AL", "AT", "GL", "GM", "PA", "PI"]},
        }
    },
}


def make_args(**kwargs):
    base = {"tag": None, "include": None, "exclude": None,
            "read_only": False, "allow": None, "deny": None}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_refd_parameter_schema_resolves():
    tools = spec_to_tools(SPEC)
    tool = next(t for t in tools if t["name"] == "listalerts")
    region = tool["inputSchema"]["properties"]["region"]
    assert region["enum"] == ["AL", "AT", "GL", "GM", "PA", "PI"]
    assert region["type"] == "string"


def test_inline_enum_still_works():
    tools = spec_to_tools(SPEC)
    tool = next(t for t in tools if t["name"] == "listalerts")
    status = tool["inputSchema"]["properties"]["status"]
    assert status["enum"] == ["actual", "exercise"]


def test_unresolvable_ref_degrades_instead_of_crashing():
    # one broken parameter must not take down tool generation
    tools = spec_to_tools(SPEC)
    tool = next(t for t in tools if t["name"] == "brokenparam")
    oops = tool["inputSchema"]["properties"]["oops"]
    assert oops["type"] == "string"
    # the healthy tool in the same spec is unaffected
    healthy = next(t for t in tools if t["name"] == "listalerts")
    assert "region" in healthy["inputSchema"]["properties"]


def test_live_nws_spec_loads():
    """The document that found the bug: api.weather.gov/openapi.json."""
    pytest.importorskip("pyyaml")  # not needed for JSON, guards CI env drift
    try:
        spec = load_spec("https://api.weather.gov/openapi.json")
    except Exception:
        pytest.skip("network unavailable")
    tools = filter_tools(spec_to_tools(spec), make_args(read_only=True))
    names = {t["name"] for t in tools}
    assert "alertsactive" in names or any("alerts" in n for n in names)
    enumliler = [p for t in tools
                 for p in t["inputSchema"]["properties"].values() if "enum" in p]
    assert enumliler, "expected enum'd parameters in the NWS spec"
