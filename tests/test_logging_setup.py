"""Logging setup tests."""
from __future__ import annotations

import json
import logging

from crypto_bot.core.logging_setup import JsonFormatter


def test_json_formatter_keeps_extra_fields():
    record = logging.LogRecord(
        name="crypto_bot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.event = "decision"
    record.symbol = "BTC/USDT"

    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "hello world"
    assert payload["event"] == "decision"
    assert payload["symbol"] == "BTC/USDT"
