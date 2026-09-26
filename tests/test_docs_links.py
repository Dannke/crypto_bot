"""Documentation references resolve — a docs-as-code link check.

Four kinds of references are checked:

1. Full repository paths that start with ``docs/`` in text files across the
   repository (docs, scripts, src, tests, config, .claude, CLAUDE.md,
   README.md). A path right after ``<commit>:`` is a ``git show`` pointer into
   history and is skipped, as is anything from a ``<placeholder>`` onwards.
2. Relative markdown links ``[text](target)`` inside ``docs/``.
3. Anchors of those links: ``target.md#anchor`` must name a heading of the
   target file, slugged the way GitHub renders it. A link to a section keeps
   pointing at that section's content: when a path is later reused for a
   different document, the file still exists but the heading does not
   (docs/README.md, "Перенос 2026-09-27 — путь переиспользуется").
4. ``@`` imports in CLAUDE.md files, parsed the way Claude Code parses them.

The documentation was restructured once already because such references had
rotted silently (docs/README.md, "Перемещённые и удалённые документы").
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from functools import cache
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("docs", "scripts", "src", "tests", "config", ".claude")
SCAN_FILES = ("CLAUDE.md", "README.md")
SUFFIXES = {".md", ".py", ".yaml", ".yml", ".toml", ".txt"}
CLAUDE_MD_FILES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md")

# Not preceded by a word character, ':' (git show pointer) or '/' (part of a longer path).
DOCS_PATH = re.compile(r"(?<![\w:/])docs/[A-Za-z0-9_./-]*[A-Za-z0-9_/-]|(?<![\w:/])docs/")
# Target (empty for a link into the same file) and optional anchor.
MARKDOWN_LINK = re.compile(r"\]\(([^)\s#]*)(?:#([^)\s]*))?\)")
CLAUDE_IMPORT = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
NOT_SCANNED_FOR_IMPORTS = re.compile(r"^```.*?^```|`[^`\n]*`|<!--.*?-->", re.MULTILINE | re.DOTALL)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
ATX_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
HTML_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"")
CODE_SPAN = re.compile(r"(`+).+?\1")


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


def _github_slug(heading: str) -> str:
    """Anchor GitHub gives a heading, as github-slugger computes it.

    The rendered text is lower-cased and every character other than a letter, a
    mark, a decimal digit, a hyphen, an underscore or a space is dropped; spaces
    then become hyphens, so ``a — b`` gives ``a--b``. Marks matter: an emoji's
    variation selector U+FE0F survives while the emoji itself does not. Checked
    2026-09-27 against the rendered repository files: 102 of 102 anchors of four
    documents under docs/research/ matched.
    """
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links and images keep their text
    text = re.sub(r"<[^>]+>", "", text).replace("`", "")         # tags and code-span ticks go
    kept = (
        ch for ch in text.lower()
        if ch in "-_ " or unicodedata.category(ch)[0] in "LM" or unicodedata.category(ch) == "Nd"
    )
    return "".join(kept).replace(" ", "-")


def _outside_fences(text: str) -> Iterator[tuple[int, str]]:
    """Numbered lines of a markdown text that are not inside fenced code blocks."""
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if opening := FENCE.match(line):
            marker = opening.group(1)
            if fence is None:
                fence = marker[0] * len(marker)
            elif marker.startswith(fence):
                fence = None
            continue
        if fence is None:
            yield number, line


def _heading_anchors(text: str) -> set[str]:
    """Anchors of a markdown text: ATX headings outside fenced code, plus ``<a id>``.

    A repeated slug gets ``-1``, ``-2``… in document order, the way GitHub numbers
    duplicate headings.
    """
    anchors: set[str] = set(HTML_ANCHOR.findall(text))
    occurrences: dict[str, int] = {}
    for _, line in _outside_fences(text):
        if not (heading := ATX_HEADING.match(line)):
            continue
        base = _github_slug(heading.group(1))
        slug = base
        while slug in occurrences:
            occurrences[base] += 1
            slug = f"{base}-{occurrences[base]}"
        occurrences[slug] = 0
        anchors.add(slug)
    return anchors


@cache
def _anchors_of(path: Path) -> frozenset[str]:
    return frozenset(_heading_anchors(path.read_text(encoding="utf-8")))


def _broken_links(path: Path) -> Iterator[str]:
    """Relative links of a markdown file whose file or section does not exist.

    Code is not scanned: GitHub renders no link inside a code span or a fenced block.
    """
    for number, line in _outside_fences(path.read_text(encoding="utf-8")):
        for match in MARKDOWN_LINK.finditer(CODE_SPAN.sub("", line)):
            target, anchor = match.group(1), match.group(2)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (path.parent / target).resolve() if target else path
            if not resolved.exists():
                yield f"{number}: {target}"
            elif anchor and resolved.suffix == ".md" and unquote(anchor) not in _anchors_of(resolved):
                yield f"{number}: {target}#{anchor} — no such heading in the target"


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
    broken = [
        f"{path.relative_to(ROOT)}:{problem}"
        for path in (ROOT / "docs").rglob("*.md")
        for problem in _broken_links(path)
    ]
    assert not broken, "broken markdown links in docs/:\n" + "\n".join(broken)


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


def test_heading_slugs_match_github() -> None:
    """Pinned against the GitHub rendering of these headings in docs/research/ (2026-09-27)."""
    rendered = {
        "0.2 Не переиспользуется без изменений — вопреки брифу":
            "02-не-переиспользуется-без-изменений--вопреки-брифу",
        "Funding/basis, цикл 1 — Task 0: гипотеза, decision rule и сетка":
            "fundingbasis-цикл-1--task-0-гипотеза-decision-rule-и-сетка",
        "Приложение B. Покрытие данных — только временные метки":
            "приложение-b-покрытие-данных--только-временные-метки",
        "Поправка 2 (2026-09-26, после заморозки регистрации `c0b0375`, до прогона walk-forward): "
        "AVAX не делистингован — исключение держится на данных, а не на спецификации":
            "поправка-2-2026-09-26-после-заморозки-регистрации-c0b0375-до-прогона-walk-forward-"
            "avax-не-делистингован--исключение-держится-на-данных-а-не-на-спецификации",
        "\u26a0\ufe0f Кавеаты (явно зафиксированы)": "\ufe0f-кавеаты-явно-зафиксированы",
    }
    assert {heading: _github_slug(heading) for heading in rendered} == rendered


def test_the_anchor_check_catches_a_missing_section(tmp_path: Path) -> None:
    """Negative control: an existing file with a missing heading is still a broken link."""
    (tmp_path / "target.md").write_text(
        "# Документ\n\n## Итог\n\n## Итог\n\n```\n## не заголовок\n```\n", encoding="utf-8")
    (tmp_path / "source.md").write_text(
        "## Раздел\n"
        "[ok](target.md#итог) [ok-dup](target.md#итог-1) "
        "[ok-encoded](target.md#%D0%B8%D1%82%D0%BE%D0%B3) [ok-self](#раздел)\n"
        "[gone](target.md#итог-2) [fenced](target.md#не-заголовок) [self](#нет)\n"
        "`[in-code](nope.md#x)`\n```\n[in-fence](nope.md)\n```\n",
        encoding="utf-8",
    )
    assert [problem.split(":")[0] for problem in _broken_links(tmp_path / "source.md")] == ["3", "3", "3"]
