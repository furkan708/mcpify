"""`mcpify mock` — a fake API generated from the OpenAPI document.

Point agents, CI or the dashboard at it before the real backend exists:
every documented operation answers with a schema-shaped JSON example
(examples > example > default > const/enum > format > type heuristics).
Stdlib only, like the rest of the tree — and deliberately a *toy*:
stateless, in-memory, no validation theater. It exists so you can wire
up an agent without waiting on another team.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .spec import SpecError, iter_operations, load_spec, resolve_schema

MAX_DEPTH = 6
MAX_OPTIONAL_PROPS = 5


def generate_from_schema(schema: Any, spec: dict[str, Any], key_hint: str = "field", depth: int = 0) -> Any:
    """Deterministic example value for one schema node."""
    if depth > MAX_DEPTH or not isinstance(schema, dict):
        return None
    try:
        schema = resolve_schema(schema, spec)
    except SpecError:
        return None
    if not isinstance(schema, dict):
        return None

    for source in ("examples", "example", "default", "const"):
        value = schema.get(source)
        if value is not None:
            return value[0] if source == "examples" and isinstance(value, list) and value else value
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    fmt = str(schema.get("format", ""))
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((t for t in schema_type if t != "null"), schema_type[0] if schema_type else None)

    if schema_type == "object" or "properties" in schema:
        obj: dict[str, Any] = {}
        for name in schema.get("required", []) or []:
            prop = (schema.get("properties") or {}).get(name)
            obj[name] = generate_from_schema(prop, spec, name, depth + 1)
        optional = [
            name for name in (schema.get("properties") or {})
            if name not in obj
        ][:MAX_OPTIONAL_PROPS]
        for name in optional:
            obj[name] = generate_from_schema(schema["properties"][name], spec, name, depth + 1)
        if not obj and schema.get("additionalProperties") is True:
            obj["example"] = "example"
        return obj
    if schema_type == "array":
        items = generate_from_schema(schema.get("items"), spec, key_hint, depth + 1)
        return [items]
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "boolean":
        return False

    # string heuristics: format first, then the property name
    if fmt == "date-time":
        return "2026-01-01T00:00:00Z"
    if fmt == "date":
        return "2026-01-01"
    if fmt == "email":
        return "user@example.com"
    if fmt == "uuid":
        return "00000000-0000-4000-8000-000000000000"
    if fmt == "uri" or fmt == "url":
        return "https://example.com/example"
    if fmt == "hostname":
        return "example.com"
    if fmt == "ipv4":
        return "127.0.0.1"
    lowered = key_hint.lower()
    if lowered.endswith("id"):
        return "1"
    if "name" in lowered:
        return "Mock name"
    if "description" in lowered or "summary" in lowered:
        return "Mock description."
    if "url" in lowered or "link" in lowered:
        return "https://example.com/example"
    return "mock-" + (key_hint or "value")


def _pick_response(operation: dict[str, Any], spec: dict[str, Any]) -> tuple[int, Any]:
    """(status, schema) for the happiest documented response."""
    responses = operation.get("responses") or {}
    for code in sorted(str(k) for k in responses if str(k).startswith("2")):
        entry = responses.get(code)
        if not isinstance(entry, dict):
            continue
        media = (entry.get("content") or {}).get("application/json") or {}
        schema = media.get("schema")
        return int(code), generate_from_schema(schema, spec)
    first = sorted(responses, key=str)
    if first:
        return int(first[0]), None
    return 200, None


def build_mock_handler(spec: dict[str, Any], delay_seconds: float = 0.0) -> type[BaseHTTPRequestHandler]:
    """Route table from the spec: method + exact path template."""
    routes: list[tuple[str, str, int, Any]] = []
    for method, path, operation in iter_operations(spec):
        status, body = _pick_response(operation, spec)
        routes.append((method.upper(), path, status, body))

    known = sorted({f"{m} {p}" for m, p, _s, _b in routes})

    def _matches(route_path: str, raw_path: str) -> bool:
        """Template match: {param} segments accept anything."""
        route_parts = route_path.strip("/").split("/")
        raw_parts = raw_path.strip("/").split("/")
        if len(route_parts) != len(raw_parts):
            return False
        return all(
            part.startswith("{") or part == raw_part
            for part, raw_part in zip(route_parts, raw_parts, strict=True)
        )

    class MockHandler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _reply(self, status: int, payload: Any) -> None:
            if delay_seconds:
                time.sleep(delay_seconds)
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self) -> None:
            method = self.command.upper()
            raw_path = self.path.split("?", 1)[0]
            for route_method, route_path, status, body in routes:
                if route_method != method or not _matches(route_path, raw_path):
                    continue
                self._reply(status if body is not None else 200,
                            body if body is not None else {"mock": "ok"})
                return
            hint = {"error": f"no mock for {method} {raw_path}", "known_routes": known}
            self._reply(404, hint)

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def do_PUT(self) -> None:
            self._handle()

        def do_PATCH(self) -> None:
            self._handle()

        def do_DELETE(self) -> None:
            self._handle()

    return MockHandler


def build_mock_server(
    spec: dict[str, Any],
    host: str,
    port: int,
    delay_seconds: float = 0.0,
) -> HTTPServer:
    """Bind the mock; the caller serves it (foreground or a test thread)."""
    return HTTPServer((host, port), build_mock_handler(spec, delay_seconds))


def serve_mock(spec_source: str, host: str, port: int, delay_seconds: float = 0.0) -> None:
    """CLI entry: load the spec, serve fake responses until Ctrl+C."""
    spec = load_spec(spec_source)
    httpd = build_mock_server(spec, host, port, delay_seconds)
    bound = httpd.server_address
    shown_host = "127.0.0.1" if str(bound[0]) == "0.0.0.0" else str(bound[0])  # noqa: S104 -- display
    print(
        f"mcpify mock: fake API for {spec_source} at http://{shown_host}:{bound[1]} "
        "(schema-shaped, stateless, Ctrl+C to stop)",
        file=sys.stderr,
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
