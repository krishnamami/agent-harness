"""Error responses.

Every error leaves this service in the same shape: RFC 9457 Problem Details,
served as ``application/problem+json``. One shape means a client writes one
error handler, and an aggregator can count error types without parsing prose.

Every problem carries the ``correlation_id``. That is the whole support loop: a
user quotes the id from their error screen, it finds the log line, the log line
carries the trace id, and the trace shows what actually happened.

In production an unhandled exception returns a generic message. Exception text
leaks connection strings, file paths and internal hostnames, and an error page
is the cheapest reconnaissance an attacker will ever get.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings
from app.guardrails import GuardrailBlockedError
from app.logging import correlation_id

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"


def problem(
    status_code: int,
    title: str,
    detail: str | None = None,
    type_: str = "about:blank",
    **extra: Any,
) -> JSONResponse:
    """Build an RFC 9457 problem response."""
    body: dict[str, Any] = {
        "type": type_,
        "title": title,
        "status": status_code,
        "correlation_id": correlation_id.get(),
    }
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status_code, content=body, media_type=PROBLEM_CONTENT_TYPE)


def register_exception_handlers(app: FastAPI, settings: Settings) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Deliberate: raised by our own code, so the detail is safe to return.
        return problem(exc.status_code, title=str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field-level errors are returned in full: the caller sent this data,
        # so echoing which part of it was wrong reveals nothing they did not
        # already know, and it is the difference between a usable API and a
        # frustrating one.
        return problem(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            title="Request validation failed",
            detail="The request body or parameters did not match the schema.",
            errors=[
                {
                    "field": ".".join(str(p) for p in err["loc"]),
                    "message": err["msg"],
                    "type": err["type"],
                }
                for err in exc.errors()
            ],
        )

    @app.exception_handler(GuardrailBlockedError)
    async def _guardrail_blocked(_: Request, exc: GuardrailBlockedError) -> JSONResponse:
        # 400, not 500: the request was understood and deliberately refused.
        # The guardrail name is returned so a caller can tell "too long" from
        # "policy violation" without a support ticket; the reason is not, since
        # a detailed rejection message is a free oracle for probing the filter.
        logger.warning(
            "guardrail blocked request",
            extra={"guardrail": exc.guardrail_name, "reason": exc.result.reason},
        )
        return problem(
            status.HTTP_400_BAD_REQUEST,
            title="Request blocked by a content policy",
            detail="This request was refused by a safety check.",
            guardrail=exc.guardrail_name,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Logged in full with the stack trace; returned as almost nothing.
        logger.exception("unhandled exception", extra={"error_type": type(exc).__name__})
        if settings.is_production:
            return problem(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                title="Internal server error",
                detail="Quote the correlation id when reporting this.",
            )
        return problem(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            title="Internal server error",
            detail=f"{type(exc).__name__}: {exc}",
        )
