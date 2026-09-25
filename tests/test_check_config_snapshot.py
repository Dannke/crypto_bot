"""scripts/check_config_snapshot.py — step 0 of preregistration-guardian.

Exercised the way the guardian runs it: a document with a "## Config Snapshot"
YAML block, an operational config, the CLI as a subprocess, the exit code.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_config_snapshot.py"

CONFIG = """\
risk:
  take_profit_risk_multiple:
    15m: 2.0
    1h: 2.5
    4h: 2.0
  emergency_drawdown_pct: 25.0
"""


def _check(tmp_path: Path, snapshot: str) -> subprocess.CompletedProcess[str]:
    config = tmp_path / "settings.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    doc = tmp_path / "prereg.md"
    doc.write_text(f"# x\n\n## Config Snapshot\n\n```yaml\n{snapshot}```\n", encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(doc), "--config", str(config)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_dict_valued_field_can_be_declared(tmp_path: Path) -> None:
    """Keys of a dict-valued schema field resolve like section fields.

    getattr used to miss dict keys, so declaring take_profit_risk_multiple
    produced a MISMATCH against "<НЕТ ТАКОГО ПОЛЯ В СХЕМЕ>" even when correct.
    """
    result = _check(tmp_path, CONFIG)
    assert result.returncode == 0, result.stdout
    assert "MISMATCH: 0 из 4" in result.stdout


def test_wrong_value_is_a_mismatch(tmp_path: Path) -> None:
    result = _check(tmp_path, CONFIG.replace("1h: 2.5", "1h: 3.0"))
    assert result.returncode == 1
    assert "MISMATCH: 1 из 4" in result.stdout


def test_undeclared_key_is_reported_as_unregistered(tmp_path: Path) -> None:
    result = _check(tmp_path, CONFIG.replace("    4h: 2.0\n", ""))
    assert result.returncode == 0          # UNREGISTERED alone does not fail by default
    assert "risk.take_profit_risk_multiple.4h = 2.0" in result.stdout
