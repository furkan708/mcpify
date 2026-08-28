"""Hostile-corpus harness: runs research-sourced OpenAPI pain points through mcpify.

Categories come from: arXiv 2507.16044 (REST->MCP empirical study),
truefoundry OpenAPI->MCP edge-case guide, digitalapi conversion guide.
Each scenario reports CRASH / WARN / OK so fixes can be prioritised.
"""

import contextlib
import io
import json
import sys
import time

sys.path.insert(0, "/home/user/mcpify")

from mcpify.cli import _base_url  # noqa: E402
from mcpify.spec import SpecError  # noqa: E402
from mcpify.tools import spec_to_tools  # noqa: E402


def make_args(**kw):
    base = {"tag": None, "include": None, "exclude": None,
            "read_only": False, "allow": None, "deny": None}
    base.update(kw)
    import argparse
    return argparse.Namespace(**base)


def durum(fn, ad, kategori, kaynak):
    try:
        sonuc = fn()
        if sonuc is True:
            print(f"  OK    | {ad} [{kategori}]")
        else:
            print(f"  SORUN | {ad} [{kategori}] -> {sonuc}")
    except SpecError as e:
        print(f"  TEMIZ-HATA | {ad} [{kategori}] -> {str(e)[:90]}")
    except SystemExit:
        print(f"  TEMIZ-HATA | {ad} [{kategori}] -> (fail ile cikti)")
    except Exception as e:
        print(f"  CRASH | {ad} [{kategori}] -> {type(e).__name__}: {str(e)[:90]}")


print("=== A) SPEC YAPISI KATEGORISI ===")

# A1: requestBody $ref (params bug'inin kardesi)
SPEC_BODY_REF = {
    "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://x.io"}],
    "paths": {"/pets": {"post": {
        "operationId": "createPet", "summary": "Create",
        "requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Pet"}}}},
        "responses": {"200": {"description": "ok"}}}}},
    "components": {"schemas": {"Pet": {"type": "object", "properties": {
        "name": {"type": "string"}, "tag": {"type": "string"}}}}},
}
def a1_test():
    body = spec_to_tools(SPEC_BODY_REF)[0]["inputSchema"]["properties"].get("body", {})
    return list(body.get("properties", {}).keys()) == ["name", "tag"] or f"body properties: {body}"


durum(a1_test, "A1 request-body $ref cozumu", "$ref", "arXiv/truefoundry")

# A2: dongusel $ref (body)
SPEC_CIRC = {
    "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://x.io"}],
    "paths": {"/node": {"post": {
        "operationId": "makeNode", "summary": "Make",
        "requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Node"}}}},
        "responses": {"200": {"description": "ok"}}}}},
    "components": {"schemas": {"Node": {"type": "object", "properties": {
        "child": {"$ref": "#/components/schemas/Node"}}}}},
}
durum(lambda: (spec_to_tools(SPEC_CIRC), True)[1],
      "A2 dongusel $ref body", "circular", "truefoundry")

# A3: multipart-only body (dosya yukleme)
SPEC_MULTIPART = {
    "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://x.io"}],
    "paths": {"/upload": {"post": {
        "operationId": "uploadFile", "summary": "Upload",
        "requestBody": {"content": {"multipart/form-data": {
            "schema": {"type": "object", "properties": {
                "file": {"type": "string", "format": "binary"}}}}}},
        "responses": {"200": {"description": "ok"}}}}},
}
durum(lambda: (spec_to_tools(SPEC_MULTIPART)[0]["inputSchema"]["properties"].get("body") is not None,
               "body tamamen KAYBOLuyor (sessiz veri kaybi)" if spec_to_tools(SPEC_MULTIPART)[0]["inputSchema"]["properties"].get("body") is None else True)[1],
      "A3 multipart-only body", "uploads", "truefoundry")

# A4: allOf merge
SPEC_ALLOF = {
    "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://x.io"}],
    "paths": {"/pets": {"post": {
        "operationId": "createPet", "summary": "Create",
        "requestBody": {"content": {"application/json": {"schema": {"allOf": [
            {"$ref": "#/components/schemas/Base"},
            {"type": "object", "properties": {"extra": {"type": "string"}}}]}}}},
        "responses": {"200": {"description": "ok"}}}}},
    "components": {"schemas": {"Base": {"type": "object", "required": ["name"],
                                        "properties": {"name": {"type": "string"}}}}},
}
durum(lambda: "name" in str(spec_to_tools(SPEC_ALLOF)[0]["inputSchema"]["properties"].get("body", {}))
      and "extra" in str(spec_to_tools(SPEC_ALLOF)[0]["inputSchema"]["properties"].get("body", {})),
      "A4 allOf birlestirme", "polymorphic", "truefoundry")

# A5: OpenAPI 3.1 tip dizisi + nullable
SPEC_31 = {
    "openapi": "3.1.0", "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://x.io"}],
    "paths": {"/x": {"get": {
        "operationId": "getX", "summary": "Get",
        "parameters": [{"name": "q", "in": "query",
                        "schema": {"type": ["string", "null"]}}],
        "responses": {"200": {"description": "ok"}}}}},
}
durum(lambda: (spec_to_tools(SPEC_31), True)[1],
      "A5 3.1 type-dizisi parametre", "oas3.1", "truefoundry")

print()
print("=== B) SUNUCU URL KATEGORISI (arXiv kategori B: ilk-3 basarisizlik sebebi) ===")

SPEC_VAR = {
    "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://{host}/v2", "variables": {
        "host": {"default": "api.example.com", "enum": ["api.example.com"]}}}],
    "paths": {"/x": {"get": {"operationId": "x", "responses": {"200": {"description": "ok"}}}}},
}
SPEC_VAR_NODEFAULT = {
    "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://{region}.api.io", "variables": {
        "region": {"enum": ["eu", "us"]}}}],
    "paths": {"/x": {"get": {"operationId": "x", "responses": {"200": {"description": "ok"}}}}},
}
SPEC_RELATIVE = {
    "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
    "servers": [{"url": "/api/v3"}],
    "paths": {"/x": {"get": {"operationId": "x", "responses": {"200": {"description": "ok"}}}}},
}
SPEC = json.loads(json.dumps(SPEC_VAR))


def b1_test():
    base = _base_url(json.loads(json.dumps(SPEC_VAR)), None)
    if "{host}" in base:
        return f"degisken cozulmedi: {base}"
    return base == "https://api.example.com/v2" or f"henuz: {base}"


def b2_test():
    try:
        base = _base_url(json.loads(json.dumps(SPEC_VAR_NODEFAULT)), None)
        if "{" in base:
            return f"degisken varsayilansiz kaldigi gibi: {base} (net hata beklenirdi)"
        return True
    except SpecError:
        return True  # temiz hata iyi


def b3_test():
    try:
        base = _base_url(json.loads(json.dumps(SPEC_RELATIVE)), None)
        return f"relative URL sessizce kabul: {base} -> calisma aninda 'unknown url type' patlar"
    except SpecError:
        return True


durum(b1_test, "B1 server {variables}+default", "base-url", "arXiv-katB")
durum(b2_test, "B2 server {variables}+default'suz", "base-url", "arXiv-katB")
durum(b3_test, "B3 relative server URL", "base-url", "arXiv-katB")

print()
print("=== C) TOOL YUZEYI KATEGORISI ===")

SPEC_METHODS = {
    "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://x.io"}],
    "paths": {"/x": {
        "get": {"operationId": "getX", "summary": "g", "responses": {"200": {"description": "ok"}}},
        "head": {"operationId": "headX", "summary": "h", "responses": {"200": {"description": "ok"}}},
        "options": {"operationId": "optionsX", "summary": "o", "responses": {"200": {"description": "ok"}}},
        "trace": {"operationId": "traceX", "summary": "t", "responses": {"200": {"description": "ok"}}},
        "patch": {"operationId": "patchX", "summary": "p", "responses": {"200": {"description": "ok"}}},
    }},
}


def c1_test():
    isimler = {t["name"] for t in spec_to_tools(SPEC_METHODS)}
    fazla = isimler & {"headx", "optionsx", "tracex"}
    eksik = {"getx", "patchx"} - isimler
    if fazla:
        return f"ajana islevsiz metodlar acik: {fazla}"
    if eksik:
        return f"gerekli metod kayip: {eksik}"
    return True


durum(c1_test, "C1 HEAD/OPTIONS/TRACE filteri", "tool-surface", "truefoundry")

SPEC_DEPRECATED = {
    "openapi": "3.0.0", "info": {"title": "t", "version": "1"},
    "servers": [{"url": "https://x.io"}],
    "paths": {"/old": {"get": {"operationId": "oldGet", "summary": "old",
                               "deprecated": True, "responses": {"200": {"description": "ok"}}}}},
}
def c2_test():
    from contextlib import redirect_stdout

    from mcpify import cli as cli_mod
    # doctor ciktisinda deprecated uyarisi var mi?
    buf = io.StringIO()
    with redirect_stdout(buf), contextlib.suppress(SystemExit):
        cli_mod.main(["doctor", "/tmp/doctor-test.json"])
    return "deprecated operation" in buf.getvalue() or "deprecated" in buf.getvalue()


durum(c2_test, "C2 deprecated operasyon uyarisi (doctor)", "tool-surface", "digitalapi")

print()
print("=== D) RUNTIME KATEGORISI ===")

# D1: dev olcekte performans (500 operasyon)
buyuk = {"openapi": "3.0.0", "info": {"title": "t", "version": "1"},
         "servers": [{"url": "https://x.io"}], "paths": {}}
for i in range(500):
    buyuk["paths"][f"/res{i}"] = {"get": {"operationId": f"get{i}", "summary": f"s{i}",
                                          "responses": {"200": {"description": "ok"}}}}


def d1_test():
    basla = time.time()
    spec_to_tools(buyuk)
    sure = time.time() - basla
    if sure > 5:
        return f"500 op -> {sure:.1f}s cok yavas"
    return True  # {len(tools)} tool, {sure:.2f}s


durum(d1_test, "D1 500-operasyon performans", "scale", "digitalapi")

# D2: cevap boyutu (context patlamasi)
from mcpify.http_client import format_result  # noqa: E402


def d2_test():
    dev = "x" * 300_000
    metin, _ = format_result({"status": 200, "body": dev, "json": None})
    if len(metin) > 100_000:
        return f"300KB cevap filtrelenmeden modele gidiyor ({len(metin)//1000}KB)"
    return True


durum(d2_test, "D2 devasa cevap kesme", "context-cost", "digitalapi")

print()
print("bitti")
