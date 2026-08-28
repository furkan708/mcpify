"""Tests for spec loading, $ref resolution, and operation walking."""

import json

import pytest

from mcpify.spec import (
    SpecError,
    iter_operations,
    load_spec,
    resolve_ref,
    resolve_schema,
    spec_servers,
)

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "T", "version": "1"},
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/a": {
            "get": {"summary": "Get a", "operationId": "get_a"},
            "post": {"summary": "Post a"},
        },
        "/b": {"get": {"operationId": "get_b"}},
    },
    "components": {
        "schemas": {
            "Pet": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "tag": {"$ref": "#/components/schemas/Tag"},
                },
            },
            "Tag": {"type": "string", "enum": ["cat", "dog"]},
        }
    },
}


@pytest.fixture()
def spec_file(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(SPEC), encoding="utf-8")
    return path


def test_load_json_spec(spec_file):
    spec = load_spec(str(spec_file))
    assert spec["openapi"] == "3.0.3"


def test_load_yaml_spec(tmp_path):
    pytest.importorskip("yaml")
    path = tmp_path / "spec.yaml"
    path.write_text(
        "openapi: 3.0.3\ninfo:\n  title: Y\n  version: '1'\npaths: {}\n",
        encoding="utf-8",
    )
    spec = load_spec(str(path))
    assert spec["info"]["title"] == "Y"


def test_yaml_without_pyyaml_gives_clear_error(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_yaml(name, *a, **kw):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    path = tmp_path / "spec.yaml"
    path.write_text("openapi: 3.0.3\n", encoding="utf-8")
    with pytest.raises(SpecError) as exc:
        load_spec(str(path))
    assert "pip install" in str(exc.value)


def test_missing_file():
    with pytest.raises(SpecError):
        load_spec("/nonexistent/spec.json")


def test_not_openapi(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"foo": 1}), encoding="utf-8")
    with pytest.raises(SpecError):
        load_spec(str(path))


def test_no_paths(tmp_path):
    path = tmp_path / "x.json"
    path.write_text(json.dumps({"openapi": "3.0.3", "info": {}, "paths": None}), encoding="utf-8")
    with pytest.raises(SpecError):
        load_spec(str(path))


def test_url_source_serves_spec(SpecServerFactory):
    server = SpecServerFactory(SPEC)
    spec = load_spec(server.url)
    assert spec["info"]["title"] == "T"
    server.shutdown()


def test_resolve_ref_simple():
    assert resolve_ref(SPEC, "#/components/schemas/Tag")["enum"] == ["cat", "dog"]


def test_resolve_ref_missing_raises():
    with pytest.raises(SpecError):
        resolve_ref(SPEC, "#/components/schemas/Nope")


def test_resolve_ref_external_rejected():
    with pytest.raises(SpecError):
        resolve_ref(SPEC, "https://elsewhere/spec.json#/x")


def test_resolve_schema_nested_refs():
    schema = resolve_schema({"$ref": "#/components/schemas/Pet"}, SPEC)
    assert schema["properties"]["tag"]["enum"] == ["cat", "dog"]
    assert schema["properties"]["name"]["type"] == "string"


def test_iter_operations_counts_methods():
    found = {(method, path) for method, path, _ in iter_operations(SPEC)}
    assert found == {("GET", "/a"), ("POST", "/a"), ("GET", "/b")}


def test_spec_servers():
    assert spec_servers(SPEC) == ["https://api.example.com/v1"]
    assert spec_servers({"paths": {}}) == []
