"""Centralised logging: one root configuration + a decision journal.

Design choices:
  * One call to ``setup_logging`` configures the root logger. Idempotent: it
    clears previously installed handlers so re-imports (tests, re-runs) don't
    produce duplicate lines.
  * Two formatters: human-readable plaintext (default) and JSON (for log
    aggregation). Selected per the ``json`` flag.
  * Structured helpers (``log_decision`` / ``log_event``) normalise how we
    record audit-worthy events, so the decision journal is queryable later.
"""
from __future__ import annotations

import contextlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .types import DecisionRecord

_LOGGER_NAME = "crypto_bot"

# Known log levels; anything else is rejected loudly.
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_RESERVED_LOG_RECORD_ATTRS = set(
    logging.LogRecord(
        name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
    ).__dict__
) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per record. ``extra`` fields are merged in."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Pull custom attributes added via logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _resolve_level(level: str) -> int:
    level = (level or "INFO").upper()
    if level not in _VALID_LEVELS:
        raise ValueError(f"invalid log level '{level}'; expected one of {_VALID_LEVELS}.")
    return int(getattr(logging, level))


def setup_logging(
    level: str = "INFO",
    file: str | Path | None = None,
    json_logs: bool = False,
) -> logging.Logger:
    """Configure and return the project logger.

    Args:
        level: root log level name.
        file: optional path to a rotating log file (created if missing).
        json_logs: if True, emit JSON records; otherwise plaintext.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(_resolve_level(level))

    # Idempotent: remove handlers from a prior call.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        # Closing must never break setup; suppress any error.
        with contextlib.suppress(Exception):
            handler.close()

    fmt_cls = JsonFormatter if json_logs else logging.Formatter
    fmt = (
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        if not json_logs
        else None
    )
    formatter = fmt_cls(fmt) if not json_logs else fmt_cls()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    if file:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger under the project namespace."""
    return logging.getLogger(_LOGGER_NAME if not name else f"{_LOGGER_NAME}.{name}")


# --------------------------------------------------------------------------- #
# Structured audit helpers
# --------------------------------------------------------------------------- #
def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit an INFO event with attached structured fields."""
    logger.info(event, extra={"event": event, **fields})


def log_decision(logger: logging.Logger, decision: DecisionRecord) -> None:
    """Record an accept/reject decision in the decision journal.

    Every candidate — whether it became a trade or was filtered out — is logged
    here with a reason, so post-hoc analysis can explain *why* a symbol was
    skipped, not just what was traded.
    """
    level = logging.INFO if decision.accepted else logging.DEBUG
    outcome = "ACCEPT" if decision.accepted else "REJECT"
    reason = decision.reason.value if decision.reason else "ok"
    logger.log(
        level,
        "decision %s %s reason=%s detail=%s",
        outcome,
        decision.symbol,
        reason,
        decision.detail,
        extra={
            "event": "decision",
            "symbol": decision.symbol,
            "accepted": decision.accepted,
            "reject_reason": reason,
            "detail": decision.detail,
            "score": decision.score,
            "signal": decision.signal.value if decision.signal else None,
        },
    )
