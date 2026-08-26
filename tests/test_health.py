from __future__ import annotations

from app.api.health import register_readiness_check


def test_health_is_alive(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_does_not_check_dependencies(client):
    """Liveness must stay green even when a dependency is down."""

    async def broken() -> None:
        raise RuntimeError("vector store unreachable")

    register_readiness_check("vector_store", broken)
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 503


def test_ready_with_no_dependencies(client):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "checks": {}}


def test_ready_reports_each_check(client):
    async def ok() -> None:
        return None

    async def bad() -> None:
        raise ConnectionError

    register_readiness_check("good", ok)
    register_readiness_check("bad", bad)

    r = client.get("/ready")
    assert r.status_code == 503
    checks = r.json()["checks"]
    assert checks["good"] == "ok"
    assert checks["bad"].startswith("failed: ConnectionError")


def test_correlation_id_is_echoed(client):
    r = client.get("/health", headers={"x-correlation-id": "abc-123"})
    assert r.headers["x-correlation-id"] == "abc-123"


def test_correlation_id_is_generated_when_absent(client):
    r = client.get("/health")
    assert len(r.headers["x-correlation-id"]) == 36
