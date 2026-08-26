"""Request middleware.

Session 1 installs correlation ids and the access log. Session 4 adds the
guardrail slots (input/output filtering, PII redaction) and OpenTelemetry span
creation, which is why the guardrail hook is declared here but left empty.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging import correlation_id

logger = logging.getLogger(__name__)

# Accepted inbound header names, in priority order. A request arriving from an
# upstream service should keep its id so one trace spans both hops.
_INBOUND_HEADERS = ("x-correlation-id", "x-request-id")
_OUTBOUND_HEADER = "x-correlation-id"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Attach a correlation id to every request, log it, and echo it back.

    BaseHTTPMiddleware is used deliberately: it is readable, and this template
    is copied by people who need to understand it. It buffers streaming
    responses, so a service that streams tokens should reimplement this as pure
    ASGI middleware -- see docs/adr when that day comes.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = next(
            (request.headers[h] for h in _INBOUND_HEADERS if h in request.headers), None
        )
        cid = incoming or str(uuid.uuid4())
        token = correlation_id.set(cid)

        start = time.perf_counter()
        try:
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000
            response.headers[_OUTBOUND_HEADER] = cid
            # Logged inside the try block, before the `finally` resets the
            # ContextVar. Logging after the try/finally looks equivalent but
            # records "-" for every correlation id -- the reset has already run.
            logger.info(
                "request",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            return response
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request failed",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            raise
        finally:
            correlation_id.reset(token)
