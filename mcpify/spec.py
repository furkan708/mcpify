"""OpenAPI specification loading, $ref resolution, and operation walking.

Supports JSON natively (zero dependencies). YAML specs work when PyYAML
is installed (``pip install 'mcpify[yaml]'``).
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head")


class SpecError(ValueError):
    """Raised when an OpenAPI document cannot be loaded or is invalid."""


def load_spec(source: str) -> dict:
    """Load an OpenAPI document from a file path or an http(s) URL.

    JSON is always supported; YAML requires PyYAML.
    """
    text: str
    if source.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(source, timeout=15) as response:
                text = response.read().decode("utf-8")
        except Exception as err:
            raise SpecError(f"could not fetch '{source}': {err}") from err
    else:
        path = Path(source)
        if not path.is_file():
            raise SpecError(f"file not found: {source}")
        text = path.read_text(encoding="utf-8")

    # Try JSON first (the common case), fall back to YAML when available.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError:
            raise SpecError(
                f"'{source}' is not valid JSON. For YAML specs install: "
                "pip install 'mcpify[yaml]'"
            ) from None
        try:
            data = yaml.safe_load(text)
        except Exception as err:
            raise SpecError(f"could not parse '{source}': {err}") from err

    if not isinstance(data, dict):
        raise SpecError("specification root must be an object")
    if "openapi" not in data and "swagger" not in data:
        raise SpecError(
            "not an OpenAPI document (missing 'openapi' or 'swagger' version field)"
        )
    if not isinstance(data.get("paths"), dict):
        raise SpecError("specification has no 'paths' object")
    return data


def resolve_ref(spec: dict, ref: str) -> Any:
    """Resolve a local reference like '#/components/schemas/Pet'."""
    if not ref.startswith("#/"):
        raise SpecError(
            f"only local references are supported (got '{ref}'); "
            "inline or bundle external documents first"
        )
    node = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            node = node[part]
        except (KeyError, TypeError):
            raise SpecError(f"unresolvable reference: '{ref}'") from None
    return node


def resolve_schema(schema: Any, spec: dict, depth: int = 0) -> Any:
    """Deeply resolve internal $ref pointers inside a schema."""
    if depth > 20:
        raise SpecError("circular $ref chain too deep (max 20)")
    if isinstance(schema, list):
        return [resolve_schema(item, spec, depth) for item in schema]
    if not isinstance(schema, dict):
        return schema

    if "$ref" in schema:
        target = resolve_ref(spec, schema["$ref"])
        return resolve_schema(target, spec, depth + 1)

    resolved = {}
    for key, value in schema.items():
        if key == "items" and isinstance(value, dict):
            resolved[key] = resolve_schema(value, spec, depth + 1)
        elif key == "properties" and isinstance(value, dict):
            resolved[key] = {
                name: resolve_schema(sub, spec, depth + 1) for name, sub in value.items()
            }
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
            resolved_subs = [resolve_schema(sub, spec, depth + 1) for sub in value]
            if key == "allOf":
                # allOf intersection: merge member properties/required upward
                merged_props = dict(resolved.get("properties") or {})
                merged_req = list(resolved.get("required") or [])
                for sub in resolved_subs:
                    if isinstance(sub, dict):
                        merged_props.update(sub.get("properties") or {})
                        for req in sub.get("required") or []:
                            if req not in merged_req:
                                merged_req.append(req)
                if merged_props:
                    resolved["properties"] = merged_props
                if merged_req:
                    resolved["required"] = merged_req
                # merged members are flattened into the parent; the allOf key
                # itself is intentionally dropped
            else:
                resolved[key] = resolved_subs
        else:
            resolved[key] = value
    return resolved


def iter_operations(spec: dict) -> Iterator[tuple[str, str, dict]]:
    """Yield (method, path, operation) for every operation in the document."""
    for path, path_item in spec.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if isinstance(operation, dict):
                yield method.upper(), path, operation


def spec_servers(spec: dict) -> list[str]:
    """Return the declared server URLs (may be empty)."""
    servers = spec.get("servers") or []
    return [s.get("url", "") for s in servers if isinstance(s, dict)]


# Well-known locations probed for a bare origin URL ("https://api.x.com").
DISCOVERY_PATHS = (
    "/.well-known/openapi.json",
    "/openapi.json",
    "/swagger.json",
    "/openapi.yaml",
    "/api-docs",
)


def discover_spec(url: str, timeout: float = 5.0) -> tuple[str, str]:
    """Probe a bare origin for a served OpenAPI document.

    Returns (document_url, hint). hint is empty when the first candidate
    wins; otherwise it lists the paths that were tried, so a failed
    discovery produces an actionable message instead of a guess.
    """
    from urllib.parse import urlparse
    from urllib.request import urlopen

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.path not in ("", "/"):
        return url, ""
    origin = f"{parsed.scheme}://{parsed.netloc}"
    tried: list[str] = []
    for path in DISCOVERY_PATHS:
        candidate = origin + path
        tried.append(path)
        try:
            with urlopen(candidate, timeout=timeout) as response:
                head = response.read(4096).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — any failure just means "not here"
            continue
        lowered = head.lower()
        if '"openapi"' in lowered or '"swagger"' in lowered or "openapi:" in lowered or "swagger:" in lowered:
            return candidate, ""
    raise SpecError(
        f"no OpenAPI document found at {origin} — tried {', '.join(tried)}. "
        "Pass the document URL explicitly (e.g. " + origin + "/openapi.json)."
    )
