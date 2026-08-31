"""Live pre-flight probe: one safe GET against the API before serving.

Shared by `mcpify doctor --probe` and `mcpify init --probe`. Read-only by
construction: an argument-free GET operation (or the base URL), never a
body, never a write.
"""

from __future__ import annotations

import time
from typing import Any

from .http_client import execute
from .spec import iter_operations


def pick_probe_operation(spec: dict[str, Any]) -> tuple[str, str] | None:
    """First safe GET: no path params, no required params, no body."""
    for method, path, operation in iter_operations(spec):
        if method != "GET" or "{" in path or operation.get("requestBody"):
            continue
        params = list(operation.get("parameters") or [])
        if any(param.get("required") for param in params):
            continue
        return method, path
    return None


def run_probe(
    spec: dict[str, Any],
    base_url: str | None,
    timeout: float,
    auth: Any = None,
    fail_on_http_error: bool = False,
) -> dict[str, Any]:
    """One live GET against the API: a connectivity (and credential)
    proof before you serve.

    Default verdict: any HTTP status proves the API is up — a 401 with no
    credentials is a working API; only a connection failure is a failed
    pre-flight. With ``fail_on_http_error`` (the CI gate), 4xx/5xx count
    as failure too — pair it with ``auth`` to prove the credential works
    end-to-end, not just that the host answers.
    """
    if not base_url:
        return {"ok": False, "error": "no absolute base URL — pass --base-url"}
    base_url = base_url.rstrip("/")
    picked = pick_probe_operation(spec)
    if picked is not None:
        method, path = picked
        url = base_url + path
    else:
        method, path = "GET", "/"
        url = base_url + "/"
    headers: dict[str, Any] = {"Accept": "application/json"}
    if auth is not None:
        headers.update(auth.headers())
        url = auth.apply_query(url)
    started = time.monotonic()
    result = execute({"method": method, "url": url, "headers": headers, "body": None},
                     timeout=timeout)
    latency = time.monotonic() - started
    status = int(result["status"])
    out: dict[str, Any] = {"ok": status != 0, "method": method, "path": path,
                           "url": url, "status": status,
                           "latency_seconds": round(latency, 3),
                           "authenticated": auth is not None}
    if status == 0:
        out["error"] = "connection failed — check the URL, network and TLS"
    elif fail_on_http_error and status >= 400:
        out["ok"] = False
        out["error"] = (f"HTTP {status} — --fail-on-http-error treats 4xx/5xx "
                        "as a failed pre-flight")
    return out
