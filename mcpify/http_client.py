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
            headers = {k: v for k, v in response.headers.items()}
    except urllib.error.HTTPError as err:
        status = err.code
        raw = err.read().decode("utf-8", "replace")
        headers = {k: v for k, v in err.headers.items()} if err.headers else {}
    except urllib.error.URLError as err:
        _log("ERROR", f"{request['method']} {url_guvenli} -> connection failed: {err.reason}")
        return {
            "status": 0,
            "body": f"connection failed: {err.reason}",
            "json": None,
            "headers": {},
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
    return {"status": status, "body": raw, "json": parsed, "headers": headers}


MAX_RESULT_CHARS = 40_000


def remediation(result: dict, tool: dict | None = None, known_paths: list[str] | None = None) -> str:
    """Turn an HTTP error into corrective guidance the agent can act on.

    The single biggest lever on agent success rate is not preventing
    errors — it is making the next call succeed. Remediation lines tell
    the agent what the API complained about (validation details), what to
    change (auth, wait time), and the closest valid alternatives on 404.
    Returned text is appended to the error body; empty when nothing helps.
    """
    status = result["status"]
    tips: list[str] = []
    if status == 0:
        tips.append("Connection failed — check the base URL and network, then re-run.")
    parsed = result.get("json")
    if isinstance(parsed, dict):
        detail = parsed.get("detail")
        if isinstance(detail, str) and detail:
            tips.append(f"API said: {detail[:300]}")
        elif isinstance(detail, list):
            items = []
            for item in detail[:3]:
                if isinstance(item, dict) and item.get("msg"):
                    loc = ".".join(str(part) for part in item.get("loc", []))
                    items.append(f"{loc}: {item['msg']}" if loc else str(item["msg"]))
            if items:
                tips.append("API validation: " + "; ".join(items))
        if "detail" not in parsed:
            for key in ("message", "error"):
                value = parsed.get(key)
                if isinstance(value, str) and value:
                    tips.append(f"API said: {value[:300]}")
                    break
        errors = parsed.get("errors")
        if isinstance(errors, list):
            items = []
            for entry in errors[:3]:
                if isinstance(entry, dict):
                    items.append(str(entry.get("message", entry))[:200])
                else:
                    items.append(str(entry)[:200])
            if items:
                tips.append("API errors: " + "; ".join(items))
    if status in (401, 403):
        tips.append(
            "Check credentials: serve with --auth-env/--auth-style so the "
            "credential is actually sent and accepted by the API."
        )
    elif status == 404 and known_paths and tool is not None:
        import difflib

        path = tool["_meta"]["path"]
        close = difflib.get_close_matches(path, known_paths, n=3, cutoff=0.5)
        if close:
            tips.append("Closest known paths: " + ", ".join(close))
    elif status == 405:
        tips.append("HTTP method not allowed on this path — the operation may have changed upstream.")
    elif status == 429:
        retry_after = (result.get("headers") or {}).get("Retry-After")
        wait = f"waiting {retry_after}s" if retry_after else "backing off"
        tips.append(f"Rate limited — the API suggests {wait}. mcpify never retries automatically.")
    elif status >= 500:
        tips.append(
            "The upstream API failed (not an mcpify error). Re-running the call "
            "yourself is reasonable; mcpify does not retry automatically."
        )
    if not tips:
        return ""
    return "\n" + "\n".join("- " + tip for tip in tips)


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
