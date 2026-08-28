"""Execute built HTTP requests with urllib; never raises on HTTP >= 400."""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request


def execute(request: dict, timeout: float = 30.0) -> dict:
    """Perform the request and return {status, body, json}.

    HTTP errors (4xx/5xx) are returned as results instead of raising, so
    the agent can see API error payloads and react to them.
    """
    data = request.get("body")
    req = urllib.request.Request(
        request["url"],
        data=data,
        headers=request["headers"],
        method=request["method"],
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        status = err.code
        raw = err.read().decode("utf-8", "replace")
    except urllib.error.URLError as err:
        return {
            "status": 0,
            "body": f"connection failed: {err.reason}",
            "json": None,
        }

    parsed = None
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        parsed = json.loads(raw)
    return {"status": status, "body": raw, "json": parsed}


def format_result(result: dict) -> tuple[str, bool]:
    """Format an execute() result for an MCP tool response: (text, is_error)."""
    body = result["body"]
    if result["json"] is not None:
        body = json.dumps(result["json"], ensure_ascii=False, indent=2)
    is_error = result["status"] == 0 or result["status"] >= 400
    if is_error:
        return f"HTTP {result['status']}\n{body}", is_error
    return body, is_error
