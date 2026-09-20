import json
import subprocess
import sys
from pathlib import Path

sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

PENDING_FILE = Path(__file__).parent / ".mypy_pending"

data = json.load(sys.stdin)
path = data.get("tool_input", {}).get("file_path", "")

if not path.endswith(".py"):
    sys.exit(0)

result = subprocess.run(
    ["ruff", "check", "--fix", path],
    capture_output=True,
    text=True,
)
if result.stdout.strip():
    print(result.stdout, file=sys.stderr)

if "/src/crypto_bot/" in path.replace("\\", "/"):
    pending = set()
    if PENDING_FILE.exists():
        pending = set(PENDING_FILE.read_text(encoding="utf-8").splitlines())
    pending.add(path)
    PENDING_FILE.write_text("\n".join(sorted(pending)), encoding="utf-8")

sys.exit(0)
