"""Distributed tracing setup. Disabled by default (OTEL_ENABLED=false) so
the gateway has zero tracing overhead/dependencies at runtime unless
explicitly turned on.

When enabled:
  - Every request gets a span via FastAPI auto-instrumentation.
  - The proxy call to the tenant's upstream propagates the current trace
    context in a `traceparent` header (W3C Trace Context standard), so a
    tenant running their own OpenTelemetry-instrumented backend can
    correlate their server-side span with the gateway's span for the same
    request — this is what makes "gateway added N ms of latency" a
    provable, traceable claim instead of an assertion.
  - Spans export to the console by default, or to a real OTLP collector if
    OTEL_EXPORTER_OTLP_ENDPOINT is set.
"""

import logging

from app.config import get_settings

logger = logging.getLogger("gateway.tracing")

_tracer = None


def setup_tracing(app) -> None:
    global _tracer
    settings = get_settings()
    if not settings.OTEL_ENABLED:
        return

    from opentelemetry import trace
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    resource = Resource(attributes={SERVICE_NAME: settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)

    if settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
    else:
        exporter = ConsoleSpanExporter()

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()

    _tracer = trace.get_tracer(settings.OTEL_SERVICE_NAME)
    logger.info(
        "OpenTelemetry tracing enabled (exporter=%s)",
        "otlp" if settings.OTEL_EXPORTER_OTLP_ENDPOINT else "console",
    )


def get_tracer():
    return _tracer


def start_span(name: str, attributes: dict | None = None):
    """Returns a context manager span if tracing is enabled, otherwise a
    no-op context manager — callers don't need to branch on whether
    tracing is on."""
    if _tracer is None:
        from contextlib import nullcontext

        return nullcontext()
    span_cm = _tracer.start_as_current_span(name)
    if attributes:
        # We can't set attributes until inside the context; wrap it.
        from contextlib import contextmanager

        @contextmanager
        def _wrapped():
            with span_cm as span:
                for k, v in attributes.items():
                    span.set_attribute(k, v)
                yield span

        return _wrapped()
    return span_cm
