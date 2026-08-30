"""v1.11.0 external-$ref bundling: multi-file and URL-referenced specs
load as one merged document. Cycles and unreadable targets are skipped
(documented limit), never crash the load."""

import json

from mcpify.spec import load_spec

MAIN = """\
openapi: 3.0.0
info: {{title: T, version: "1"}}
servers: [http://api.example.com]
paths:
  /pets:
    post:
      operationId: createPet
      summary: Create
      requestBody:
        content:
          application/json:
            schema:
              $ref: '{ref}'
      responses: {{201: {{description: ok}}}}
"""


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_external_file_ref_is_inlined(tmp_path):
    write(tmp_path, "schemas.yaml", """\
components:
  schemas:
    Pet:
      type: object
      properties:
        name: {type: string}
""")
    main = write(tmp_path, "main.yaml", MAIN.format(ref="schemas.yaml#/components/schemas/Pet"))
    spec = load_spec(main)
    schema = spec["paths"]["/pets"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["type"] == "object"
    assert "name" in schema["properties"]
    assert "$ref" not in schema


def test_component_only_target_file_is_valid(tmp_path):
    """Ref targets are often component-only files that are not valid
    OpenAPI documents on their own — bundling must accept them."""
    write(tmp_path, "schemas.json", json.dumps({
        "components": {"schemas": {"Pet": {"type": "object", "properties": {"age": {"type": "integer"}}}}},
    }))
    main = write(tmp_path, "main.json", json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1"},
        "servers": [{"url": "http://api.example.com"}],
        "paths": {
            "/pets": {
                "post": {
                    "operationId": "createPet",
                    "requestBody": {"content": {"application/json": {
                        "schema": {"$ref": "schemas.json#/components/schemas/Pet"}}}},
                    "responses": {"201": {"description": "ok"}},
                },
            },
        },
    }))
    schema = load_spec(main)["paths"]["/pets"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["properties"]["age"]["type"] == "integer"


def test_missing_target_is_skipped_not_fatal(tmp_path):
    main = write(tmp_path, "main.yaml", MAIN.format(ref="ghost.yaml#/components/schemas/Pet"))
    spec = load_spec(main)  # must not raise
    assert spec["paths"]["/pets"]["post"]["operationId"] == "createPet"


def test_circular_ref_does_not_hang(tmp_path):
    write(tmp_path, "a.yaml", """\
openapi: 3.0.0
info: {title: A, version: "1"}
servers: [http://api.example.com]
paths:
  /a:
    get:
      operationId: aGet
      responses: {200: {description: ok}}
components:
  schemas:
    Loop:
      $ref: 'b.yaml#/components/schemas/Bloop'
""")
    write(tmp_path, "b.yaml", """\
components:
  schemas:
    Bloop:
      $ref: 'a.yaml#/components/schemas/Loop'
""")
    spec = load_spec(write(tmp_path, "main.yaml", MAIN.format(
        ref="a.yaml#/components/schemas/Loop")))
    # load finished; the innermost cycle edge is left in place (documented)
    assert spec["paths"]["/pets"]["post"]["operationId"] == "createPet"


def test_nested_refs_resolve_relative_to_their_own_file(tmp_path):
    write(tmp_path, "outer.yaml", """\
components:
  schemas:
    Outer:
      allOf:
        - $ref: 'inner.yaml#/components/schemas/Inner'
""")
    write(tmp_path, "inner.yaml", """\
components:
  schemas:
    Inner:
      type: object
      properties:
        code: {type: string}
""")
    spec = load_spec(write(tmp_path, "main.yaml", MAIN.format(
        ref="outer.yaml#/components/schemas/Outer")))
    schema = spec["paths"]["/pets"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    # Outer is an allOf wrapper; the inner file landed inside it
    assert schema["allOf"][0]["properties"]["code"]["type"] == "string"


def test_same_document_refs_are_untouched(tmp_path):
    main = write(tmp_path, "main.yaml", MAIN.format(ref="#/components/schemas/Pet"))
    spec = load_spec(main)
    schema = spec["paths"]["/pets"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert schema["$ref"] == "#/components/schemas/Pet"
