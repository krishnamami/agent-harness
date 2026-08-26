"""Logging tests.

test_access_log_carries_correlation_id is a regression test. The first version
of the middleware logged after its own `try/finally`, which reset the
ContextVar first -- so every access log line recorded "-" while the response
header was correct. The header test passed, the suite was green, and the bug
was only visible by reading real log output.
"""

from __future__ import annotations

import json
import logging

from app.logging import JsonFormatter, correlation_id


def test_access_log_carries_correlation_id(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.middleware"):
        client.get("/health", headers={"x-correlation-id": "trace-me"})

    records = [r for r in caplog.records if r.getMessage() == "request"]
    assert records, "middleware did not emit an access log line"

    formatted = json.loads(JsonFormatter("svc", "local", "0.0.0").format(records[0]))
    assert formatted["correlation_id"] == "trace-me"
    assert formatted["http_status"] == 200
    assert formatted["http_path"] == "/health"
    assert isinstance(formatted["duration_ms"], float)


def test_json_formatter_promotes_extras_to_top_level():
    record = logging.LogRecord("t", logging.INFO, "f", 1, "hello", None, None)
    record.tenant_id = "acme"
    out = json.loads(JsonFormatter("svc", "prod", "1.2.3").format(record))
    assert out["tenant_id"] == "acme"
    assert out["service"] == "svc"
    assert out["environment"] == "prod"
    assert out["message"] == "hello"


def test_json_formatter_drops_uvicorn_ansi_copy():
    record = logging.LogRecord("uvicorn.error", logging.INFO, "f", 1, "up", None, None)
    record.color_message = "\x1b[36mup\x1b[0m"
    out = json.loads(JsonFormatter("svc", "prod", "1.0.0").format(record))
    assert "color_message" not in out


def test_json_formatter_survives_unserialisable_extras():
    record = logging.LogRecord("t", logging.INFO, "f", 1, "x", None, None)
    record.weird = object()
    out = json.loads(JsonFormatter("svc", "local", "0.0.0").format(record))
    assert isinstance(out["weird"], str)


def test_correlation_id_defaults_outside_a_request():
    assert correlation_id.get() == "-"
