"""MCP Streamable HTTP transport for the same ApiServer (stdlib only).

`mcpify serve SPEC --http [HOST:]PORT` exposes the identical tool surface
over HTTP POST instead of stdio, which makes mcpify usable as a shared
team/gateway server. Design, all deliberate and spec-aligned:

- one endpoint, any path: clients POST JSON-RPC to the URL you hand them
- responses are `application/json` (the spec allows JSON or SSE; a
  stateless server has nothing to stream, so SSE is never used)
- GET/DELETE on the endpoint return 405 (no server-initiated stream to
  accept); OPTIONS returns 204 with the allowed methods
- notifications (requests without an id) return 202 Accepted, empty body
- the current MCP spec is stateless: no Mcp-Session-Id is issued or
  required; request state is only the process-wide initialized flag
- batching was removed from the spec and is rejected (-32600); the
  stdio transport still tolerates legacy batched lines
- optional bearer auth via --http-token or MCPIFY_HTTP_TOKEN: when set,
  every POST must carry `Authorization: Bearer <token>` (401 otherwise,
  with WWW-Authenticate). Binding a non-loopback host without a token
  prints a prominent warning — the endpoint is unauthenticated then.
- body cap (default 10 MB) and Content-Length enforcement keep a single
  request from exhausting memory

Errors are layered like the spec intends: transport problems (auth,
size, media type, method) are HTTP status codes; JSON-RPC problems
(parse error, invalid request) are HTTP 200/400 bodies with an error
object so agents can read the code and message.
"""

from __future__ import annotations

import hmac
import http.server
import json
import sys
from socketserver import ThreadingMixIn
from typing import Any, ClassVar

from . import __version__
from .http_client import _log

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_MAX_BODY = 10 * 1024 * 1024  # 10 MB per request, hard cap


class _MCPHandler(http.server.BaseHTTPRequestHandler):
    """Request handler wired to an ApiServer via the factory below."""

    protocol_version = "HTTP/1.1"
    server_version = "mcpify/" + __version__
    mcp_server: Any = None  # set by make_handler
    bearer_token: str | None = None
    max_body: int = DEFAULT_MAX_BODY
    token_scopes: ClassVar[dict[str, dict[str, Any]]] = {}  # bearer -> compiled scopes

    def log_message(self, fmt: str, *args: Any) -> None:
        _log("INFO", f"http: {self.address_string()} {fmt % args}")

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, payload: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> None:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _reject(self, status: int, reason: str, rpc_error: int | None = None) -> None:
        # the request body was not (fully) consumed; keep-alive would
        # desynchronize the stream, so close after the response
        self.close_connection = True
        payload = None
        extra = {"Connection": "close"}
        if rpc_error is not None:
            payload = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": rpc_error, "message": reason},
            }
        self._send(status, payload, extra)

    def _bearer_value(self) -> str:
        header = self.headers.get("Authorization", "")
        return header[7:] if header.startswith("Bearer ") else ""

    def _authorized(self) -> bool:
        token = type(self).bearer_token
        scopes = type(self).token_scopes
        if token is None and not scopes:
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        value = value.strip()
        if scopes:
            return scheme.lower() == "bearer" and value in scopes
        return scheme.lower() == "bearer" and hmac.compare_digest(value, token or "")

    # -- methods -----------------------------------------------------------
    def do_POST(self) -> None:
        if not self._authorized():
            self._send(
                401,
                {"error": "unauthorized: send 'Authorization: Bearer <token>'"},
                {"WWW-Authenticate": "Bearer"},
            )
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._reject(415, "Content-Type must be application/json", -32600)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reject(411, "Content-Length is required", -32600)
            return
        if length < 0:
            self._reject(400, "invalid Content-Length", -32600)
            return
        if length > type(self).max_body:
            self._reject(413, f"request body exceeds {type(self).max_body} bytes", -32600)
            return
        raw = self.rfile.read(length) if length else b""
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reject(400, "parse error: request body is not valid JSON", -32700)
            return
        if isinstance(decoded, list):
            self._reject(400, "batching was removed from the MCP spec; send one message per request", -32600)
            return
        if not isinstance(decoded, dict):
            self._reject(400, "invalid request: expected a JSON-RPC message object", -32600)
            return
        response = type(self).mcp_server.handle_message(decoded)
        if response is None:
            self._send(202)  # notification: accepted, nothing to say
            return
        scopes = type(self).token_scopes.get(self._bearer_value()) if type(self).token_scopes else None
        if scopes:
            response = _apply_scopes(response, decoded, scopes)
        self._send(200, response)

    def do_GET(self) -> None:
        self._send(
            405,
            {"error": "GET is not supported: this stateless server has no SSE stream; POST JSON-RPC messages"},
            {"Allow": "POST, OPTIONS"},
        )

    def do_DELETE(self) -> None:
        self._send(
            405,
            {"error": "DELETE is not supported: this server issues no session id"},
            {"Allow": "POST, OPTIONS"},
        )

    def do_OPTIONS(self) -> None:
        self._send(204, None, {"Allow": "POST, OPTIONS"})


def parse_http_bind(value: str) -> tuple[str, int]:
    """Parse the --http value: "8080" binds 127.0.0.1:8080, ":8080" or
    "*:8080" binds all interfaces, "host:port" binds that host. Raises
    ValueError with a user-facing message."""
    text = value.strip()
    if ":" in text:
        host, _, port_text = text.rpartition(":")
    else:
        host, port_text = "127.0.0.1", text
    if host in ("", "*"):
        host = "0.0.0.0"  # noqa: S104 — konteyner/preview gorunurlugu bilerek
    if host not in ("127.0.0.1", "localhost", "0.0.0.0", "::") and not _is_host(host):  # noqa: S104
        raise ValueError(f"'{host}' does not look like a hostname or IP")
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"port must be an integer, got '{port_text}'") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"port must be 1-65535, got {port}")
    return host, port


def _is_host(host: str) -> bool:
    """Loose sanity check: hostname chars, dots, dashes, or an IPv6 literal."""
    import re

    if host.startswith("[") and host.endswith("]"):
        return bool(re.fullmatch(r"[0-9a-fA-F:.]+", host[1:-1]))
    return bool(re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]*)", host))


def _apply_scopes(response: dict[str, Any], request: dict[str, Any],
                  scopes: dict[str, Any]) -> dict[str, Any]:
    """Scope a tools/list result; gate tools/call on the target tool."""
    method = request.get("method", "")
    request_id = request.get("id")
    if method == "tools/list":
        result = response.get("result") or {}
        tools = result.get("tools") or []
        result["tools"] = [tool for tool in tools if tool_allowed(scopes, str(tool.get("name", "")))]
        result["totalTools"] = len(result["tools"])
        return {**response, "result": result}
    if method == "tools/call":
        name = str(((request.get("params") or {}).get("name")) or "")
        if not tool_allowed(scopes, name):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32602,
                    "message": f"tool '{name}' is not permitted for token '{scopes['name']}'",
                },
            }
    return response


def compile_token_scopes(
    entries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate + compile a token -> {allow, deny} regex map.

    ``entries`` comes from the [tokens.NAME] tables of a token file:
    {"token": str, "allow": [regex...], "deny": [regex...]}. At least
    one allow pattern is required — a token with no allow list would be
    either dead weight or (worse) accidentally unlimited."""
    import re as _re

    compiled: dict[str, dict[str, Any]] = {}
    seen_tokens: dict[str, str] = {}
    for name, entry in entries.items():
        token = str(entry.get("token", ""))
        if not token:
            raise ValueError(f"tokens.{name}: missing 'token'")
        if token in seen_tokens:
            raise ValueError(f"tokens.{name}: duplicate token of {seen_tokens[token]}")
        seen_tokens[token] = name
        allow = entry.get("allow") or []
        deny = entry.get("deny") or []
        if not allow:
            raise ValueError(
                f"tokens.{name}: at least one 'allow' pattern is required "
                "(a scope-less token is what --http-token is for)"
            )
        compiled[token] = {
            "name": name,
            "allow": [_re.compile(str(pattern)) for pattern in allow],
            "deny": [_re.compile(str(pattern)) for pattern in deny],
        }
    return compiled


def tool_allowed(scopes: dict[str, Any], tool_name: str) -> bool:
    """Deny wins over allow (same rule as the path policy layer)."""
    if any(pattern.search(tool_name) for pattern in scopes["deny"]):
        return False
    return any(pattern.search(tool_name) for pattern in scopes["allow"])


def make_handler(server: Any, token: str | None = None, max_body: int = DEFAULT_MAX_BODY,
                 token_scopes: dict[str, dict[str, Any]] | None = None) -> type:
    """Bind an ApiServer (and optional bearer token) into a handler class.

    A fresh class object per server instance keeps tests and multi-server
    processes independent — class attributes are the injection point."""
    return type(
        "BoundMCPHandler",
        (_MCPHandler,),
        {"mcp_server": server, "bearer_token": token, "max_body": max_body,
         "token_scopes": token_scopes or {}},
    )


class _ThreadingHTTPServer(ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_http_server(server: Any, host: str, port: int, token: str | None = None,
                      max_body: int = DEFAULT_MAX_BODY,
                      token_scopes: dict[str, dict[str, Any]] | None = None) -> _ThreadingHTTPServer:
    """Construct (not start) the threaded HTTP server. Separated from
    serve_http so tests can bind port 0, read the real port, and drive
    requests while serve_http() remains the blocking production entry."""
    handler = make_handler(server, token, max_body, token_scopes)
    return _ThreadingHTTPServer((host, port), handler)


def serve_http(server: Any, host: str, port: int, token: str | None = None,
               max_body: int = DEFAULT_MAX_BODY,
               token_scopes: dict[str, dict[str, Any]] | None = None) -> None:
    """Run the HTTP transport until Ctrl+C (the `--http` entry)."""
    if token_scopes and token is None:
        # scoped tokens carry their own secrets; a shared --http-token would defeat them.
        # sentinel: _authorized checks token_scopes membership, this value is never compared
        token = "__mcpify_scoped__"  # noqa: S105 - sentinel, not a credential
    httpd = build_http_server(server, host, port, token, max_body, token_scopes)
    bound_host = str(httpd.server_address[0])
    bound_port = int(httpd.server_address[1])
    if host not in ("127.0.0.1", "localhost") and token is None:
        print(
            "WARNING: binding a non-loopback address WITHOUT --http-token — "
            "anyone who can reach this port can call the API through mcpify. "
            "Set --http-token or MCPIFY_HTTP_TOKEN to require a bearer token.",
            file=sys.stderr,
            flush=True,
        )
    print(
        f"mcpify: HTTP endpoint http://{bound_host}:{bound_port} "
        + ("(bearer auth required)" if token else "(no auth)"),
        file=sys.stderr,
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nmcpify: HTTP server stopped", file=sys.stderr)
    finally:
        httpd.server_close()
