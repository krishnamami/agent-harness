from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.health import clear_readiness_checks
from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="local", log_format="console", log_level="DEBUG")


@pytest.fixture
def client(settings: Settings):
    clear_readiness_checks()
    with TestClient(create_app(settings)) as c:
        yield c
    clear_readiness_checks()
