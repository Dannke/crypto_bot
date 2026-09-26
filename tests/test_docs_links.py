"""Documentation references resolve — a docs-as-code link check.

Two kinds of references are checked:

1. Full repository paths that start with ``docs/`` in text files across the
   repository (docs, scripts, src, tests, config, .claude, CLAUDE.md,
   README.md). A path right after ``<commit>:`` is a ``git show`` pointer into
   history and is skipped, as is anything from a ``<placeholder>`` onwards.
2. Relative markdown links ``[text](target)`` inside ``docs/``.

The documentation was restructured once already because such references had
rotted silently (docs/README.md, "Перемещённые и удалённые документы").
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("docs", "scripts", "src", "tests", "config", ".claude")
SCAN_FILES = ("CLAUDE.md", "README.md")
SUFFIXES = {".md", ".py", ".yaml", ".yml", ".toml", ".txt"}

# Not preceded by a word character, ':' (git show pointer) or '/' (part of a longer path).
DOCS_PATH = re.compile(r"(?<![\w:/])docs/[A-Za-z0-9_./-]*[A-Za-z0-9_/-]|(?<![\w:/])docs/")
MARKDOWN_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")


def _text_files() -> Iterator[Path]:
    for directory in SCAN_DIRS:
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and path.suffix in SUFFIXES and "__pycache__" not in path.parts:
                yield path
    for name in SCAN_FILES:
        yield ROOT / name


def _lines(path: Path) -> Iterator[tuple[int, str]]:
    yield from enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)


def test_full_docs_paths_exist() -> None:
    missing = [
        f"{path.relative_to(ROOT)}:{number}: {match.group(0)}"
        for path in _text_files()
        for number, line in _lines(path)
        for match in DOCS_PATH.finditer(line)
        if not (ROOT / match.group(0)).exists()
    ]
    assert not missing, "dangling docs/ paths:\n" + "\n".join(missing)


def test_relative_markdown_links_in_docs_resolve() -> None:
    missing = []
    for path in (ROOT / "docs").rglob("*.md"):
        for number, line in _lines(path):
            for match in MARKDOWN_LINK.finditer(line):
                target = match.group(1)
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                if not (path.parent / target).resolve().exists():
                    missing.append(f"{path.relative_to(ROOT)}:{number}: {target}")
    assert not missing, "broken markdown links in docs/:\n" + "\n".join(missing)


def test_the_check_catches_a_dangling_path() -> None:
    """Negative control: the pattern finds a real path and skips history pointers.

    The sample path is assembled at runtime so this file does not trip the scan.
    """
    dangling = "docs" + "/research/nope.md"
    found = [m.group(0) for m in DOCS_PATH.finditer(
        f"see {dangling} and git show 6e88fe8:{dangling}")]
    assert found == [dangling]
    assert not (ROOT / found[0]).exists()
