"""Tests for the --read-only / --allow / --deny policy layer.

These cover the "GET-only is not side-effect-free" failure mode: a GET
endpoint that mutates state must be deniable, and a read-style POST
endpoint must be includable under --read-only.
"""

import argparse

from mcpify.cli import filter_tools
from mcpify.tools import spec_to_tools

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Ugly API", "version": "1.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/pets/{petId}": {
            "get": {
                "operationId": "getpet",
                "summary": "Get a pet",
                "parameters": [
                    {"name": "petId", "in": "path", "required": True,
                     "schema": {"type": "integer"}}
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/admin/reset-cache": {
            "get": {
                "operationId": "resetcache",
                "summary": "Flush the global cache (mutates state)",
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/search": {
            "post": {
                "operationId": "searchpets",
                "summary": "Search pets (read query via POST body)",
                "requestBody": {
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                    }}}
                },
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def make_args(**kwargs):
    base = {"tag": None, "include": None, "exclude": None,
            "read_only": False, "allow": None, "deny": None}
    base.update(kwargs)
    return argparse.Namespace(**base)


def tool_names(tools):
    return {t["name"] for t in tools}


def all_tools():
    return spec_to_tools(SPEC)


def test_read_only_hides_post_search():
    names = tool_names(filter_tools(all_tools(), make_args(read_only=True)))
    assert "searchpets" not in names


def test_read_only_exposes_mutating_get_by_default():
    # documented limitation: method filtering alone cannot see side effects
    names = tool_names(filter_tools(all_tools(), make_args(read_only=True)))
    assert "resetcache" in names


def test_deny_overrides_read_only_get():
    names = tool_names(filter_tools(
        all_tools(), make_args(read_only=True, deny=["/admin"])))
    assert "resetcache" not in names
    assert "getpet" in names


def test_allow_reincludes_post_search_under_read_only():
    names = tool_names(filter_tools(
        all_tools(), make_args(read_only=True, allow=["/search"])))
    assert "searchpets" in names
    assert "resetcache" in names  # still there without --deny


def test_deny_wins_over_allow():
    names = tool_names(filter_tools(
        all_tools(), make_args(read_only=True, allow=["/search"], deny=["search"])))
    assert "searchpets" not in names


def test_deny_works_without_read_only():
    names = tool_names(filter_tools(all_tools(), make_args(deny=["reset"])))
    assert "resetcache" not in names
    assert "getpet" in names and "searchpets" in names


def test_policy_combo_get_pet_and_search_only():
    names = tool_names(filter_tools(
        all_tools(), make_args(read_only=True, allow=["/search"], deny=["/admin"])))
    assert names == {"getpet", "searchpets"}
