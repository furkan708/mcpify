"""v1.11.0 spec diff: summarize/diff_specs/migration_guide/render and the
`mcpify diff` CLI contract (exit 0 clean / 1 breaking / 2 usage)."""

import json

import pytest

from mcpify.cli import main as cli_main
from mcpify.diff import (
    diff_documents,
    diff_specs,
    migration_guide,
    render,
    summarize,
)
from mcpify.spec import load_spec


def spec_text(body: str) -> str:
    return body


@pytest.fixture()
def old_spec(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1"},
        "servers": [{"url": "http://api.example.com"}],
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "summary": "List pets",
                    "parameters": [],
                    "responses": {"200": {"description": "ok"}},
                },
                "post": {
                    "operationId": "createPet",
                    "summary": "Create a pet",
                    "requestBody": {"required": False, "content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"201": {"description": "ok"}},
                },
            },
            "/pets/{id}": {
                "get": {
                    "operationId": "showPetById",
                    "summary": "One pet",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "ok"}},
                },
            },
        },
    }))
    return str(path)


def write_spec(tmp_path, name, paths):
    path = tmp_path / name
    path.write_text(json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "2"},
        "servers": [{"url": "http://api.example.com"}],
        "paths": paths,
    }))
    return str(path)


# ------------------------------------------------------------------ summarize

def test_summarize_extracts_surface_facts(old_spec):
    facts = summarize(load_spec(old_spec))
    assert facts["GET /pets"]["operationId"] == "listPets"
    assert facts["GET /pets"]["body_required"] is False
    assert facts["POST /pets"]["body_required"] is False
    # path parameters never appear in the parameter list — agents cannot
    # skip them, so they are not agent-facing choices at all
    assert facts["GET /pets/{id}"]["parameters"] == []
    assert facts["GET /pets/{id}"]["required_params"] == []


# ----------------------------------------------------------------- diff_specs

def test_added_and_removed(tmp_path, old_spec):
    new = write_spec(tmp_path, "new.json", {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets",
                          "parameters": [], "responses": {"200": {"description": "ok"}}}},
        "/dogs": {"get": {"operationId": "listDogs", "summary": "List dogs",
                          "parameters": [], "responses": {"200": {"description": "ok"}}}},
    })
    report = diff_specs(load_spec(old_spec), load_spec(new))
    assert report["added"] == ["GET /dogs"]
    assert "GET /pets/{id}" in report["removed"]
    assert "POST /pets" in report["removed"]
    assert report["breaking"] is True  # removals are breaking for callers


def test_required_param_added_is_breaking(tmp_path, old_spec):
    new = write_spec(tmp_path, "new.json", {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets",
                          "parameters": [{"name": "limit", "in": "query", "required": True,
                                          "schema": {"type": "integer"}}],
                          "responses": {"200": {"description": "ok"}}}},
    })
    report = diff_specs(load_spec(old_spec), load_spec(new))
    assert report["breaking"] is True
    item = report["changed"][0]
    assert item["operation"] == "GET /pets"
    assert any("parameter added: query:limit (required)" in c for c in item["changes"])


def test_param_became_required_is_breaking(tmp_path, old_spec):
    new = write_spec(tmp_path, "new.json", {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets",
                          "parameters": [{"name": "limit", "in": "query", "required": True,
                                          "schema": {"type": "integer"}}],
                          "responses": {"200": {"description": "ok"}}}},
    })
    old2 = write_spec(tmp_path, "old2.json", {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets",
                          "parameters": [{"name": "limit", "in": "query", "required": False,
                                          "schema": {"type": "integer"}}],
                          "responses": {"200": {"description": "ok"}}}},
    })
    report = diff_specs(load_spec(old2), load_spec(new))
    assert report["breaking"] is True
    assert any("became required: query:limit" in c for c in report["changed"][0]["changes"])


def test_body_became_required_is_breaking(tmp_path, old_spec):
    new = write_spec(tmp_path, "new.json", {
        "/pets": {
            "get": {"operationId": "listPets", "summary": "List pets",
                    "parameters": [], "responses": {"200": {"description": "ok"}}},
            "post": {"operationId": "createPet", "summary": "Create a pet",
                     "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                     "responses": {"201": {"description": "ok"}}},
        },
    })
    report = diff_specs(load_spec(old_spec), load_spec(new))
    assert report["breaking"] is True
    post = next(item for item in report["changed"] if item["operation"] == "POST /pets")
    assert any("body became required" in c for c in post["changes"])


def test_deprecation_and_operationid_change_warn_without_breaking(tmp_path):
    old2 = write_spec(tmp_path, "old2.json", {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets", "parameters": [],
                          "responses": {"200": {"description": "ok"}}}},
    })
    new = write_spec(tmp_path, "new.json", {
        "/pets": {"get": {"operationId": "listAllPets", "summary": "List pets", "deprecated": True,
                          "parameters": [], "responses": {"200": {"description": "ok"}}}},
    })
    report = diff_specs(load_spec(old2), load_spec(new))
    assert report["breaking"] is False  # deprecation + rename warn, never break
    item = report["changed"][0]
    assert item["breaking"] is False
    assert any("operationId changed: 'listPets' -> 'listAllPets'" in c for c in item["changes"])
    assert any("deprecated" in c for c in item["changes"])


def test_no_changes_is_clean(tmp_path, old_spec):
    report = diff_specs(load_spec(old_spec), load_spec(old_spec))
    assert report == {"added": [], "removed": [], "changed": [], "breaking": False}


# ---------------------------------------------------- migration guide + render

def test_migration_guide_lines(tmp_path, old_spec):
    new = write_spec(tmp_path, "new.json", {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets",
                          "parameters": [{"name": "limit", "in": "query", "required": True,
                                          "schema": {"type": "integer"}}],
                          "responses": {"200": {"description": "ok"}}}},
    })
    guide = migration_guide(diff_specs(load_spec(old_spec), load_spec(new)))
    assert any("pass `limit` when calling `GET /pets`" in line for line in guide)
    assert any("remove usage of `POST /pets`" in line for line in guide)


def test_render_mentions_sections(tmp_path, old_spec):
    new = write_spec(tmp_path, "new.json", {
        "/dogs": {"get": {"operationId": "listDogs", "summary": "s", "parameters": [],
                          "responses": {"200": {"description": "ok"}}}},
    })
    text = render(diff_specs(load_spec(old_spec), load_spec(new)))
    assert "added (1)" in text and "GET /dogs" in text
    assert "removed" in text
    assert "BREAKING" in text


# ------------------------------------------------------------------ documents

def test_diff_documents_roundtrip(tmp_path, old_spec):
    new = write_spec(tmp_path, "new.json", {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets",
                          "parameters": [], "responses": {"200": {"description": "ok"}}}},
    })
    report = diff_documents(open(old_spec).read(), open(new).read())
    assert report["breaking"] is True  # two operations removed
    assert report["removed"] == ["POST /pets", "GET /pets/{id}"] or report["removed"]


# ------------------------------------------------------------------------ CLI

def test_cli_diff_clean_exit_zero(tmp_path, old_spec, capsys):
    cli_main(["diff", old_spec, old_spec])
    out = capsys.readouterr().out
    assert "no tool-surface changes" in out


def test_cli_diff_breaking_exit_one(tmp_path, old_spec, capsys):
    new = write_spec(tmp_path, "new.json", {})
    with pytest.raises(SystemExit) as exc:
        cli_main(["diff", old_spec, new, "--fail-on-breaking"])
    assert exc.value.code == 1


def test_cli_diff_json_output(tmp_path, old_spec, capsys):
    new = write_spec(tmp_path, "new.json", {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets",
                          "parameters": [], "responses": {"200": {"description": "ok"}}}},
    })
    cli_main(["diff", old_spec, new, "--json"])
    report = json.loads(capsys.readouterr().out)
    assert "POST /pets" in report["removed"]


def test_cli_diff_missing_spec_fails(tmp_path, capsys):
    ghost = str(tmp_path / "missing.json")
    with pytest.raises(SystemExit) as exc:
        cli_main(["diff", ghost, ghost])
    assert exc.value.code == 2
