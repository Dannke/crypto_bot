"""Configuration loader: the single entry point for a validated ``Config``.

Pipeline:

    .env  ─► EnvConfig   (secrets, kill-switches, optional overrides)
    YAML  ─► Settings    (structured strategy/risk/universe contract)
        └─► env overrides applied (mode, exchange, storage, logging)
        └─► validators.validate_all()   (live-gate, TFs, strategy, universe)

Two-stage validation is deliberate:
  * pydantic in ``schemas.py`` handles structural shape/range checks;
  * validators here handle cross-field + environment policy (live gate).

This is the ONLY module the rest of the application should call to obtain
configuration. Keeping it central means no module can quietly read ``.env`` or
re-parse YAML on its own.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..core.enums import Mode
from ..core.exceptions import ConfigError
from ..core.logging_setup import setup_logging
from ..core.validators import validate_all
from .env import Config, EnvConfig, load_env
from .schemas import Settings

_DEFAULT_YAML = Path("config/settings.yaml")
_DEFAULT_ENV = Path(".env")
_DEFAULT_UNIVERSE_YAML = Path("config/universe.yaml")


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to parse YAML {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"top-level of {path} must be a mapping, got {type(data)!r}.")
    return data


def _load_universe_yaml(path: Path) -> dict[str, Any]:
    """Universe lives in its own file for convenience; merged into Settings."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to parse YAML {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"top-level of {path} must be a mapping, got {type(data)!r}.")
    return data


def _apply_env_overrides(settings: Settings, env: EnvConfig) -> Settings:
    """Let .env override the matching non-secret fields of the YAML config.

    Env is authoritative for kill-switches and secrets; for everything else it
    only overrides when explicitly set (so an unset env var won't clobber YAML).
    """
    data = settings.model_dump()

    if env.crypto_bot_mode is not None:
        data["runtime"]["mode"] = env.crypto_bot_mode

    if env.exchange_name:
        data["exchange"]["name"] = env.exchange_name
    if env.exchange_sandbox is not None:
        data["exchange"]["sandbox"] = env.exchange_sandbox

    if env.db_path:
        data["storage"]["db_path"] = env.db_path

    if env.log_level:
        data["logging"]["level"] = env.log_level
    if env.log_file:
        data["logging"]["file"] = env.log_file
    if env.log_json is not None:
        data["logging"]["json_logs"] = env.log_json

    # Re-validate: env overrides must still satisfy the pydantic contract.
    return Settings.model_validate(data)


def _merge_universe(settings: Settings, universe_data: dict[str, Any]) -> Settings:
    """Overlay the standalone universe YAML onto the Settings.universe block."""
    if not universe_data:
        return settings
    data = settings.model_dump()
    merged = {**data["universe"], **universe_data}
    data["universe"] = merged
    return Settings.model_validate(data)


def load_settings(
    yaml_path: str | Path = _DEFAULT_YAML,
    universe_path: str | Path = _DEFAULT_UNIVERSE_YAML,
    env_path: str | Path = _DEFAULT_ENV,
) -> Config:
    """Load .env + YAML, merge, validate policy, and return a ``Config``."""
    env = load_env(env_path)

    raw = _load_yaml(Path(yaml_path))
    universe = _load_universe_yaml(Path(universe_path))

    settings = Settings.from_dict(raw)
    settings = _merge_universe(settings, universe)
    settings = _apply_env_overrides(settings, env)

    config = Config(settings=settings, env=env)
    validate_all(config)

    # Configure logging from the now-resolved config so the rest of the app
    # logs consistently. Configured here so a bad config still aborts before
    # any logging side effects from later modules.
    setup_logging(
        level=settings.logging.level,
        file=settings.logging.file,
        json_logs=settings.logging.json_logs,
    )
    return config


# Convenience re-export for callers that only want the Mode.
__all__ = ["Config", "EnvConfig", "Settings", "load_settings", "Mode"]
