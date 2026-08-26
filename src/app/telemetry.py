"""OpenTelemetry tracing.

Vendor-neutral by design. Traces are exported over OTLP to whatever collector
the platform runs -- Tempo, Jaeger, Datadog, an OTel Collector fanning out to
several. Nothing here names a vendor, so switching one is a config change.

Two ideas that are easy to conflate:

  correlation id  -- ours. Business-facing, accepted from the caller, echoed in
                     the response, quoted by a user in a support ticket.
  trace id        -- OpenTelemetry's. Spans a whole distributed call graph and
                     is what an observability backend indexes on.

We keep both and put both on every log line, so a support ticket quoting a
correlation id leads to a log line, and that log line leads to the trace.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app.config import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Probe traffic is high-volume, uninteresting, and would dominate any sampled
# trace budget. The platform already knows whether the probes pass.
_EXCLUDED_ROUTES = "/health,/ready"


def configure_tracing(app: FastAPI, settings: Settings) -> None:
    """Install the tracer provider and instrument the app.

    A no-op when no OTLP endpoint is configured, so the template runs on a
    laptop with no collector. Instrumentation is still installed either way:
    spans are created and become the source of trace ids for log correlation,
    they are simply not exported.
    """
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.version,
            "deployment.environment.name": settings.environment,
        }
    )

    # ParentBased: if an upstream service already decided to sample this trace,
    # honour that. Sampling each hop independently produces broken traces where
    # the middle of a call graph is missing.
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(root=TraceIdRatioBased(settings.trace_sample_ratio)),
    )

    if settings.tracing_enabled:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
        )
        logger.info("tracing enabled", extra={"otlp_endpoint": settings.otlp_endpoint})
    else:
        logger.info("tracing not exported: no otlp_endpoint configured")

    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls=_EXCLUDED_ROUTES)


def current_trace_ids() -> tuple[str, str]:
    """Return (trace_id, span_id) as hex, or ('-', '-') outside a span."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return "-", "-"
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
