"""A build reads each file once and lists each directory once, and never walks an excluded directory."""

from __future__ import annotations

import builtins
import io
import os
from collections import Counter
from pathlib import Path

import pytest
from axiom_annotations import workflow

from axiom_graph.index import builder, db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index
from tests.fixtures.full_recompute import assert_matches_full_recompute

_DOC = '{"title": "%s", "sections": [{"id": "intro", "heading": "Intro", "content": "Hello %s"}]}'

_FILES = {
    "axiom-graph.toml": (
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\n'
        'docs_dirs = ["docs"]\nconfig_dirs = [".claude"]\nexclude_dirs = ["scratch"]\n'
    ),
    "pkg/__init__.py": "",
    "pkg/mod.py": "def f():\n    return 1\n",
    "pkg/sub/deep.py": "def g():\n    return f()\n",
    "pkg/build/gen.py": "def generated():\n    return 0\n",
    "pkg/sub/node_modules/lib.py": "def vendored():\n    return 0\n",
    "pkg/scratch/tmp.py": "def tmp():\n    return 0\n",
    "scratch/top.py": "def top():\n    return 0\n",
    ".claude/settings.json": '{"a": 1}\n',
    ".claude/skills/s/SKILL.md": "# Skill\n\nDoes things.\n",
    ".claude/worktrees/wt/pkg/mod.py": "def f():\n    return 2\n",
    ".claude/worktrees/wt/.claude/settings.json": '{"a": 2}\n',
    "docs/guide.md": "# Guide\n\n## Usage\n\nUse it.\n",
    "docs/node_modules/kept.md": "# Kept\n\n## Part\n\nA docs root is walked whole.\n",
    "docs/build/also_kept.md": "# Also kept\n\n## Part\n\nText.\n",
    "docs/spec.docjson": _DOC % ("Spec", "spec"),
    "docs/release.notes.docjson": _DOC % ("Release notes", "notes"),
    "docs/nested/more.docjson": _DOC % ("More", "more"),
    "docs/data.json": '{"not": "a document"}\n',
}


def _project(root: Path) -> Path:
    for rel, text in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _build(root: Path):
    return build_index(_db_path(str(root)), root)


def _locations(root: Path) -> set[str]:
    with db._connect(_db_path(str(root))) as conn:
        rows = conn.execute("SELECT DISTINCT location FROM nodes").fetchall()
    return {r[0].replace("\\", "/") for r in rows if r[0] and r[0] != "external"}


def _key(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _count_opens(monkeypatch: pytest.MonkeyPatch) -> Counter:
    opens: Counter = Counter()
    real = io.open

    def counting_open(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, os.PathLike)) and "r" in mode and "+" not in mode:
            opens[_key(file)] += 1
        return real(file, mode, *args, **kwargs)

    monkeypatch.setattr(io, "open", counting_open)
    monkeypatch.setattr(builtins, "open", counting_open)
    return opens


class _Listed:
    def __init__(self, entries: list) -> None:
        self._entries = entries

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def __iter__(self):
        return iter(self._entries)

    def close(self) -> None:
        return None


def _count_listings(monkeypatch: pytest.MonkeyPatch) -> tuple[Counter, Counter]:
    listings: Counter = Counter()
    entries: Counter = Counter()
    real = os.scandir

    def counting_scandir(path="."):
        with real(path) as it:
            found = list(it)
        listings[_key(path)] += 1
        entries[_key(path)] += len(found)
        return _Listed(found)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    return listings, entries


@workflow(
    purpose="A no-change build opens every file the index tracks once -- the walk's read serves the parse "
    "decision, the doc-id checks and the staleness refresh -- and its stored statuses match a full recompute"
)
def test_a_no_change_build_opens_each_tracked_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    _build(root)
    _build(root)
    tracked = _locations(root)
    assert {"pkg/mod.py", "docs/spec.docjson", "docs/release.notes.docjson", ".claude/settings.json"} <= tracked

    opens = _count_opens(monkeypatch)
    summary = _build(root)
    monkeypatch.undo()

    assert summary.files_scanned == 0
    per_file = {loc: opens[_key(root / loc)] for loc in tracked}
    assert max(per_file.values()) <= 1, {loc: n for loc, n in per_file.items() if n > 1}
    assert_matches_full_recompute(_db_path(str(root)), root)


@workflow(
    purpose="Files added under excluded directories (a worktrees copy, a configured scratch dir) change neither "
    "what a build lists nor what it indexes"
)
def test_junk_under_an_excluded_dir_leaves_the_walk_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    _build(root)
    _build(root)
    listings, entries = _count_listings(monkeypatch)
    _build(root)
    monkeypatch.undo()
    before = (sum(listings.values()), sum(entries.values()))
    nodes_before = _locations(root)

    for i in range(60):
        for base in (".claude/worktrees/wt2", "scratch/deeper", "pkg/sub/node_modules/more"):
            junk = root / base / f"d{i % 4}" / f"junk_{i}.py"
            junk.parent.mkdir(parents=True, exist_ok=True)
            junk.write_text(f"def junk_{i}():\n    return {i}\n", encoding="utf-8")

    listings, entries = _count_listings(monkeypatch)
    _build(root)
    monkeypatch.undo()
    assert (sum(listings.values()), sum(entries.values())) == before
    assert _locations(root) == nodes_before


@workflow(
    purpose="One build lists each directory at most once: the code walk, the doc-id checks, both doc scanners "
    "and the config scanner share one listing of the docs tree"
)
def test_each_directory_is_listed_once_per_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    _build(root)
    listings, _entries = _count_listings(monkeypatch)
    _build(root)
    monkeypatch.undo()
    assert listings, "the build listed nothing"
    assert max(listings.values()) == 1, {path: n for path, n in listings.items() if n > 1}
    docs = _key(root / "docs")
    assert listings[docs] == 1
    assert listings[_key(root / "docs" / "nested")] == 1


def _expected_locations(root: Path) -> set[str]:
    """What the component-filtered rglob walks indexed: the semantics the pruned walk must keep."""
    skip = builder._BASE_SKIP_DIRS | {"scratch"}

    def kept(path: Path) -> bool:
        return not any(part in skip for part in path.relative_to(root).parts)

    found = {p for p in root.rglob("*.py") if kept(p)}
    found |= set((root / "docs").rglob("*.md"))
    found |= {p for p in (root / "docs").rglob("*.docjson") if p.is_file()}
    found |= {
        p
        for p in (root / ".claude").rglob("*")
        if p.is_file() and p.suffix.lower() in {".md", ".json", ".yaml", ".yml", ".toml"} and kept(p)
    }
    return {p.relative_to(root).as_posix() for p in found}


@workflow(
    purpose="Pruning excluded directories indexes exactly the files the walk-then-filter scan indexed, nested "
    "excluded directories included, while a docs root is still walked whole"
)
def test_pruned_walk_indexes_what_the_filtered_walk_indexed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _build(root)
    locations = _locations(root)
    assert locations == _expected_locations(root)
    assert "docs/node_modules/kept.md" in locations
    assert not {loc for loc in locations if "worktrees" in loc or "scratch" in loc or "/build/gen" in loc}
