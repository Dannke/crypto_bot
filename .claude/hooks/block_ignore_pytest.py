"""Блокировать `--ignore` в реальном вызове pytest.

Старая версия искала подстроки "pytest" и "--ignore" где угодно в команде,
поэтому срабатывала на текст, который лишь записывается в файл (тело heredoc,
документация об этом самом правиле, grep по докам). Это ловило привычку, а не
failure mode.

Здесь команда разбирается на сегменты по shell-разделителям, тела heredoc
выбрасываются (это данные, не команда), и блокировка наступает только если в
одном сегменте есть и вызов pytest, и `--ignore` отдельным аргументом.
"""
from __future__ import annotations

import json
import re
import shlex
import sys

sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

HEREDOC_START = re.compile(r"<<-?\s*[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?")
SEGMENT_SPLIT = re.compile(r"\|\||&&|[;\n|]")
PYTEST_NAMES = {"pytest", "py.test", "pytest.exe"}
PYTHON_NAMES = {"python", "python3", "py", "python.exe", "python3.exe"}


def strip_heredocs(command: str) -> str:
    """Убрать тела heredoc: их содержимое — данные, а не исполняемая команда."""
    lines = command.split("\n")
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        match = HEREDOC_START.search(line)
        if match:
            delimiter = match.group(1)
            i += 1
            while i < len(lines) and lines[i].strip() != delimiter:
                i += 1
        i += 1
    return "\n".join(kept)


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def invokes_pytest(tokens: list[str]) -> bool:
    """True, если сегмент действительно запускает pytest (в т.ч. `python -m pytest`)."""
    for i, token in enumerate(tokens):
        base = _basename(token)
        if base in PYTEST_NAMES:
            return True
        if base in PYTHON_NAMES:
            rest = tokens[i + 1 :]
            for j, arg in enumerate(rest):
                if arg == "-m" and j + 1 < len(rest) and rest[j + 1] == "pytest":
                    return True
    return False


def has_ignore_flag(tokens: list[str]) -> bool:
    return any(token == "--ignore" or token.startswith("--ignore=") for token in tokens)


def should_block(command: str) -> bool:
    for segment in SEGMENT_SPLIT.split(strip_heredocs(command)):
        if not segment.strip():
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            # Незакрытая кавычка и т.п. — не угадываем, применяем грубую проверку.
            tokens = segment.split()
        if invokes_pytest(tokens) and has_ignore_flag(tokens):
            return True
    return False


def main() -> None:
    data = json.load(sys.stdin)
    command = data.get("tool_input", {}).get("command", "")
    if should_block(command):
        print(
            "BLOCKED: --ignore скрывает часть регрессии из отчёта. "
            "Если нужно временно исключить файл — обоснуй явно и спроси пользователя.",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
