"""Typed domain exceptions.

Organised by layer so the orchestrator can catch at the right granularity:
broad ``CryptoBotError`` for "anything in this app", or a specific subclass
to react (e.g. ``LiveTradingForbiddenError`` -> abort startup; ``DataFeedError``
-> skip symbol + retry).

These are *control-flow* errors, not crash reports: every one carries a
human-readable message so logs explain what happened without a traceback.
"""
from __future__ import annotations


class CryptoBotError(Exception):
    """Base for every error raised by the bot."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class ConfigError(CryptoBotError):
    """The configuration is structurally valid but semantically wrong.

    Raised by validators when a cross-field invariant fails (bad mode/timeframe
    combination, malformed universe, etc.). Startup must abort.
    """


class LiveTradingForbiddenError(ConfigError):
    """Live trading was requested but a safety gate blocked it.

    Raised when ``mode == LIVE`` while ``ENABLE_LIVE_TRADING`` is false,
    when paper trading/tests are not yet released, when sandbox is left on,
    or when API credentials are missing. This is intentional, not a bug.
    """


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
class DataFeedError(CryptoBotError):
    """Market-data retrieval failed for a symbol/timeframe (network, parse)."""


class ExchangeError(CryptoBotError):
    """Low-level ccxt / exchange communication problem."""


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #
class StrategyError(CryptoBotError):
    """A pure-strategy function received inconsistent inputs.

    Indicates a programming/config bug rather than a market condition.
    """


class InsufficientDataError(StrategyError):
    """Not enough candles to compute the requested indicators."""


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
class StorageError(CryptoBotError):
    """SQLite read/write/migration failure."""


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
class ExecutionError(CryptoBotError):
    """An executor could not carry out a requested action."""


class OrderError(ExecutionError):
    """An order-specific failure (rejected, not filled, unknown status)."""


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
class OrchestratorError(CryptoBotError):
    """The main scan loop crashed and could not continue.

    Raised by ``run_orchestrator`` when an unexpected exception escapes the
    per-cycle error handling, signalling that the whole process must stop
    rather than just skipping a cycle.
    """