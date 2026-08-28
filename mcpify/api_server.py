"""MCP (Model Context Protocol) stdio server that exposes an OpenAPI spec.

Specks newline-delimited JSON-RPC 2.0 over stdio, as used by MCP stdio
transports. Every OpenAPI operation becomes an MCP tool that performs a
real HTTP call against the configured base URL.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .http_client import execute, format_result
from .tools import AuthConfig, RequestError, build_request, spec_to_tools
from .tools import BODY_ARG

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "mcpify"
SERVER_VERSION = "1.0.0"


class ApiServer:
    """MCP handler backed by one OpenAPI specification."""

    def __init__(
        self,
        spec: dict,
        base_url: str,
        server_name: str = "mcpify",
        auth: AuthConfig | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.spec = spec
        self.base_url = base_url
        self.server_name = server_name
        self.auth = auth
        self.timeout = timeout
        self.tools = spec_to_tools(spec)
        self.by_name = {tool["name"]: tool for tool in self.tools}

    # -- public API used by the CLI --------------------------------------
    @property
    def tool_count(self) -> int:
        return len(self.tools)

    def public_tools(self) -> list[dict]:
        return [
            {k: v for k, v in tool.items() if not k.startswith("_")}
            for tool in self.tools
        ]

    def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        tool = self.by_name.get(name)
        if tool is None:
            raise KeyError(name)
        request = build_request(self.base_url, tool["_meta"], arguments, self.auth)
        if self.auth is not None:
            request["url"] = self.auth.apply_query(request["url"])
        result = execute(request, timeout=self.timeout)
        return format_result(result)

    # -- MCP plumbing -----------------------------------------------------
    def _result(self, request_id, payload):
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}

    def _error(self, request_id, code: int, message: str):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _text(self, text: str, is_error: bool = False) -> dict:
        payload = {"content": [{"type": "text", "text": text}]}
        if is_error:
            payload["isError"] = True
        return payload

    def handle_message(self, message: dict) -> dict | None:
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
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": self.public_tools()})
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            try:
                text, is_error = self.call_tool(name, arguments)
            except KeyError:
                return self._error(request_id, -32601, f"unknown tool: {name}")
            except RequestError as err:
                return self._result(request_id, self._text(str(err), is_error=True))
            return self._result(request_id, self._text(text, is_error=is_error))
        return self._error(request_id, -32601, f"method not found: {method}")

    def serve(self, stdin=None, stdout=None) -> None:
        input_stream = stdin if stdin is not None else sys.stdin
        output_stream = stdout if stdout is not None else sys.stdout
        for line in input_stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                response = self._error(None, -32700, "parse error")
            else:
                response = self.handle_message(message)
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
                output_stream.flush()


def serve(
    spec_path: str,
    base_url: str,
    name: str = "mcpify",
    auth: AuthConfig | None = None,
    timeout: float = 30.0,
) -> None:
    """Load the spec and block on the stdio loop (the `mcpify serve` entry)."""
    from .spec import load_spec

    spec = load_spec(spec_path)
    ApiServer(spec, base_url, server_name=name, auth=auth, timeout=timeout).serve()
