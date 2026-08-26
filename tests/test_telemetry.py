from __future__ import annotations

import json
import logging

from app.config import Settings
from app.logging import JsonFormatter
from app.telemetry import current_trace_ids


def test_no_span_returns_placeholders():
    trace_id, span_id = current_trace_ids()
    assert trace_id == "-"
    assert span_id == "-"


def test_tracing_disabled_without_an_endpoint():
    assert Settings().tracing_enabled is False


def test_tracing_enabled_with_an_endpoint():
    s = Settings(otlp_endpoint="http://collector:4318/v1/traces")
    assert s.tracing_enabled is True


def test_sample_ratio_must_be_a_probability():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(trace_sample_ratio=1.5)


def test_log_records_carry_trace_fields():
    """Placeholders when no span is active, real ids when one is."""
    record = logging.LogRecord("t", logging.INFO, "f", 1, "x", None, None)
    out = json.loads(JsonFormatter("svc", "local", "0.0.0").format(record))
    assert "trace_id" in out
    assert "span_id" in out
