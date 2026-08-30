"""MCP (Model Context Protocol) stdio server that exposes an OpenAPI spec.

Speaks newline-delimited JSON-RPC 2.0 over stdio, as used by MCP stdio
transports. Every OpenAPI operation becomes an MCP tool that performs a
real HTTP call against the configured base URL.

Agent-grade surface:
- structured output: tools whose spec documents a 2xx JSON body declare
  outputSchema and return structuredContent (plus the back-compat text)
- remediation: HTTP errors carry corrective guidance, not just the body
- lazy mode (--lazy): three meta-tools (search / schema / call) replace
  the full listing so a 500-endpoint API costs ~3 tool definitions
- preview (--enable-preview): mcpify_preview_request shows the exact
  request that would be sent — a dry run, with credentials masked
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import __version__ as SERVER_VERSION  # never hardcode: avoids version drift
from . import audit, metrics, otel
from .convert import convert as convert_format
from .http_client import (
    OAuth2ClientCredentials,
    RateLimiter,
    ResponseCache,
    execute,
    format_result,
    project_json,
    redact_json,
    remediation,
)
from .tools import (
    META_TOOL_NAMES,
    AuthConfig,
    RequestError,
    build_request,
    spec_to_tools,
    tool_cost_tokens,
)

# Anything that can inject credentials into outgoing requests: the static
# env-var credential or the OAuth2 client-credentials flow (duck-typed
# interface: headers/apply_query/describe).
AuthProvider = AuthConfig | OAuth2ClientCredentials

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mcpify"

# Meta tools are namespaced with an explicit prefix so a spec can never
# collide with them (a spec defining operationId "search_tools" stays
# callable under its own name).
SEARCH_TOOL = "mcpify_search_tools"
SCHEMA_TOOL = "mcpify_get_tool_schema"
CALL_TOOL = "mcpify_call_tool"
PREVIEW_TOOL = "mcpify_preview_request"
INVALIDATE_TOOL = "mcpify_cache_invalidate"
HEALTH_TOOL = "mcpify_health"


def _public(tool: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in tool.items() if not k.startswith("_")}


def _annotations(read_only: bool, open_world: bool, destructive: bool, idempotent: bool, title: str) -> dict[str, Any]:
    out: dict[str, Any] = {"title": title}
    out.update(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )
    return out


class ApiServer:
    """MCP handler backed by one OpenAPI specification."""

    def __init__(
        self,
        spec: dict[str, Any],
        base_url: str,
        server_name: str = "mcpify",
        auth: AuthProvider | None = None,
        timeout: float = 30.0,
        tools: list[dict[str, Any]] | None = None,
        lazy: bool = False,
        enable_preview: bool = False,
        cache_ttl: float = 0.0,
        retry: int = 0,
        retry_delay: float = 1.0,
        response_format: str = "auto",
        wait_on_429: float = 0.0,
        write_auth: AuthProvider | None = None,
        fields: frozenset[str] | None = None,
        redact: frozenset[str] | None = None,
        rate_limit: float | None = None,
    ) -> None:
        self.spec = spec
        self.base_url = base_url
        self.server_name = server_name
        self.auth: AuthProvider | None = auth
        # dedicated non-GET credential (--write-auth-env): reads keep the
        # primary identity, writes carry their own — structural least
        # privilege instead of one shared API identity
        self.write_auth: AuthProvider | None = write_auth
        # response projection (--fields): successful JSON responses keep
        # only the requested fields — token cuts without losing the data
        # you asked for
        self.fields = fields
        # response redaction (--redact): values whose KEY names a secret
        # are masked at every level — the model never sees them
        self.redact = redact
        # client-side courtesy throttle (--rate-limit RPS): one limiter
        # per upstream; every execute() waits for a slot before dialing
        self.rate_limiter = RateLimiter(rate_limit) if rate_limit else None
        self.timeout = timeout
        self.lazy = lazy
        # tools may be pre-filtered by the CLI policy layer; building them
        # here keeps direct construction (tests, embedders) working.
        self.tools = tools if tools is not None else spec_to_tools(spec)
        self.by_name = {tool["name"]: tool for tool in self.tools}
        shadowed = sorted(set(self.by_name) & META_TOOL_NAMES)
        if shadowed:
            # impossible via spec_to_tools (names are claimed there); this
            # guards hand-fed tool lists (embedders, future callers)
            raise ValueError(
                "tool names collide with reserved meta tools: " + ", ".join(shadowed)
            )
        self.known_paths = sorted({tool["_meta"]["path"] for tool in self.tools})
        self.cache = ResponseCache(cache_ttl) if cache_ttl and cache_ttl > 0 else None
        self.retry = retry
        self.retry_delay = retry_delay
        self.wait_on_429 = wait_on_429
        self.response_format = response_format
        # dashboard config-form defaults ([serve] values when run via CLI)
        self.ui_config_defaults: dict[str, Any] = {}
        self.meta_tools: dict[str, dict[str, Any]] = {t["name"]: t for t in self._build_meta_tools()}
        if lazy:
            listed = [SEARCH_TOOL, SCHEMA_TOOL, CALL_TOOL]
        else:
            listed = [tool["name"] for tool in self.tools]
        self.request_hooks: list[Callable[[dict[str, Any]], dict[str, Any] | None]] = []
        self.result_hooks: list[Callable[[dict[str, Any]], dict[str, Any] | None]] = []
        if enable_preview:
            listed.append(PREVIEW_TOOL)
        if self.cache is not None:
            listed.append(INVALIDATE_TOOL)
        listed.append(HEALTH_TOOL)
        self.listed_names = listed
        self._initialized = False

    # -- meta tool descriptors -------------------------------------------
    def _build_meta_tools(self) -> list[dict[str, Any]]:
        search = {
            "name": SEARCH_TOOL,
            "description": (
                "Search the API's tools by keyword (matches names, paths, "
                "summaries and tags). Returns compact entries — use "
                f"{SCHEMA_TOOL} for a full schema before calling."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "space-separated keywords"},
                    "tag": {"type": "string", "description": "exact tag filter (case-insensitive)"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
                },
                "required": [],
            },
            "annotations": _annotations(True, False, False, True, "Search API tools"),
            "_local": True,
        }
        schema_tool = {
            "name": SCHEMA_TOOL,
            "description": (
                "Get the full definition of one tool: input schema, annotations "
                "and output schema. Call this before invoking a tool the first time."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "tool name from search"}},
                "required": ["name"],
            },
            "annotations": _annotations(True, False, False, True, "Get tool schema"),
            "_local": True,
        }
        call = {
            "name": CALL_TOOL,
            "description": (
                "Execute one of the API's tools by name with the given arguments. "
                "Honest hints: this can reach write and destructive endpoints — "
                "check the target tool's annotations (from "
                f"{SCHEMA_TOOL}) before calling."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "tool name from search"},
                    "arguments": {"type": "object", "description": "arguments for the target tool"},
                },
                "required": ["name"],
            },
            "annotations": _annotations(False, True, True, False, "Execute API tool"),
            "_local": True,
        }
        preview = {
            "name": PREVIEW_TOOL,
            "description": (
                "Dry run: show the exact HTTP request (method, URL, headers, "
                "body) that a tool call would produce — nothing is sent. "
                "Credentials are masked."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "tool name"},
                    "arguments": {"type": "object", "description": "arguments to preview"},
                },
                "required": ["name"],
            },
            "annotations": _annotations(True, False, False, True, "Preview request"),
            "_local": True,
        }
        health = {
            "name": HEALTH_TOOL,
            "description": (
                "Check that the upstream API is reachable and report this "
                "server's own configuration (tool count, cache, retry, auth)."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "annotations": _annotations(True, False, False, True, "Health check"),
            "_local": True,
        }
        invalidate = {
            "name": INVALIDATE_TOOL,
            "description": (
                "Clear cached GET responses. With no arguments clears the whole "
                "cache; pass 'path' to drop only entries whose URL contains it. "
                "Only available when response caching is enabled."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "drop only matching entries"}},
                "required": [],
            },
            "annotations": _annotations(False, False, True, True, "Clear response cache"),
            "_local": True,
        }
        return [search, schema_tool, call, preview, health, invalidate]

    # -- public API used by the CLI --------------------------------------
    @property
    def tool_count(self) -> int:
        return len(self.tools)

    def public_tools(self) -> list[dict[str, Any]]:
        lookup = {**self.by_name, **self.meta_tools}
        return [_public(lookup[name]) for name in self.listed_names]

    # -- tooling bridges (dashboard, reload) --------------------------------
    def preview_request(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Public dry-run for tooling: the masked request a call would send."""
        return self._preview({"name": name, "arguments": arguments})

    def run_health_check(self) -> dict[str, Any]:
        """Public health probe for tooling; also refreshes metrics gauges."""
        return self._health()

    def reload_tools(self, tools: list[dict[str, Any]]) -> None:
        """Swap the tool surface in place (hot reload); keeps identity so
        stdio loops and HTTP handler closures keep working."""
        self.tools = tools
        self.by_name = {tool["name"]: tool for tool in self.tools}
        self.known_paths = sorted({tool["_meta"]["path"] for tool in self.tools})

    # -- tool execution ----------------------------------------------------
    def run_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute any listed tool and return a full MCP tool-result payload."""
        if name in self.meta_tools:
            if name not in self.listed_names:
                raise KeyError(name)  # not listed -> not callable
            return self._run_meta(name, arguments)
        tool = self.by_name.get(name)
        if tool is None:
            raise KeyError(name)
        if self.lazy:
            raise RequestError(
                f"'{name}' is not listed (lazy mode) — search with {SEARCH_TOOL}, "
                f"inspect with {SCHEMA_TOOL}, then call via {CALL_TOOL}."
            )
        return self._execute_real(tool, arguments)

    def _context_for(self, tool: dict[str, Any]) -> dict[str, Any]:
        """Execution context for one tool: base URL, auth and tuning.

        A plain ApiServer has exactly one context (its own); the
        aggregator overrides this to route each tool to the API that
        owns it. Every execution path (_execute_real, preview) goes
        through here, so per-API auth/cache/retry can never be bypassed.
        With a write credential configured (--write-auth-env), non-GET
        calls carry THAT identity instead of the primary one."""
        auth = self.auth
        if self.write_auth is not None and tool["_meta"]["method"] != "GET":
            auth = self.write_auth
        return {
            "base": self.base_url,
            "auth": auth,
            "timeout": self.timeout,
            "cache": self.cache,
            "retry": self.retry,
            "retry_delay": self.retry_delay,
            "wait_on_429": self.wait_on_429,
            "fields": self.fields,
            "redact": self.redact,
            "rate_limiter": self.rate_limiter,
        }

    def _send(self, tool: dict[str, Any], arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        auth = context["auth"]
        request = build_request(context["base"], tool["_meta"], arguments, auth)
        if auth is not None:
            request["url"] = auth.apply_query(request["url"])
        for hook in self.request_hooks:
            with contextlib.suppress(Exception):  # eklenti hatasi servisi durdurmaz
                request = hook(request) or request
        started = time.monotonic()
        span = otel.trace_call(tool["name"], str(tool.get("api", self.server_name)))
        result = execute(
            request,
            timeout=context["timeout"],
            cache=context["cache"],
            retry=context["retry"],
            retry_delay=context["retry_delay"],
            wait_on_429=context["wait_on_429"],
            rate_limit=context.get("rate_limiter"),
        )
        # OAuth2 self-heal: a token that expired server-side (or was
        # revoked mid-flight) surfaces as 401 once. Drop the cached
        # token, rebuild the request with a fresh one, and retry a
        # single time — a second 401 is a real authorization problem
        # and is reported as such.
        if result["status"] == 401 and isinstance(auth, OAuth2ClientCredentials):
            auth.invalidate()
            request = build_request(context["base"], tool["_meta"], arguments, auth)
            result = execute(
                request,
                timeout=context["timeout"],
                cache=context["cache"],
                retry=context["retry"],
                retry_delay=context["retry_delay"],
                wait_on_429=context["wait_on_429"],
                rate_limit=context.get("rate_limiter"),
            )
        latency = time.monotonic() - started
        span.set_status(200 <= result["status"] < 400, f"HTTP {result['status']}")
        span.finish(latency)
        audit.record(tool["name"], str(tool.get("api", self.server_name)),
                     int(result["status"]), latency, arguments)
        for hook in self.result_hooks:
            with contextlib.suppress(Exception):  # eklenti hatasi servisi durdurmaz
                result = hook(result) or result
        wanted = context.get("fields")
        if wanted and 200 <= result["status"] < 400 and result.get("json") is not None:
            projected = project_json(result["json"], wanted)
            result = {**result, "json": projected,
                      "body": json.dumps(projected, ensure_ascii=False)}
        secrets = context.get("redact")
        if secrets and result.get("json") is not None:
            # runs on success AND error bodies, after projection: whatever
            # the model is about to read, secrets never survive to it
            masked = redact_json(result["json"], secrets)
            result = {**result, "json": masked,
                      "body": json.dumps(masked, ensure_ascii=False)}
        return result

    def _metric_labels(self, tool: dict[str, Any]) -> dict[str, str]:
        return {"tool": tool["name"], "api": str(tool.get("api", self.server_name))}

    def _execute_real(self, tool: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        result = self._send(tool, arguments, self._context_for(tool))
        metrics.observe(
            "mcpify_tool_latency_seconds", self._metric_labels(tool), time.monotonic() - started
        )
        outcome = "error" if (result["status"] == 0 or result["status"] >= 400) else "ok"
        metrics.inc("mcpify_tool_calls_total", {**self._metric_labels(tool), "outcome": outcome})
        return self._payload_for(tool, result)

    def _payload_for(self, tool: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        status = result["status"]
        if status == 0 or status >= 400:
            text, _ = format_result(result)
            extra = remediation(result, tool, self.known_paths)
            return self._text(text + extra, is_error=True)

        if self.response_format in ("auto", "xml"):
            content_type = (result.get("headers") or {}).get("Content-Type", "")
            text, converted = convert_format(result["body"], result["json"], content_type, self.response_format)
            if converted is not None and converted is not result["json"]:
                result = {**result, "body": text, "json": converted}

        schema = tool.get("outputSchema")
        if schema is not None:
            # Structured output is a declared promise: success must carry
            # structuredContent. A non-JSON body breaks that contract, so
            # it is reported as a tool error (spec exempts isError results
            # from output validation).
            if result["json"] is None:
                excerpt = result["body"][:500]
                return self._text(
                    f"HTTP {status}\nThis tool declares a JSON response, but the API "
                    f"returned a non-JSON body:\n{excerpt}",
                    is_error=True,
                )
            text, _ = format_result(result)  # serialized JSON, truncated for context safety
            return {
                "content": [{"type": "text", "text": text}],
                "structuredContent": result["json"],
            }
        text, is_error = format_result(result)
        return self._text(text, is_error=is_error)

    def _run_meta(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == SEARCH_TOOL:
            return self._search(arguments)
        if name == SCHEMA_TOOL:
            return self._get_schema(arguments)
        if name == CALL_TOOL:
            return self._lazy_call(arguments)
        if name == PREVIEW_TOOL:
            return self._preview(arguments)
        if name == HEALTH_TOOL:
            return self._health()
        if name == INVALIDATE_TOOL:
            raw = arguments.get("path")
            pattern = str(raw) if raw else None  # no path -> clear everything
            cleared = self.cache.invalidate(pattern) if self.cache is not None else 0
            return self._text(json.dumps(
                {"cleared": cleared, "cache_ttl": self.cache.ttl if self.cache else 0},
                ensure_ascii=False))
        raise KeyError(name)  # unreachable: meta_tools keys are exactly these

    def _health(self) -> dict[str, Any]:
        import time as _time

        started = _time.monotonic()
        probe = execute(
            {"method": "GET", "url": self.base_url.rstrip("/") + "/", "headers": {"Accept": "application/json"}, "body": None},
            timeout=min(self.timeout, 10.0),
            retry=self.retry,
            retry_delay=self.retry_delay,
        )
        latency = _time.monotonic() - started
        reachable = probe["status"] != 0
        metrics.health_report(self.server_name, reachable)
        auth = self.auth.describe() if self.auth is not None else None
        report = {
            "api_reachable": reachable,
            "api_status": probe["status"],
            "latency_seconds": round(latency, 3),
            "base_url": self.base_url,
            "tools": len(self.tools),
            "cache_ttl": self.cache.ttl if self.cache else 0,
            "retry": self.retry,
            "format": self.response_format,
            "auth": auth,
        }
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if reachable:
            return self._text(text)
        report["hint"] = "the API did not answer — check base URL, network, or --timeout"
        return self._text(json.dumps(report, ensure_ascii=False, indent=2), is_error=True)

    # -- lazy mode internals ---------------------------------------------
    @staticmethod
    def _score(query: str, tool: dict[str, Any]) -> int:
        """Deterministic keyword score: every token must match somewhere."""
        meta = tool["_meta"]
        haystack = " ".join(
            [tool["name"], meta["path"], tool["description"], " ".join(meta["tags"]),
             str(tool.get("api", ""))]
        ).lower()
        tokens = [t for t in re.split(r"[^a-z0-9]+", query.lower()) if t]
        if not tokens:
            return 0
        hits = sum(1 for t in tokens if t in haystack)
        if hits < len(tokens):
            return hits if hits >= len(tokens) - 1 else 0
        score = len(tokens)
        if all(t in tool["name"].lower() for t in tokens):
            score += 3
        if query.lower() in meta["path"]:
            score += 2
        return score

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        tag = str(arguments.get("tag") or "").strip().lower()
        limit = arguments.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise RequestError("'limit' must be a positive integer (1-25)")
        limit = min(limit, 25)
        scored: list[tuple[int, dict[str, Any]]] = []
        for tool in self.tools:
            meta = tool["_meta"]
            if tag and not any(entry.lower() == tag for entry in meta["tags"]):
                continue
            score = self._score(query, tool)
            if query and score == 0:
                continue
            scored.append((score, tool))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
        top = scored[:limit]
        entries = [
            {
                "name": tool["name"],
                "method": tool["_meta"]["method"],
                "path": tool["_meta"]["path"],
                "summary": tool["description"],
                "tags": tool["_meta"]["tags"],
                "readOnly": bool(tool.get("annotations", {}).get("readOnlyHint")),
                "hasOutputSchema": "outputSchema" in tool,
                # what this tool's full schema would add to context when
                # pulled via get_tool_schema — search stays cheap, the
                # price of the pull is visible BEFORE pulling
                "cost_tokens": tool_cost_tokens(tool),
            }
            for _, tool in top
        ]
        total = len(scored)
        suffix = f" ({total} total matches)" if total > len(entries) else ""
        pull = sum(entry["cost_tokens"] for entry in entries)
        text = json.dumps(entries, ensure_ascii=False, indent=2)
        header = (f"{len(entries)} tool(s){suffix} — full schemas cost ~{pull} tokens "
                  f"total; pull only what you need with {SCHEMA_TOOL}")
        return self._text(f"{header}\n{text}")

    def _resolve_target(self, arguments: dict[str, Any], caller: str) -> dict[str, Any]:
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RequestError(f"'name' is required (the tool to {caller})")
        tool = self.by_name.get(name)
        if tool is None:
            import difflib

            close = difflib.get_close_matches(name, list(self.by_name), n=3, cutoff=0.4)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise RequestError(f"unknown tool '{name}'.{hint}")
        return tool

    def _get_schema(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._resolve_target(arguments, "inspect")
        return self._text(json.dumps(_public(tool), ensure_ascii=False, indent=2))

    def _lazy_call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._resolve_target(arguments, "call")
        inner = arguments.get("arguments")
        if inner is None:
            inner = {}
        if not isinstance(inner, dict):
            raise RequestError("'arguments' must be an object")
        return self._execute_real(tool, inner)

    # -- preview (dry run) -------------------------------------------------
    def _mask_request(self, request: dict[str, Any], auth: AuthConfig | OAuth2ClientCredentials | None = None) -> dict[str, Any]:
        auth = auth if auth is not None else self.auth
        headers: dict[str, Any] = {}
        for key, value in request["headers"].items():
            if key.lower() == "authorization":
                scheme = value.split(" ", 1)[0]
                headers[key] = f"{scheme} ***"
            elif isinstance(auth, AuthConfig) and auth.style == "header" and key == (
                auth.name or "X-API-Key"
            ):
                headers[key] = "***"
            else:
                headers[key] = value
        url = request["url"]
        if isinstance(auth, AuthConfig) and auth.style == "query":
            name = auth.name or "api_key"
            url = re.sub(rf"([?&]{re.escape(name)}=)[^&]*", r"\1***", url)
        return {**request, "headers": headers, "url": url}

    def _preview(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._resolve_target(arguments, "preview")
        inner = arguments.get("arguments") or {}
        if not isinstance(inner, dict):
            raise RequestError("'arguments' must be an object")
        context = self._context_for(tool)
        request = build_request(context["base"], tool["_meta"], inner, context["auth"])
        if context["auth"] is not None:
            request["url"] = context["auth"].apply_query(request["url"])
        masked = self._mask_request(request, context["auth"])
        lines = [f"{masked['method']} {masked['url']}"]
        for key, value in masked["headers"].items():
            lines.append(f"{key}: {value}")
        if masked["body"] is not None:
            body = masked["body"].decode("utf-8", "replace")
            if len(body) > 2000:
                body = body[:2000] + " …[truncated]"
            lines.append(f"\nbody:\n{body}")
        lines.append("\n(dry run — nothing was sent)")
        return self._text("\n".join(lines))

    # -- MCP plumbing -----------------------------------------------------
    def _result(self, request_id: int | str | None, payload: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}

    def _error(self, request_id: int | str | None, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _text(self, text: str, is_error: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if is_error:
            payload["isError"] = True
        return payload

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method", "")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            requested = params.get("protocolVersion", PROTOCOL_VERSION)
            return self._result(
                request_id,
                {
                    "protocolVersion": requested,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.server_name, "version": SERVER_VERSION},
                },
            )
        if method == "notifications/initialized":
            self._initialized = True
            return None
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return self._result(request_id, {})
        # MCP 2026-07-28 made the protocol stateless: the initialize handshake
        # is gone and every request carries its version in _meta. Accept those
        # requests as pre-authorized; classic (2025-06-18) clients keep using
        # the handshake. One path per client generation, both served here.
        request_meta = params.get("_meta")
        if isinstance(request_meta, dict) and "io.modelcontextprotocol/protocolVersion" in request_meta:
            self._initialized = True
        if method in ("tools/list", "tools/call") and not self._initialized:
            # MCP lifecycle: legacy requests before initialization completes must fail
            return self._error(request_id, -32002, "Server not initialized: send initialize and notifications/initialized first")
        if method == "tools/list":
            return self._result(request_id, {"tools": self.public_tools()})
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            try:
                payload = self.run_tool(name, arguments)
            except KeyError:
                return self._error(request_id, -32601, f"unknown tool: {name}")
            except RequestError as err:
                return self._result(request_id, self._text(str(err), is_error=True))
            return self._result(request_id, payload)
        return self._error(request_id, -32601, f"method not found: {method}")

    def serve(self, stdin: Any = None, stdout: Any = None) -> None:
        input_stream = stdin if stdin is not None else sys.stdin
        output_stream = stdout if stdout is not None else sys.stdout
        for line in input_stream:
            line = line.strip()
            if not line:
                continue
            decoded: Any
            response: dict[str, Any] | None
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                response = self._error(None, -32700, "parse error")
                output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
                output_stream.flush()
                continue
            if isinstance(decoded, list):
                for item in self._handle_batch(decoded):
                    if item is not None:
                        output_stream.write(json.dumps(item, ensure_ascii=False) + "\n")
                        output_stream.flush()
                continue
            response = self.handle_message(decoded)
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
                output_stream.flush()

    def _handle_batch(self, items: list[Any]) -> list[Any]:
        """Legacy JSON-RPC batching: an array of requests on one line.

        The current MCP spec removed batching, but gateways still emit
        it; tolerate it. Notifications are processed in order first
        (state must be settled), then request calls — all tools/call
        entries run concurrently in a small thread pool (safe: the cache
        is locked, execute() is per-call state).
        """
        notifications = [item for item in items if isinstance(item, dict) and str(item.get("method", "")).startswith("notifications/")]
        requests = [item for item in items if isinstance(item, dict) and not str(item.get("method", "")).startswith("notifications/")]
        for item in notifications:
            self.handle_message(item)
        if not requests:
            return []
        if len(requests) == 1 or not all(item.get("method") == "tools/call" for item in requests):
            return [self.handle_message(item) for item in requests]
        with ThreadPoolExecutor(max_workers=min(8, len(requests))) as pool:
            return list(pool.map(self.handle_message, requests))


def serve(
    spec_path: str,
    base_url: str,
    name: str = "mcpify",
    auth: AuthProvider | None = None,
    timeout: float = 30.0,
    tools: list[dict[str, Any]] | None = None,
    lazy: bool = False,
    enable_preview: bool = False,
    cache_ttl: float = 0.0,
    retry: int = 0,
    retry_delay: float = 1.0,
    response_format: str = "auto",
    wait_on_429: float = 0.0,
) -> None:
    """Load the spec and block on the stdio loop (the `mcpify serve` entry)."""
    from .spec import load_spec

    spec = load_spec(spec_path)
    ApiServer(
        spec,
        base_url,
        server_name=name,
        auth=auth,
        timeout=timeout,
        tools=tools,
        lazy=lazy,
        enable_preview=enable_preview,
        cache_ttl=cache_ttl,
        retry=retry,
        retry_delay=retry_delay,
        response_format=response_format,
        wait_on_429=wait_on_429,
    ).serve()
