"""The shared directory walk returns what the pathlib walks it replaced returned, without listing pruned dirs."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom_graph.index import walk
from axiom_graph.index.doc_ids import enumerate_markdown_files
from axiom_graph.index.walk import TreeListing, name_matcher, suffix_matcher

SKIP = frozenset({"worktrees", "node_modules", ".pev-scratch", "build", ".git"})


def _tree(root: Path) -> Path:
    files = [
        "a.py",
        "B.PY",
        ".hidden.py",
        "notes.md",
        "pkg/__init__.py",
        "pkg/mod.py",
        "pkg/sub/deep.py",
        "pkg/sub/deep.ts",
        "pkg/sub/Other.Ts",
        "pkg/build/gen.py",
        "pkg/build.py",
        "pkg/data.json",
        "docs/x.md",
        "docs/y.docjson",
        "docs/z.json",
        "docs/nested/w.md",
        "docs/node_modules/kept.md",
        ".claude/settings.json",
        ".claude/skills/s/SKILL.md",
        ".claude/worktrees/wt1/pkg/mod.py",
        ".claude/worktrees/wt1/docs/x.md",
        ".pev-scratch/junk/j.py",
        "src/node_modules/lib/index.ts",
        "src/app/main.ts",
        "src/app/deeper/more.ts",
        "src/main.ts",
        ".git/objects/x.py",
    ]
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    # A directory whose name matches a file pattern: rglob returns it too.
    (root / "pkg" / "dir_named.py").mkdir()
    (root / "pkg" / "dir_named.py" / "inner.py").write_text("x\n", encoding="utf-8")
    return root


def _filtered(paths, root: Path, skip=SKIP) -> list[Path]:
    return [p for p in paths if not any(part in skip for part in p.relative_to(root).parts)]


@pytest.mark.parametrize("pattern", ["*.py", "*.md", "*", "*.ts", "*.json"])
def test_matches_returns_what_rglob_returns_in_its_order(tmp_path: Path, pattern: str) -> None:
    root = _tree(tmp_path)
    assert TreeListing().matches(root, name_matcher([pattern])) == list(root.rglob(pattern))


@pytest.mark.parametrize("pattern", ["*.py", "*.md", "*"])
def test_pruned_matches_drop_exactly_what_the_component_filter_dropped(tmp_path: Path, pattern: str) -> None:
    root = _tree(tmp_path)
    listing = TreeListing()
    assert listing.matches(root, name_matcher([pattern]), skip_names=SKIP) == _filtered(root.rglob(pattern), root)
    listed = {Path(k) for k in listing._listed}
    pruned = [
        root / ".claude" / "worktrees",
        root / ".pev-scratch",
        root / "src" / "node_modules",
        root / ".git",
        root / "pkg" / "build",
    ]
    assert not any(Path(os.path.normcase(p)) in listed for p in pruned)


def test_junk_under_an_excluded_dir_changes_nothing_the_walk_lists(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    before = TreeListing()
    found = before.matches(root, suffix_matcher([".py"]), skip_names=SKIP)
    for i in range(200):
        junk = root / ".claude" / "worktrees" / f"copy{i % 5}" / f"d{i % 3}" / f"j{i}.py"
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_text("x\n", encoding="utf-8")
    after = TreeListing()
    assert after.matches(root, suffix_matcher([".py"]), skip_names=SKIP) == found
    assert after.listings == before.listings
    assert sum(map(len, after._listed.values())) == sum(map(len, before._listed.values()))


def test_files_only_matches_drop_directories_like_is_file(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    got = TreeListing().matches(root, name_matcher(["*.py"]), files_only=True)
    assert got == [p for p in root.rglob("*.py") if p.is_file()]


def test_every_directory_is_listed_once_across_walkers(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    listing = TreeListing()
    listing.matches(root, suffix_matcher([".py"]), skip_names=SKIP)
    first = listing.listings
    listing.matches(root / "docs", suffix_matcher([".md"]))
    listing.matches(root / "docs", suffix_matcher([".json", ".docjson"]), files_only=True)
    listing.matches(root / ".claude", None, skip_names=SKIP, files_only=True)
    # docs/node_modules was pruned from the root walk but belongs to the docs walk: listed once, then shared.
    assert listing.listings == first + 1


@pytest.mark.parametrize(
    "pattern",
    [
        "src/*.ts",
        "src/**/*.ts",
        "**/*.ts",
        "src/**",
        "src/**/",
        "src/*/main.ts",
        "**/**/*.ts",
        "src/../src/*.ts",
        "pkg/sub/*.ts",
        "PKG/SUB/*.TS" if os.path.normcase("A") == "a" else "pkg/sub/*.ts",
        "**",
        "*",
        "src/main.ts",
        "src/",
        "**/*/**",
        "pkg/**/deep.ts",
        "nope/**",
    ],
)
def test_glob_returns_what_path_glob_returns_minus_skipped(tmp_path: Path, pattern: str) -> None:
    root = _tree(tmp_path)
    assert list(TreeListing().glob(root, pattern, skip_names=SKIP)) == _filtered(root.glob(pattern), root)
    assert list(TreeListing().glob(root, pattern)) == list(root.glob(pattern))


@pytest.mark.parametrize("pattern", ["", "src/a**/*.ts"])
def test_glob_rejects_what_path_glob_rejects(tmp_path: Path, pattern: str) -> None:
    root = _tree(tmp_path)
    try:
        expected = list(root.glob(pattern))
    except ValueError:
        with pytest.raises(ValueError):
            list(TreeListing().glob(root, pattern))
    else:
        assert list(TreeListing().glob(root, pattern)) == expected


def _primed(root: Path) -> TreeListing:
    """Return a listing of *root*'s tree whose every directory lists its entries by name."""
    listing = TreeListing()
    for dirpath, _dirs, _files in os.walk(root):
        with os.scandir(dirpath) as it:
            entries = sorted((walk._entry(e) for e in it), key=lambda e: e.name)
        listing._listed[os.path.normcase(dirpath)] = tuple(entries)
    return listing


_DIR_ORDER = {
    "selectors": [".", "a", "a/a1", "b", "b/b1"],
    "walk": [".", "a", "b", "a/a1", "b/b1"],
    "globber": [".", "a", "b", "b/b1", "a/a1"],
}
_TRAILING_STARSTAR = {
    "selectors": _DIR_ORDER["selectors"],
    "walk": _DIR_ORDER["walk"],
    "globber": [".", "a", "b", "x.ts", "b/b1", "b/x.ts", "b/b1/x.ts", "a/a1", "a/x.ts", "a/a1/x.ts"],
}


@pytest.mark.parametrize("model", ["selectors", "walk", "globber"])
def test_each_interpreter_model_walks_in_its_pathlib_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """Pin each pathlib generation's order on a tree whose listings are by name.

    ``selectors`` is Python 3.10/3.11, ``walk`` 3.12, ``globber`` 3.13 and
    later; the comparisons against the running interpreter cover one model,
    these cover all three on any interpreter.
    """
    for rel in ["x.ts", "a/x.ts", "a/a1/x.ts", "b/x.ts", "b/b1/x.ts"]:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(walk, "_GLOB_MODEL", model)
    expected_ts = [tmp_path / d / "x.ts" if d != "." else tmp_path / "x.ts" for d in _DIR_ORDER[model]]
    assert _primed(tmp_path).matches(tmp_path, name_matcher(["*.ts"])) == expected_ts
    assert list(_primed(tmp_path).glob(tmp_path, "**/*.ts")) == expected_ts
    expected_all = [tmp_path / rel if rel != "." else tmp_path for rel in _TRAILING_STARSTAR[model]]
    assert list(_primed(tmp_path).glob(tmp_path, "**")) == expected_all


def test_the_reparse_probe_runs_on_windows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off Windows an entry costs no ``lstat``: ``is_symlink`` already says whether it is a link."""

    class _Entry:
        name = "x"
        stats = 0

        def is_dir(self, follow_symlinks: bool = True) -> bool:
            return False

        def is_file(self, follow_symlinks: bool = True) -> bool:
            return True

        def is_symlink(self) -> bool:
            return False

        def stat(self, follow_symlinks: bool = True) -> SimpleNamespace:
            type(self).stats += 1
            return SimpleNamespace(st_file_attributes=0)

    monkeypatch.setattr(walk, "_IS_WINDOWS", False)
    assert walk._entry(_Entry()).is_link is False
    assert _Entry.stats == 0
    monkeypatch.setattr(walk, "_IS_WINDOWS", True)
    assert walk._entry(_Entry()).is_link is False
    assert _Entry.stats == 1


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows feature")
def test_junctions_are_descended_flagged_and_deduplicated_by_resolved_path(tmp_path: Path) -> None:
    """A junction is no symlink, yet what sits behind it resolves elsewhere: flag it and dedupe by resolving."""
    import _winapi

    root = _tree(tmp_path)
    _winapi.CreateJunction(str(root / "docs" / "nested"), str(root / "docs" / "jn"))
    hits = TreeListing().hits(root / "docs", name_matcher(["*.md"]))
    assert [path for path, _linked in hits] == list((root / "docs").rglob("*.md"))
    flags = dict(hits)
    assert flags[root / "docs" / "jn" / "w.md"] is True
    assert flags[root / "docs" / "nested" / "w.md"] is False
    found = [f.rel_to_root for f in enumerate_markdown_files(root, ["docs"])]
    assert sorted(found) == ["jn/w.md", "node_modules/kept.md", "x.md"]


def test_links_are_listed_not_descended_and_flagged(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    try:
        os.symlink(root / "pkg" / "sub", root / "docs" / "linked_dir", target_is_directory=True)
        os.symlink(root / "docs" / "x.md", root / "docs" / "linked.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    listing = TreeListing()
    assert listing.matches(root, name_matcher(["*.md"])) == list(root.rglob("*.md"))
    hits = dict(listing.hits(root / "docs", name_matcher(["*.md"])))
    assert hits[root / "docs" / "linked.md"] is True
    assert hits[root / "docs" / "x.md"] is False
