"""
Structured logging (structlog), Prometheus metrics, and OpenTelemetry tracing.

Call configure_logging() and configure_tracing() once at process startup.
All three pillars export in Grafana-stack-compatible formats:
  - Logs  : structlog JSON → Loki (via Promtail/Alloy)
  - Metrics: prometheus-client → Prometheus scrape at /metrics
  - Traces : OTLP HTTP → Grafana Tempo (or any OTEL-compatible backend)

Tracing is a no-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram, start_http_server  # noqa: F401

# ── Prometheus counters ───────────────────────────────────────────────────────

tasks_total = Counter(
    "tack_ai_tasks_total",
    "Number of tasks submitted",
    ["status"],
)

tool_calls_total = Counter(
    "tack_ai_tool_calls_total",
    "Number of tool calls made by the agent",
    ["tool_name", "outcome"],
)

policy_decisions_total = Counter(
    "tack_ai_policy_decisions_total",
    "Policy decisions from OPA / Cedar",
    ["decision"],
)

task_cost_usd = Counter(
    "tack_ai_task_cost_usd_total",
    "Cumulative cost of all tasks in USD",
)

task_duration_seconds = Histogram(
    "tack_ai_task_duration_seconds",
    "End-to-end task duration",
    buckets=[1, 5, 15, 30, 60, 120, 300],
)

# ── OpenTelemetry tracing ─────────────────────────────────────────────────────

def configure_tracing() -> None:
    """
    Set up an OTLP-HTTP TracerProvider.

    Reads standard OTEL env vars:
      OTEL_EXPORTER_OTLP_ENDPOINT  e.g. http://localhost:4318  (Tempo / Collector)
      OTEL_SERVICE_NAME            defaults to "tack-ai"

    No-op when OTEL_EXPORTER_OTLP_ENDPOINT is not set.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        return

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,  # noqa: PLC0415
    )

    service_name = os.environ.get("OTEL_SERVICE_NAME", "tack-ai")
    resource = Resource(attributes={SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_global_default_tracer_provider(provider)


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


# ── Structlog processor: inject OTEL trace/span IDs ──────────────────────────

def _inject_trace_context(
    logger: object, method_name: str, event_dict: dict
) -> dict:
    """
    Add trace_id and span_id to every log record emitted inside an active span.

    Grafana can use these fields to jump from a Loki log line directly to the
    correlated Tempo trace (requires the Loki → Tempo derived-field datasource
    link configured in Grafana).
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


# ── Structlog setup ───────────────────────────────────────────────────────────

def configure_logging(json: bool = True, level: str = "INFO") -> None:
    """Configure structlog with JSON output and OTEL trace context injection."""
    pre_chain: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _inject_trace_context,
    ]

    # add_logger_name uses record.name — only safe on stdlib bridge records.
    foreign_pre_chain: list = [
        *pre_chain,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
    ]

    if json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *pre_chain,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign_pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for name in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
