"""Translate OpenAPI operations into MCP tools, and arguments into requests."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from .spec import SpecError, resolve_schema

# Body arguments are exposed under this property name.
BODY_ARG = "body"


def slugify(*parts: str) -> str:
    text = "_".join(parts).lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "call"


def operation_id(method: str, path: str, operation: dict) -> str:
    """Prefer the spec's operationId, fall back to method_path."""
    declared = str(operation.get("operationId", "")).strip()
    if declared:
        return slugify(declared)
    template = re.sub(r"[{}]", "", path)
    return slugify(method, template)


def build_description(method: str, path: str, operation: dict) -> str:
    text = str(operation.get("summary") or operation.get("description") or "").strip()
    if text:
        return f"[{method}] {text}"
    return f"[{method}] Call {path}"


def extract_parameters(operation: dict, path_item: dict, spec: dict) -> list[dict]:
    """Collect path/query/header parameters from the operation and its path item."""
    merged: dict[tuple, dict] = {}

    def add(params: list[dict]) -> None:
        for param in params:
            if not isinstance(param, dict):
                continue
            if "$ref" in param:
                from .spec import resolve_ref

                param = resolve_ref(spec, param["$ref"])
            key = (param.get("in"), param.get("name"))
            merged[key] = param

    add(path_item.get("parameters") or [])
    add(operation.get("parameters") or [])
    return list(merged.values())


def build_input_schema(
    method: str,
    operation: dict,
    parameters: list[dict],
    request_schema: dict | None,
    spec: dict | None = None,
    raw_body_content_type: str | None = None,
) -> dict:
    """Build the JSON Schema for an MCP tool from parameters + request body.

    Parameter schemas are resolved against the full spec so `$ref`-based
    parameters (common in large real-world specs) work. A parameter whose
    schema cannot be resolved degrades to a string rather than failing the
    whole server.
    """
    spec = spec or {}
    properties: dict = {}
    required: list[str] = []

    def safe_resolve(schema: dict) -> dict:
        try:
            return resolve_schema(schema, spec)
        except SpecError:
            return {"type": "string",
                    "description": "(schema could not be fully resolved from the spec)"}

    for param in parameters:
        location = param.get("in")
        if location not in ("path", "query", "header"):
            continue
        name = str(param.get("name", ""))
        if not name:
            continue
        schema = safe_resolve(param["schema"]) if param.get("schema") else {"type": "string"}
        entry = {"type": schema.get("type", "string"), "description": str(param.get("description", ""))}
        if schema.get("enum"):
            entry["enum"] = schema["enum"]
        # never advertise a header param for Authorization — auth is managed by flags
        if location == "header" and name.lower() == "authorization":
            continue
        # path/query params are plain names; header params are namespaced
        key = name if location in ("path", "query") else f"header:{name}"
        properties[key] = entry
        if param.get("required"):
            required.append(key)

    if request_schema is not None and method not in ("GET", "HEAD", "DELETE"):
        if raw_body_content_type:
            properties[BODY_ARG] = {
                "type": "string",
                "description": (f"raw request body, sent as-is "
                                f"(content type: {raw_body_content_type})"),
            }
        else:
            note = ""
            if not request_schema.get("properties") and request_schema.get("x-unresolved"):
                note = " (schema could not be fully resolved from the spec)"
            properties[BODY_ARG] = {
                "type": "object",
                "description": "JSON request body" + note,
                **({"properties": request_schema.get("properties", {})} if request_schema.get("properties") else {}),
                **({"required": request_schema["required"]} if request_schema.get("required") else {}),
            }

    return {"type": "object", "properties": properties, "required": required}


def operation_to_tool(method: str, path: str, operation: dict, path_item: dict, spec: dict, taken: set) -> dict:
    """Return an MCP tool descriptor for one OpenAPI operation."""
    parameters = extract_parameters(operation, path_item, spec)

    request_schema = None
    raw_body_content_type = None
    body = operation.get("requestBody")
    if isinstance(body, dict):
        content = body.get("content") or {}
        json_media = content.get("application/json")
        if isinstance(json_media, dict) and json_media.get("schema"):
            try:
                request_schema = resolve_schema(json_media["schema"], spec)
            except SpecError:
                # circular / unresolvable body schema must not kill the tool
                request_schema = {"type": "object", "x-unresolved": True}
        elif content:
            # non-JSON bodies (multipart uploads, form posts): expose a raw
            # string body instead of silently dropping the operation's payload
            raw_body_content_type = next(iter(content))
            request_schema = {"type": "string"}

    name = operation_id(method, path, operation)
    base = name
    counter = 2
    while name in taken:
        name = f"{base}_{counter}"
        counter += 1
    taken.add(name)

    return {
        "name": name,
        "description": build_description(method, path, operation),
        "inputSchema": build_input_schema(method.upper(), operation, parameters, request_schema, spec, raw_body_content_type),
        "_meta": {
            "method": method.upper(),
            "path": path,
            "parameters": parameters,
            "has_body": request_schema is not None,
            "raw_body_content_type": raw_body_content_type,
            "tags": list(operation.get("tags") or []),
        },
    }


def spec_to_tools(spec: dict) -> list[dict]:
    """Convert every operation in the spec into MCP tool descriptors."""
    from .spec import iter_operations

    tools: list[dict] = []
    taken: set = set()
    for method, path, operation in iter_operations(spec):
        if method.lower() in ("head", "options", "trace"):
            continue  # no agent value; they carry no request semantics
        path_item = spec["paths"][path]
        tools.append(operation_to_tool(method, path, operation, path_item, spec, taken))
    return tools


# ---------------------------------------------------------------------------
# request building
# ---------------------------------------------------------------------------

class RequestError(ValueError):
    """Raised when arguments cannot form a valid HTTP request."""


def build_request(
    base_url: str,
    meta: dict,
    arguments: dict,
    auth: AuthConfig | None = None,
) -> dict:
    """Turn tool arguments into a concrete HTTP request (url/headers/body)."""

    method = meta["method"]
    path_template = meta["path"]
    used: set = set()

    # path parameters
    def substitute(match: re.Match) -> str:
        name = match.group(1)
        arg = arguments.get(name)
        if arg is None or arg == "":
            raise RequestError(f"missing required path parameter '{name}'")
        used.add(name)
        return quote(str(arg), safe="")

    path = re.sub(r"\{([^{}]+)\}", substitute, path_template)
    if "{" in path or "}" in path:
        raise RequestError(f"unfilled path parameter in '{path}'")

    # query parameters
    query_pairs: list[tuple[str, str]] = []
    for param in meta["parameters"]:
        if param.get("in") != "query":
            continue
        name = str(param.get("name", ""))
        if name in arguments and arguments[name] not in (None, ""):
            query_pairs.append((name, str(arguments[name])))
            used.add(name)

    # header parameters
    headers = {"Accept": "application/json"}
    for param in meta["parameters"]:
        if param.get("in") != "header":
            continue
        name = str(param.get("name", ""))
        key = f"header:{name}"
        if key in arguments and arguments[key] not in (None, ""):
            headers[name] = str(arguments[key])
            used.add(key)

    # body
    body_bytes = None
    if meta.get("has_body"):
        body = arguments.get(BODY_ARG)
        if body is None:
            raise RequestError(f"missing required argument '{BODY_ARG}' (JSON request body)")
        raw_ct = meta.get("raw_body_content_type")
        if raw_ct:
            if not isinstance(body, str):
                raise RequestError(f"'{BODY_ARG}' must be a string for {raw_ct} bodies")
            body_bytes = body.encode("utf-8")
            headers["Content-Type"] = raw_ct
        elif not isinstance(body, dict):
            raise RequestError(f"'{BODY_ARG}' must be a JSON object")
        else:
            body_bytes = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        used.add(BODY_ARG)

    unknown = sorted(set(arguments) - used)
    if unknown:
        raise RequestError(f"unknown argument(s): {', '.join(unknown)}")

    url = base_url.rstrip("/") + path
    if query_pairs:
        from urllib.parse import urlencode

        url += "?" + urlencode(query_pairs)

    if auth is not None:
        headers.update(auth.headers())

    return {"method": method, "url": url, "headers": headers, "body": body_bytes}


class AuthConfig:
    """Authentication injected into outgoing requests from an env variable."""

    def __init__(self, env_var: str, style: str = "bearer", name: str | None = None):
        self.env_var = env_var
        self.style = style
        self.name = name

    def headers(self) -> dict:
        import os

        value = os.environ.get(self.env_var)
        if not value:
            raise RequestError(
                f"environment variable '{self.env_var}' is not set "
                "(required for API authentication)"
            )
        if self.style == "bearer":
            return {"Authorization": f"Bearer {value}"}
        if self.style == "header":
            return {(self.name or "X-API-Key"): value}
        return {}  # query style is applied at URL build time

    def apply_query(self, url: str) -> str:
        if self.style != "query":
            return url
        import os

        value = os.environ.get(self.env_var)
        if not value:
            raise RequestError(f"environment variable '{self.env_var}' is not set")
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{self.name or 'api_key'}={quote(value)}"


def describe_tools(tools: list[dict]) -> str:
    """One-line-per-tool summary used by `mcpify list`."""
    lines = []
    for tool in tools:
        meta = tool["_meta"]
        body = " +body" if meta["has_body"] else ""
        lines.append(
            f"{tool['name']:34} {meta['method']:7} {meta['path']}{body}  {tool['description'][len(meta['method']) + 3:]}"
        )
    return "\n".join(lines)


def input_schema_json(schema: dict) -> str:
    """Compact, stable JSON dump for schemas (without private keys)."""
    return json.dumps({k: v for k, v in schema.items() if not k.startswith("_")}, ensure_ascii=False)
