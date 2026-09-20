import json
import subprocess
import sys
from pathlib import Path

sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

PENDING_FILE = Path(__file__).parent / ".mypy_pending"

data = json.load(sys.stdin)

# Avoid an infinite Stop loop: if this hook already blocked once this turn,
# let it stop regardless of remaining issues (single-shot enforcement).
if data.get("stop_hook_active"):
    sys.exit(0)

if not PENDING_FILE.exists():
    sys.exit(0)

files = [f for f in PENDING_FILE.read_text(encoding="utf-8").splitlines() if f.strip()]
PENDING_FILE.unlink()

if not files:
    sys.exit(0)

result = subprocess.run(
    ["mypy", *files],
    capture_output=True,
    text=True,
)

if result.returncode != 0:
    print(
        "mypy strict нашёл проблемы в файлах, изменённых за этот ход:\n"
        + result.stdout,
        file=sys.stderr,
    )
    sys.exit(2)

sys.exit(0)
