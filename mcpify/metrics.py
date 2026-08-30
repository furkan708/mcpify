"""Prometheus text-format metrics — thread-safe, opt-in, zero-dependency.

Enabled by ``--metrics`` (or implicitly by ``mcpify ui``). When disabled,
every recording call is a single boolean check, so the serving hot path
stays untouched.

Exposed series:
- ``mcpify_tool_calls_total{tool,api,outcome}`` — outcome: ok | error
- ``mcpify_tool_latency_seconds`` histogram per {tool,api}
- ``mcpify_cache_requests_total{result}`` — hit | miss
- ``mcpify_up`` / ``mcpify_uptime_seconds`` / ``mcpify_tools`` gauges
- ``mcpify_api_health{api}`` — 1 reachable, 0 down (set by health checks)

Alerting belongs to the Prometheus side; docs/USAGE.md ships example
per-API error-rate rules.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

Labels = tuple[tuple[str, str], ...]

_lock = threading.Lock()
_enabled = False
_started = time.monotonic()
_counters: dict[tuple[str, Labels], float] = {}
_gauges: dict[tuple[str, Labels], float] = {}
_histograms: dict[tuple[str, Labels], dict[str, Any]] = {}
_meta: dict[str, tuple[str, str]] = {}  # series family -> (HELP, TYPE)

BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def enable() -> None:
    """Turn on recording (idempotent)."""
    global _enabled
    with _lock:
        _enabled = True
        _declare("mcpify_up", "1 when the server process is serving.", "gauge")
        _declare("mcpify_uptime_seconds", "Seconds since the process started serving.", "gauge")
        _declare("mcpify_tools", "Tools currently listed by the server.", "gauge")
        _declare(
            "mcpify_tool_calls_total",
            "Tool executions. outcome=ok|error (transport/HTTP errors included).",
            "counter",
        )
        _declare("mcpify_tool_latency_seconds", "End-to-end tool call latency.", "histogram")
        _declare(
            "mcpify_cache_requests_total",
            "GET cache lookups. result=hit|miss (only when --cache-ttl is on).",
            "counter",
        )
        _declare(
            "mcpify_api_health",
            "1 when the API answered the last health probe, 0 when down.",
            "gauge",
        )


def disable() -> None:
    """Turn recording off and drop buffered samples (tests, shutdown)."""
    global _enabled
    with _lock:
        _enabled = False
        _counters.clear()
        _gauges.clear()
        _histograms.clear()


def is_enabled() -> bool:
    return _enabled


def _declare(name: str, help_text: str, typ: str) -> None:
    if name not in _meta:
        _meta[name] = (help_text, typ)


def _key(name: str, labels: dict[str, str] | None) -> tuple[str, Labels]:
    return name, tuple(sorted((labels or {}).items()))


def inc(name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
    """Increment a counter (no-op unless enabled)."""
    if not _enabled:
        return
    with _lock:
        key = _key(name, labels)
        _counters[key] = _counters.get(key, 0.0) + value


def gauge_set(name: str, labels: dict[str, str] | None, value: float) -> None:
    """Set a gauge to an absolute value (no-op unless enabled)."""
    if not _enabled:
        return
    with _lock:
        _gauges[_key(name, labels)] = value


def observe(name: str, labels: dict[str, str] | None, value: float) -> None:
    """Record one latency observation into the cumulative histogram."""
    if not _enabled:
        return
    with _lock:
        hist = _histograms.setdefault(
            _key(name, labels),
            {"buckets": dict.fromkeys(BUCKETS, 0), "+Inf": 0, "sum": 0.0, "count": 0},
        )
        for bound in BUCKETS:
            if value <= bound:
                hist["buckets"][bound] = int(hist["buckets"][bound]) + 1
        hist["+Inf"] = int(hist["+Inf"]) + 1
        hist["sum"] = float(hist["sum"]) + value
        hist["count"] = int(hist["count"]) + 1


def health_report(api: str, reachable: bool) -> None:
    """Record the latest health probe result for one API."""
    gauge_set("mcpify_api_health", {"api": api}, 1.0 if reachable else 0.0)


def set_tool_gauges(server_name: str, tool_count: int) -> None:
    """Process-level gauges refreshed by the serving layer."""
    gauge_set("mcpify_up", {"server": server_name}, 1.0)
    gauge_set("mcpify_uptime_seconds", {"server": server_name}, max(0.0, time.monotonic() - _started))
    gauge_set("mcpify_tools", {"server": server_name}, float(tool_count))


def _format_labels(labels: Labels) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{key}="{val}"' for key, val in labels)
    return "{" + inner + "}"


def render_prometheus() -> str:
    """Serialize every buffered sample as Prometheus text format 0.0.4."""
    with _lock:
        lines: list[str] = []
        seen_meta: set[str] = set()

        def header(name: str) -> None:
            if name in _meta and name not in seen_meta:
                help_text, typ = _meta[name]
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {typ}")
                seen_meta.add(name)

        rows = sorted(
            [(*key, value) for key, value in _counters.items()],
            key=lambda item: (item[0], item[1]),
        )
        for name, labels, value in rows:
            header(name)
            lines.append(f"{name}{_format_labels(labels)} {value}")
        rows = sorted(
            [(*key, value) for key, value in _gauges.items()],
            key=lambda item: (item[0], item[1]),
        )
        for name, labels, value in rows:
            header(name)
            lines.append(f"{name}{_format_labels(labels)} {value}")
        for (name, labels), hist in sorted(_histograms.items(), key=lambda item: (item[0][0], item[0][1])):
            header(name)
            for bound in BUCKETS:
                bucket_labels: Labels = tuple(sorted((*labels, ("le", repr(bound)))))
                lines.append(f'{name}_bucket{_format_labels(bucket_labels)} {hist["buckets"][bound]}')
            inf_labels: Labels = tuple(sorted((*labels, ("le", "+Inf"))))
            lines.append(f'{name}_bucket{_format_labels(inf_labels)} {hist["+Inf"]}')
            lines.append(f'{name}_sum{_format_labels(labels)} {hist["sum"]}')
            lines.append(f'{name}_count{_format_labels(labels)} {hist["count"]}')
        return "\n".join(lines) + "\n" if lines else ""


def snapshot() -> dict[str, Any]:
    """Structured form of everything exposed, for the dashboard."""
    with _lock:
        counters = [
            {"name": name, "labels": dict(labels), "value": value}
            for (name, labels), value in _counters.items()
        ]
        gauges = [
            {"name": name, "labels": dict(labels), "value": value}
            for (name, labels), value in _gauges.items()
        ]
        histograms = [
            {
                "name": name,
                "labels": dict(labels),
                "count": hist["count"],
                "sum": round(float(hist["sum"]), 6),
            }
            for (name, labels), hist in _histograms.items()
        ]
        return {
            "enabled": _enabled,
            "uptime_seconds": round(max(0.0, time.monotonic() - _started), 3),
            "counters": sorted(counters, key=lambda row: (row["name"], str(row["labels"]))),
            "gauges": sorted(gauges, key=lambda row: (row["name"], str(row["labels"]))),
            "histograms": sorted(histograms, key=lambda row: (row["name"], str(row["labels"]))),
        }


class _MetricsHandler(BaseHTTPRequestHandler):
    """GET / -> Prometheus text; everything else -> 404."""

    def log_message(self, *args: object) -> None:  # scrape noise stays silent
        pass

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = render_prometheus().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_metrics_server(host: str, port: int) -> tuple[HTTPServer, threading.Thread]:
    """Bind the /metrics endpoint and serve it on a daemon thread."""
    httpd = HTTPServer((host, port), _MetricsHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread
