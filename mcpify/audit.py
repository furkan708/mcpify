"""Append-only audit log for tool executions.

``--audit-log FILE`` turns serving into a compliance-friendly trail:
one JSON line per real API call — timestamp, tool, API label, outcome,
latency and an argument *fingerprint*. Arguments are never written raw
(they can carry end-user data); the fingerprint lets you correlate
repeat calls without storing their content. Thread-safe, fail-safe: an
unwritable log file must never take the server down.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_FILE: str | None = None
_warned = False


def enable(path: str) -> None:
    """Start recording (called by the CLI when --audit-log is passed)."""
    global _FILE
    _FILE = path


def disable() -> None:
    global _FILE
    _FILE = None


def is_enabled() -> bool:
    return _FILE is not None


def arguments_fingerprint(arguments: dict[str, Any]) -> str:
    """Stable short hash of the argument keys+values — correlation
    without content storage."""
    payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def record(tool: str, api: str, status: int, latency_seconds: float, arguments: dict[str, Any]) -> None:
    """Append one JSONL line. Never raises into the serving path."""
    global _warned
    if _FILE is None:
        return
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": tool,
        "api": api,
        "status": status,
        "outcome": "ok" if (0 < status < 400) else "error",
        "latency_ms": round(latency_seconds * 1000, 1),
        "arguments_fingerprint": arguments_fingerprint(arguments),
    }
    line = json.dumps(entry, ensure_ascii=False)
    with _LOCK:
        try:
            with open(_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _warned = False
        except OSError:
            # fail-safe by design: an unwritable audit file must never take
            # the serving path down. Warn once per process (unless silenced);
            # further failures stay silent — the log is simply incomplete.
            if not _warned and not os.environ.get("MCPIFY_QUIET_AUDIT"):
                _warned = True
                print(f"mcpify audit: cannot write {_FILE} — audit entries are being dropped",
                      file=sys.stderr, flush=True)
