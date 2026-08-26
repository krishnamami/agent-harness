"""Operational endpoints.

Two endpoints, and the distinction between them matters more than it looks:

  /health  -- liveness.  "Is this process alive?"  Never checks a dependency.
  /ready   -- readiness. "Should traffic be sent here?"  Checks dependencies.

Conflating them is a common and expensive mistake. If /health checks the
database, then a slow database makes the orchestrator conclude the process is
dead and restart it -- turning a degraded dependency into a restart loop that
takes the whole service down. Liveness answers only whether the process needs
killing; readiness answers whether it should receive requests right now.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Response

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["operations"])

ReadinessCheck = Callable[[], Awaitable[None]]
_checks: dict[str, ReadinessCheck] = {}


def register_readiness_check(name: str, check: ReadinessCheck) -> None:
    """Register a dependency check.

    A service built from this template calls this at startup for each thing it
    cannot serve without -- a vector store, a model provider, a queue. The
    template itself registers nothing, because it depends on nothing.
    """
    _checks[name] = check


def clear_readiness_checks() -> None:
    """Reset the registry. Test support."""
    _checks.clear()


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    settings: Settings = get_settings()
    return {
        "status": "ok",
        "service": settings.service_name,
        "version": settings.version,
        "environment": settings.environment,
    }


@router.get("/ready", summary="Readiness probe")
async def ready(response: Response) -> dict[str, object]:
    results: dict[str, str] = {}
    healthy = True

    for name, check in _checks.items():
        try:
            await check()
            results[name] = "ok"
        except Exception as exc:  # a failing check must report, not 500
            results[name] = f"failed: {type(exc).__name__}"
            healthy = False
            logger.warning("readiness check failed", extra={"check": name})

    if not healthy:
        response.status_code = 503

    return {"status": "ready" if healthy else "not_ready", "checks": results}
