"""Error contract tests.

Every error leaves this service as RFC 9457 problem+json carrying the
correlation id. These tests are the contract: if they change, clients break.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.main import create_app


@pytest.fixture
def failing_app(settings: Settings):
    from fastapi.testclient import TestClient

    app = create_app(settings)

    @app.get("/v1/boom")
    async def boom() -> None:
        raise RuntimeError("connection string postgres://user:hunter2@db:5432")

    @app.get("/v1/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="I'm a teapot")

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_404_is_problem_json(client):
    r = client.get("/v1/does-not-exist")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 404
    assert "correlation_id" in body


def test_problem_carries_the_correlation_id(client):
    r = client.get("/v1/nope", headers={"x-correlation-id": "support-ticket-99"})
    assert r.json()["correlation_id"] == "support-ticket-99"


def test_http_exception_detail_is_preserved(failing_app):
    r = failing_app.get("/v1/teapot")
    assert r.status_code == 418
    assert r.json()["title"] == "I'm a teapot"


def test_unhandled_error_shows_detail_outside_production(failing_app):
    r = failing_app.get("/v1/boom")
    assert r.status_code == 500
    assert "RuntimeError" in r.json()["detail"]


def test_unhandled_error_leaks_nothing_in_production():
    """The important one: exception text carries credentials and hostnames."""
    from fastapi.testclient import TestClient

    prod = Settings(environment="prod", log_format="json", log_level="INFO")
    app = create_app(prod)

    @app.get("/v1/boom")
    async def boom() -> None:
        raise RuntimeError("connection string postgres://user:hunter2@db:5432")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/v1/boom")

    assert r.status_code == 500
    body = r.text
    assert "hunter2" not in body
    assert "postgres" not in body
    assert "RuntimeError" not in body
    assert r.json()["correlation_id"]


def test_docs_are_disabled_in_production():
    from fastapi.testclient import TestClient

    prod = Settings(environment="prod", log_format="json", log_level="INFO")
    with TestClient(create_app(prod)) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
