"""Shared pytest fixtures and helpers.

The tests need to flip ``policy.LIVE_TRADING_RELEASED`` on and off without
touching the real module. The ``live_released`` fixture provides a context
manager for that, and ``tmp_env`` builds an in-memory .env.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from crypto_bot.config.env import EnvConfig
from crypto_bot.config.schemas import Settings
from crypto_bot.core import policy


# --------------------------------------------------------------------------- #
# Minimal but valid building blocks
# --------------------------------------------------------------------------- #
def make_settings(**overrides) -> Settings:
    """Return a default-valid Settings, applying nested overrides.

    Overrides are passed as dotted paths, e.g. ``make_settings("runtime__mode"=LIVE)``.
    """

    base = Settings().model_dump()
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            base[section][field] = value
        else:
            base[section] = value
    return Settings.model_validate(base)


def make_env(**overrides) -> EnvConfig:
    """Build an EnvConfig directly (no file) with given overrides."""
    data = dict(
        crypto_bot_mode=None,
        enable_trading=False,
        enable_live_trading=False,
        exchange_name=None,
        exchange_api_key="",
        exchange_api_secret="",
        exchange_sandbox=None,
        db_path=None,
        log_level=None,
        log_file=None,
        log_json=None,
    )
    data.update(overrides)
    return EnvConfig.model_construct(**data)


@contextmanager
def released(value: bool = True) -> Iterator[None]:
    """Temporarily flip ``policy.LIVE_TRADING_RELEASED``."""
    prev = policy.LIVE_TRADING_RELEASED
    policy.LIVE_TRADING_RELEASED = value
    try:
        yield
    finally:
        policy.LIVE_TRADING_RELEASED = prev


@pytest.fixture
def live_released():
    """Yield the ``released`` context manager for use inside tests."""
    return released


@pytest.fixture
def settings_factory():
    return make_settings


@pytest.fixture
def env_factory():
    return make_env
