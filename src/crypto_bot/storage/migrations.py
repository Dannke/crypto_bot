"""Migration SQL loader."""
from __future__ import annotations

from pathlib import Path

_MIGRATIONS_FILE = Path(__file__).parent / "migrations.sql"

__all__ = ["_MIGRATIONS_FILE"]