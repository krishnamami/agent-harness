"""Application entry point.

create_app() is a factory rather than a module-level `app = FastAPI()` object.
That matters for two reasons: tests can build an app with a different Settings
instance instead of mutating global state, and nothing touches the environment
at import time -- so importing this module can never fail because a variable
was missing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.api.v1 import router as v1_router
from app.config import Settings, get_settings
from app.errors import register_exception_handlers
from app.logging import configure_logging
from app.middleware import CorrelationIdMiddleware
from app.telemetry import configure_tracing

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "service starting",
            extra={"environment": settings.environment, "version": settings.version},
        )
        # A service built from this template opens its connections here and
        # registers a readiness check for each one.
        yield
        logger.info("service stopping")

    app = FastAPI(
        title=settings.service_name,
        version=settings.version,
        lifespan=lifespan,
        # The interactive docs are useful everywhere except production, where
        # they advertise your surface area to anyone who finds the host.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(CorrelationIdMiddleware)
    register_exception_handlers(app, settings)

    # Instrumented last, so the tracing middleware sits outermost and its span
    # covers the correlation-id middleware and the exception handlers rather
    # than being nested inside them.
    configure_tracing(app, settings)

    # Operational endpoints are deliberately unversioned: probes are configured
    # by the platform, not by API consumers, and must not move between versions.
    app.include_router(health.router)
    app.include_router(v1_router)

    return app


app = create_app()
