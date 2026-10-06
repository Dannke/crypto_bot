"""Тест 8 Task 1: прогон цикла 2 MR воспроизводится на текущем коде число в число.

План — docs/research/funding-basis/plan.md, раздел 9, Task 1. Исправление учёта
фандинга не должно сдвинуть прогон, в котором фандинга не было: те же строки
decisions / equity / positions / trades в БД окон и тот же вывод
scripts/mr_decision_rule.py, что опубликован в
docs/research/mean-reversion/cycle-2/4-closure.md. Логика — в
scripts/reproduce_mr_cycle2.py, тест её только запускает.

Запуск явный: CRYPTO_BOT_REPRODUCE_MR_CYCLE2=1 — два окна walk-forward идут около
получаса. Без этой переменной или без данных на диске (data/ git не отслеживает)
тест пропускается, причина видна в сводке pytest.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    ROOT / "data" / "crypto_bot.db",
    ROOT / "data" / "cache" / "bybit_instruments_2026-09-26T0709Z.json",
    ROOT / "data" / "backtests" / "mr_cycle2_production" / "validation.db",
    ROOT / "data" / "backtests" / "mr_cycle2_production" / "test.db",
]

pytestmark = pytest.mark.skipif(
    os.environ.get("CRYPTO_BOT_REPRODUCE_MR_CYCLE2") != "1",
    reason="прогон двух окон walk-forward ~30 мин; включается CRYPTO_BOT_REPRODUCE_MR_CYCLE2=1",
)


def test_mr_cycle2_reproduces_number_for_number(tmp_path) -> None:
    missing = [str(p.relative_to(ROOT)) for p in REQUIRED if not p.exists()]
    if missing:
        pytest.skip(f"нет на диске: {missing}")

    result = subprocess.run(
        [sys.executable, "scripts/reproduce_mr_cycle2.py", "--db-dir", str(tmp_path)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    print(result.stdout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "VERDICT: REPRODUCED" in result.stdout
