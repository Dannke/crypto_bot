"""Documentation references resolve — a docs-as-code link check.

Three kinds of references are checked:

1. Full repository paths that start with ``docs/`` in text files across the
   repository (docs, scripts, src, tests, config, .claude, CLAUDE.md,
   README.md). A path right after ``<commit>:`` is a ``git show`` pointer into
   history and is skipped, as is anything from a ``<placeholder>`` onwards.
2. Relative markdown links ``[text](target)`` inside ``docs/``.
3. ``@`` imports in CLAUDE.md files, parsed the way Claude Code parses them.

The documentation was restructured once already because such references had
rotted silently (docs/README.md, "Перемещённые и удалённые документы").
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("docs", "scripts", "src", "tests", "config", ".claude")
SCAN_FILES = ("CLAUDE.md", "README.md")
SUFFIXES = {".md", ".py", ".yaml", ".yml", ".toml", ".txt"}
CLAUDE_MD_FILES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")

# Not preceded by a word character, ':' (git show pointer) or '/' (part of a longer path).
DOCS_PATH = re.compile(r"(?<![\w:/])docs/[A-Za-z0-9_./-]*[A-Za-z0-9_/-]|(?<![\w:/])docs/")
MARKDOWN_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
CLAUDE_IMPORT = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
NOT_SCANNED_FOR_IMPORTS = re.compile(r"^```.*?^```|`[^`\n]*`|<!--.*?-->", re.MULTILINE | re.DOTALL)


def _text_files() -> Iterator[Path]:
    for directory in SCAN_DIRS:
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and path.suffix in SUFFIXES and "__pycache__" not in path.parts:
                yield path
    for name in SCAN_FILES:
        yield ROOT / name


def _lines(path: Path) -> Iterator[tuple[int, str]]:
    yield from enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)


def _claude_imports(text: str) -> Iterator[str]:
    """Import targets as Claude Code extracts them (grammar copied from its 2.1.281 bundle).

    A target is the whole run of non-space characters after ``@``, cut at ``#``, so
    trailing punctuation stays in it: ``@docs/README.md.`` names ``README.md.``.
    Code spans, fenced blocks and HTML comments are not scanned.
    """
    for match in CLAUDE_IMPORT.finditer(NOT_SCANNED_FOR_IMPORTS.sub(" ", text)):
        target = match.group(1).split("#", 1)[0].replace("\\ ", " ")
        if (target.startswith(("~/", "/")) and target != "/") or re.match(r"[A-Za-z0-9._-]", target):
            yield target


def _exact_file(base: Path, target: str) -> Path | None:
    """The file ``target`` names relative to ``base``, if every component matches exactly.

    ``Path.is_file`` is not enough: on Windows it accepts ``README.md.`` and
    ``readme.md`` for ``README.md``, while Claude Code loads nothing for
    ``@docs/README.md.`` (checked 2026-09-26 on an isolated copy of CLAUDE.md).
    """
    path = Path.home() if target.startswith("~/") else base
    for part in PurePosixPath(target.removeprefix("~/")).parts:
        if part == "/":
            path = Path(path.anchor)
        elif part == "..":
            path = path.parent
        elif path.is_dir() and part in {child.name for child in path.iterdir()}:
            path = path / part
        else:
            return None
    return path if path.is_file() else None


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


def test_claude_md_imports_resolve() -> None:
    """Every ``@`` import in the CLAUDE.md files, followed recursively, loads a file.

    Claude Code skips an import that names no file without a warning, so a broken
    import shows up only as context the session silently lacks.
    """
    pending = [ROOT / name for name in CLAUDE_MD_FILES if (ROOT / name).is_file()]
    pending += [path for directory in SCAN_DIRS for path in (ROOT / directory).rglob("CLAUDE.md")]
    seen: set[Path] = set()
    missing = []
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for target in _claude_imports(path.read_text(encoding="utf-8")):
            imported = _exact_file(path.parent, target)
            if imported is None:
                shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
                missing.append(f"{shown}: @{target}")
            else:
                pending.append(imported)
    assert not missing, "CLAUDE.md imports that load nothing:\n" + "\n".join(missing)


def test_the_check_catches_a_dangling_path() -> None:
    """Negative control: the pattern finds a real path and skips history pointers.

    The sample path is assembled at runtime so this file does not trip the scan.
    """
    dangling = "docs" + "/research/nope.md"
    found = [m.group(0) for m in DOCS_PATH.finditer(
        f"see {dangling} and git show 6e88fe8:{dangling}")]
    assert found == [dangling]
    assert not (ROOT / found[0]).exists()


def test_the_import_check_keeps_trailing_punctuation() -> None:
    """Negative control: '@path.' is read as Claude Code reads it, not as prose."""
    text = "map: @docs/README.md. more: @a.md, @b.md `@in/code.md` <!-- @in/note.md --> a@b.c"
    assert list(_claude_imports(text)) == ["docs/README.md.", "a.md,", "b.md"]
    assert _exact_file(ROOT, "docs/README.md") == ROOT / "docs" / "README.md"
    assert _exact_file(ROOT, "docs/README.md.") is None
