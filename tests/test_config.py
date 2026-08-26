from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults_are_local():
    s = Settings()
    assert s.environment == "local"
    assert s.is_production is False


def test_prod_rejects_console_logs():
    with pytest.raises(ValidationError, match="log_format must be 'json'"):
        Settings(environment="prod", log_format="console")


def test_prod_rejects_debug_logging():
    with pytest.raises(ValidationError, match="must not be DEBUG"):
        Settings(environment="prod", log_level="DEBUG")


def test_unknown_setting_is_rejected():
    with pytest.raises(ValidationError):
        Settings(typo_in_deploy_manifest="oops")


def test_port_must_be_valid():
    with pytest.raises(ValidationError):
        Settings(port=0)


def test_settings_are_immutable():
    s = Settings()
    with pytest.raises(ValidationError):
        s.port = 9000
