"""Environment configuration (``.env``) and the merged ``Config`` object.

Kept separate from ``settings.py`` (the loader) and from ``schemas.py`` (the
YAML contract) to avoid a circular import:

    env.py        -> schemas, core.enums        (no validators)
    validators.py -> env.py, schemas, exceptions, policy
    settings.py   -> env.py, validators, schemas

``EnvConfig`` reads the infrastructure / safety knobs from ``.env`` via
pydantic-settings. These values can *override* matching YAML fields but, unlike
YAML, they are the only place where kill-switches and secrets may live.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from ..core.enums import Mode
from .schemas import Settings


class EnvConfig(BaseSettings):
    """Secrets + safety switches read from ``.env`` (case-insensitive)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Operating mode override (YAML default wins if this is unset).
    crypto_bot_mode: Mode | None = None

    # ---- Kill switches ----
    enable_trading: bool = False
    enable_live_trading: bool = False

    # ---- Exchange ----
    exchange_name: str | None = None
    exchange_api_key: str = ""
    exchange_api_secret: str = ""
    exchange_sandbox: bool | None = None

    # ---- Storage / logging overrides ----
    db_path: str | None = None
    log_level: str | None = None
    log_file: str | None = None
    log_json: bool | None = None


@dataclass(frozen=True, slots=True)
class Config:
    """Fully resolved, validated configuration handed to the rest of the app.

    ``settings`` holds the structured strategy/risk/universe contract.
    ``env`` holds secrets + kill-switches (never serialised to logs in full).
    """

    settings: Settings
    env: EnvConfig

    @property
    def mode(self) -> Mode:
        return self.settings.runtime.mode


def load_env(env_path: Path | str = ".env") -> EnvConfig:
    """Read ``.env`` from ``env_path``; missing file yields an empty-ish config."""
    p = Path(env_path)
    # Passing _env_file lets us target a non-default path.
    return EnvConfig(_env_file=str(p) if p.exists() else "")  # type: ignore[call-arg]
