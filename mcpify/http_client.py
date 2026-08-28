"""Execute built HTTP requests with urllib; never raises on HTTP >= 400."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

_DEBUG = os.environ.get("MCPIFY_DEBUG") == "1"


def _log(level: str, message: str) -> None:
    """Optional stderr logging (MCPIFY_DEBUG=1). NEVER touches stdout:
    the JSON-RPC stream must stay clean. URLs are logged without query
    strings so query-style auth credentials never hit the log."""
    if _DEBUG:
        print(f"{level} mcpify: {message}", file=sys.stderr, flush=True)


def execute(request: dict, timeout: float = 30.0) -> dict:
    """Perform the request and return {status, body, json}.

    HTTP errors (4xx/5xx) are returned as results instead of raising, so
    the agent can see API error payloads and react to them.
    """
    data = request.get("body")
    url_guvenli = request["url"].split("?", 1)[0]
    req = urllib.request.Request(
        request["url"],
        data=data,
        headers=request["headers"],
        method=request["method"],
    )
    basla = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        status = err.code
        raw = err.read().decode("utf-8", "replace")
    except urllib.error.URLError as err:
        _log("ERROR", f"{request['method']} {url_guvenli} -> connection failed: {err.reason}")
        return {
            "status": 0,
            "body": f"connection failed: {err.reason}",
            "json": None,
        }
    sure = time.monotonic() - basla
    if status >= 500:
        _log("ERROR", f"{request['method']} {url_guvenli} -> {status} ({sure:.2f}s)")
    elif status >= 400:
        _log("WARNING", f"{request['method']} {url_guvenli} -> {status} ({sure:.2f}s)")
    else:
        _log("INFO", f"{request['method']} {url_guvenli} -> {status} ({sure:.2f}s)")

    parsed = None
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        parsed = json.loads(raw)
    return {"status": status, "body": raw, "json": parsed}


MAX_RESULT_CHARS = 40_000


def format_result(result: dict) -> tuple[str, bool]:
    """Format an execute() result for an MCP tool response: (text, is_error).

    Oversized bodies are truncated so a single tool call cannot blow the
    client's context window.
    """
    body = result["body"]
    if result["json"] is not None:
        body = json.dumps(result["json"], ensure_ascii=False, indent=2)
    if len(body) > MAX_RESULT_CHARS:
        kesilen = len(body) - MAX_RESULT_CHARS
        body = body[:MAX_RESULT_CHARS] + f"\n… [truncated {kesilen:,} more characters]"
    is_error = result["status"] == 0 or result["status"] >= 400
    if is_error:
        return f"HTTP {result['status']}\n{body}", is_error
    return body, is_error
