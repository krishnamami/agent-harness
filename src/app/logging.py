"""Structured logging.

Logs are written to stdout as one JSON object per line. Nothing is written to a
file: the process emits a stream and the platform decides where it goes. That
is factor XI of the twelve-factor app, and it is what makes the same image work
unchanged on a laptop, in ECS and in Kubernetes.

Every line carries the correlation id of the request that produced it, so one
request can be reconstructed from an aggregator without guessing.

Do not log request bodies, headers, or anything derived from user content from
here. Redaction is a guardrail concern and belongs in middleware, where it can
be tested in isolation -- not in a formatter that every log line passes through.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from app.config import Settings

# A ContextVar rather than a global: asyncio tasks each get their own value, so
# concurrent requests cannot read each other's correlation id.
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

# Captured at import so repeated configure_logging() calls cannot chain
# factories on top of each other.
_BASE_RECORD_FACTORY = logging.getLogRecordFactory()


def _install_record_factory() -> None:
    """Stamp the correlation id onto every LogRecord as it is created.

    Reading the ContextVar inside the formatter instead looks equivalent and is
    subtly wrong. A formatter runs when a handler processes the record, which
    for a QueueHandler -- the standard way to keep logging off the request path
    -- happens on another thread after the request has finished. The ContextVar
    is gone by then and every line records "-". Stamping at creation binds the
    value to the record while the request context still exists.
    """

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = _BASE_RECORD_FACTORY(*args, **kwargs)
        record.correlation_id = correlation_id.get()
        # The trace id is what an observability backend indexes on. Putting it
        # on the log line is what turns "here is an error" into "here is the
        # exact request that produced it, and every hop it made".
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record.trace_id = format(span_context.trace_id, "032x")
            record.span_id = format(span_context.span_id, "016x")
        return record

    logging.setLogRecordFactory(factory)


# Anything on a LogRecord that is not one of these was passed by the caller as
# `extra=` and should be promoted to a top-level field in the JSON output.
_LOGRECORD_BUILTINS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
    # uvicorn attaches an ANSI-coloured copy of its own message. Useful in a
    # terminal, noise in a log aggregator.
    "color_message",
    # promoted to first-class fields, not extras
    "correlation_id",
    "trace_id",
    "span_id",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with service identity on every record."""

    def __init__(self, service: str, environment: str, version: str) -> None:
        super().__init__()
        self._base = {"service": service, "environment": environment, "version": version}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
            **self._base,
        }
        for key, value in record.__dict__.items():
            if key not in _LOGRECORD_BUILTINS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str so an unexpected object never takes the process down at
        # the moment you most need the log line.
        return json.dumps(payload, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Human-readable output. Local development only -- config enforces that."""

    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", "-")
        head = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}"
        tail = " ".join(
            f"{k}={v}" for k, v in record.__dict__.items() if k not in _LOGRECORD_BUILTINS
        )
        line = f"{head} [{cid[:8]}] {record.getMessage()}"
        if tail:
            line = f"{line} | {tail}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(settings: Settings) -> None:
    """Install the formatter on the root logger and tame uvicorn's own loggers.

    uvicorn installs its own handlers at startup. Left alone they emit plain
    text alongside our JSON, which produces a log stream that no aggregator can
    parse consistently. Clearing their handlers and letting records propagate to
    root is the fix, and it is the step most services forget.
    """
    _install_record_factory()

    formatter: logging.Formatter
    if settings.log_format == "json":
        formatter = JsonFormatter(
            service=settings.service_name,
            environment=settings.environment,
            version=settings.version,
        )
    else:
        formatter = ConsoleFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # We emit our own access log in middleware with timing and correlation id.
    # uvicorn's version would be a duplicate without either.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
