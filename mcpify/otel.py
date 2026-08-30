"""Optional OpenTelemetry tracing (the ``otel`` extra).

mcpify's core has zero dependencies; this module is imported lazily and
only when ``--otel`` is passed. Without the extra installed you get a
clear, actionable error instead of a traceback. When the extra IS
installed, each upstream API call emits one span (tool, api, status,
latency) to an OTLP/HTTP endpoint.

Prometheus ``--metrics`` already covers numeric metrics; this module is
traces only — request traces need timeline context that counters and
histograms cannot represent.
"""

from __future__ import annotations

import contextlib
from typing import Any


class OtelError(Exception):
    """--otel requested but the 'otel' extra is not installed."""


_TRACER: Any = None  # set by enable_otel(); tracer-like objects are testable
_SERVICE: str = "mcpify"


def enable_otel(endpoint: str, service_name: str = "mcpify") -> str:
    """Install an OTLP/HTTP span exporter and start tracing API calls.

    Returns a one-line status for the serve banner. Raises OtelError with
    install instructions when the optional dependency set is missing.
    """
    global _TRACER, _SERVICE
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as err:
        raise OtelError(
            "--otel needs the optional tracing extra: pip install 'mcpify[otel]' "
            f"(import error: {err})"
        ) from err

    provider = TracerProvider(resource=Resource({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("mcpify")
    _SERVICE = service_name
    return f"tracing -> {endpoint} (service.name={service_name})"


def disable_otel() -> None:
    """Test hook: drop the tracer so subsequent calls are no-ops."""
    global _TRACER
    _TRACER = None


def is_enabled() -> bool:
    """True when spans are being produced (enable_otel or a test tracer)."""
    return _TRACER is not None


def trace_call(tool: str, api: str) -> _SpanCtx:
    """Context manager around ONE upstream API call. No-op unless
    enable_otel() ran (or a test injected a fake tracer)."""
    if _TRACER is None:
        return _SpanCtx(None)
    span = _TRACER.start_span(f"{_SERVICE}:{tool}")
    span.set_attribute("mcpify.tool", tool)
    span.set_attribute("mcpify.api", api)
    return _SpanCtx(span)


class _SpanCtx:
    """Minimal span handle: records status + latency on exit."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_status(self, ok: bool, detail: str = "") -> None:
        if self._span is None:
            return
        self._span.set_attribute("mcpify.ok", ok)
        if detail:
            self._span.set_attribute("mcpify.status_detail", detail[:200])
        if not ok and hasattr(self._span, "set_status"):
            with contextlib.suppress(Exception):  # status must never break a request
                # StatusCode.ERROR == 2 in the SDK; kept literal to stay import-free here
                self._span.set_status(2, detail[:200] or "upstream call failed")

    def finish(self, latency_seconds: float) -> None:
        if self._span is None:
            return
        self._span.set_attribute("mcpify.latency_ms", round(latency_seconds * 1000, 1))
        with contextlib.suppress(Exception):  # export issues are the SDK's job, not the request path
            self._span.end()


__all__ = ["OtelError", "disable_otel", "enable_otel", "is_enabled", "trace_call"]
