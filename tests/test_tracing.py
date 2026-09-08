import pytest

from app.tracing import get_tracer, setup_tracing, start_span


def test_tracing_disabled_by_default_is_noop(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "OTEL_ENABLED", False)

    from fastapi import FastAPI

    app = FastAPI()
    setup_tracing(app)  # should do nothing, not raise
    assert get_tracer() is None


def test_start_span_is_noop_context_manager_when_disabled():
    # Should not raise even with no tracer configured.
    with start_span("test.span", {"foo": "bar"}):
        pass


def test_tracing_enabled_instruments_app_without_error(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "")

    from fastapi import FastAPI

    app = FastAPI()
    setup_tracing(app)  # console exporter, should not raise
    assert get_tracer() is not None

    with start_span("test.span", {"tenant.slug": "acme"}):
        pass

    # Properly shut down the tracer provider (flushes and stops its
    # background export thread) rather than just nulling our own
    # reference — otherwise that thread can try to log after pytest
    # closes stdout, producing harmless but noisy teardown errors.
    from opentelemetry import trace

    trace.get_tracer_provider().shutdown()

    import app.tracing as tracing_module

    tracing_module._tracer = None
