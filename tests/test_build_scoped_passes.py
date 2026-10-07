"""Build's per-file passes cover what the build changed, and store what a full build stores.

Rename detection, broken-link flagging, the doc-section FTS re-sync and the
staleness refresh after a build all start from the files the build parsed
(and the ids it deleted).  Each case here compares an incremental build with
a full one (every file parsed, after clearing the parse records) from the
same index and the same edit.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.index import builder, db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index
from tests.fixtures.full_recompute import assert_matches_full_recompute

_MOD = (
    "def f(items):\n"
    "    total = 0\n"
    "    for item in items:\n"
    "        if item > 10:\n"
    "            total += item * 2\n"
    "        else:\n"
    "            total -= item\n"
    "    return total\n"
)
_OTHER = "def g(a, b):\n    return a * b + a - b\n\n\ndef keep():\n    return 'kept'\n"


def _project(root: Path, *, git: bool = False) -> Path:
    pkg = root / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(_MOD, encoding="utf-8")
    (pkg / "other.py").write_text(_OTHER, encoding="utf-8")
    (root / ".gitignore").write_text(".axiom_graph/\n", encoding="utf-8")
    if git:
        if shutil.which("git") is None:
            pytest.skip("git is not installed")
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(
                ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                cwd=root,
                check=True,
                capture_output=True,
            )
    return root


def _dbp(root: Path) -> Path:
    return _db_path(str(root))


def _copy_db(src: Path, dst: Path) -> None:
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)


def _full_build_from(root: Path, snapshot: Path) -> object:
    """Restore *snapshot* and build with every file parsed (the parse records cleared)."""
    _copy_db(snapshot, _dbp(root))
    with db._connect(_dbp(root)) as conn:
        db.clear_file_records_conn(conn)
    return build_index(_dbp(root), root)


def _outcome(root: Path) -> tuple[list, list]:
    with db._connect(_dbp(root)) as conn:
        renames = sorted((r[0], r[1]) for r in conn.execute("SELECT old_id, new_id FROM node_renames"))
        rows = sorted((r[0], r[1], r[2]) for r in conn.execute("SELECT id, own_status, link_status FROM nodes"))
    return renames, rows


def _move_g_into_mod(root: Path) -> None:
    (root / "pkg" / "other.py").write_text("def keep():\n    return 'kept'\n", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text(_MOD + "\n\ndef g(a, b):\n    return a * b + a - b\n", encoding="utf-8")


def _rename_f_in_file(root: Path) -> None:
    (root / "pkg" / "mod.py").write_text(_MOD.replace("def f(", "def f_total("), encoding="utf-8")


def _delete_other(root: Path) -> None:
    (root / "pkg" / "other.py").unlink()


_TS_APP = "export function add(a: number, b: number): number {\n  const s = a + b;\n  return s * 2 - a;\n}\n"


def _rename_ts_function(root: Path) -> None:
    (root / "web" / "app.ts").write_text(_TS_APP.replace("function add(", "function add_twice("), encoding="utf-8")


def _with_ts(root: Path) -> None:
    """Configure a TS source tree before the project is indexed (or committed)."""
    from axiom_graph.scanners.js_scanner import HAS_TREE_SITTER

    if not HAS_TREE_SITTER:
        pytest.skip("tree-sitter (the js extra) is not installed")
    (root / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\njs_paths = ["web/*.ts"]\n', encoding="utf-8"
    )
    (root / "web").mkdir(parents=True, exist_ok=True)
    (root / "web" / "app.ts").write_text(_TS_APP, encoding="utf-8")


_EDITS = {
    "in_file_rename": _rename_f_in_file,
    "cross_file_move": _move_g_into_mod,
    "deletion_only": _delete_other,
    "ts_only_rename": _rename_ts_function,
}
_PARSED = {"in_file_rename": 1, "cross_file_move": 2, "deletion_only": 0}


def _rename_skips(root: Path, summary) -> tuple[list[str], list[tuple]]:
    """Return the build's rename-skip warnings and the index's ``RENAME_SCORING_SKIPPED`` rows."""
    warned = sorted(w for w in summary.warnings if "similarity skipped" in w)
    with db._connect(_dbp(root)) as conn:
        rows = sorted(
            (r[0], r[1])
            for r in conn.execute("SELECT node_id, meta FROM node_history WHERE change_type = 'RENAME_SCORING_SKIPPED'")
        )
    return warned, rows


@workflow(
    purpose="An incremental build detects the renames a full build detects for the same edit -- an in-file rename, a "
    "cross-file move whose source file still exists, a deleted file, a TS-only rename -- with and without git, "
    "including the similarity-skipped warning and markers"
)
@pytest.mark.parametrize("git", [True, False], ids=["git", "no_git"])
@pytest.mark.parametrize("edit", list(_EDITS))
def test_incremental_build_detects_the_renames_a_full_build_detects(tmp_path: Path, edit: str, git: bool) -> None:
    口 = Step(step_num=1, name="Index the project", purpose="A project indexed twice, so the second is a no-op")
    if edit == "ts_only_rename":
        _with_ts(tmp_path)
    root = _project(tmp_path, git=git)
    build_index(_dbp(root), root)
    build_index(_dbp(root), root)

    口 = Step(step_num=2, name="Edit and snapshot", purpose="Make the edit, then copy the index before any build")
    _EDITS[edit](root)
    snapshot = tmp_path / "snapshot.db"
    _copy_db(_dbp(root), snapshot)

    口 = Step(step_num=3, name="Incremental build", purpose="Only the edited files are parsed")
    incremental = build_index(_dbp(root), root)
    if edit in _PARSED:
        assert incremental.files_scanned == _PARSED[edit]
    incremental_outcome = _outcome(root)
    incremental_skips = _rename_skips(root, incremental)
    assert_matches_full_recompute(_dbp(root), root)

    口 = Step(step_num=4, name="Full build", purpose="The same index and edit, every file parsed")
    full = _full_build_from(root, snapshot)
    assert full.files_scanned > incremental.files_scanned
    assert_matches_full_recompute(_dbp(root), root)

    口 = Step(
        step_num=5,
        name="Same renames",
        purpose="Renames, statuses, the reported count, the skip warning and the skip markers agree",
    )
    assert incremental_outcome == _outcome(root)
    assert incremental.nodes_renamed == full.nodes_renamed
    assert incremental_skips == _rename_skips(root, full)
    if edit == "cross_file_move" and git:
        # The source file still exists: only a pool formed from the parsed
        # files (not from every file that is gone) sees g leave it.
        assert incremental.nodes_renamed == 1
    if edit == "deletion_only" and not git:
        # Nothing was parsed, yet the vanished file's nodes still form a pool.
        assert incremental_skips[0] == ["similarity skipped for 1 scope(s): no_git=1"]


def _write_doc(path: Path, content: str, links: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "title": path.stem,
                "sections": [
                    {"id": "s1", "heading": "Intro", "content": content, "links": [{"node_id": n} for n in links]}
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _fts_rows(root: Path) -> list[tuple]:
    with db._connect(_dbp(root)) as conn:
        return sorted(tuple(r) for r in conn.execute("SELECT id, level_1, level_2 FROM node_fts"))


@workflow(purpose="After a doc edit, an incremental build leaves the search index a full build leaves")
def test_incremental_build_fts_matches_a_full_build(tmp_path: Path) -> None:
    root = _project(tmp_path)
    docs = root / "docs"
    docs.mkdir()
    _write_doc(docs / "guide.json", "Original words.", [])
    _write_doc(docs / "untouched.json", "Kept words.", [])
    build_index(_dbp(root), root)
    build_index(_dbp(root), root)

    _write_doc(docs / "guide.json", "Freshly edited zebracorn words.", [])
    snapshot = tmp_path / "snapshot.db"
    _copy_db(_dbp(root), snapshot)

    build_index(_dbp(root), root)
    incremental = _fts_rows(root)
    assert any("zebracorn" in (row[2] or "") for row in incremental)

    _full_build_from(root, snapshot)
    assert incremental == _fts_rows(root)


def _broken(root: Path) -> set[str]:
    with db._connect(_dbp(root)) as conn:
        return {r[0] for r in conn.execute("SELECT id FROM nodes WHERE link_status = 'BROKEN_LINK'")}


@workflow(
    purpose="Deleting a linked file flags the linking section BROKEN_LINK as a full build does, and the reported "
    "count stays the whole index's on a following no-op build"
)
def test_broken_links_after_an_incremental_build_match_a_full_build(tmp_path: Path) -> None:
    root = _project(tmp_path)
    docs = root / "docs"
    docs.mkdir()
    _write_doc(docs / "guide.json", "About g.", [])
    build_index(_dbp(root), root)
    with db._connect(_dbp(root)) as conn:
        (g_id,) = conn.execute("SELECT id FROM nodes WHERE title = 'g' AND location = 'pkg/other.py'").fetchone()
    _write_doc(docs / "guide.json", "About g.", [g_id])
    build_index(_dbp(root), root)
    build_index(_dbp(root), root)

    (root / "pkg" / "other.py").unlink()
    snapshot = tmp_path / "snapshot.db"
    _copy_db(_dbp(root), snapshot)

    incremental = build_index(_dbp(root), root)
    flagged = _broken(root)
    assert flagged, "the section linking the deleted function is BROKEN_LINK"
    assert incremental.broken_links_flagged == len(flagged)
    assert_matches_full_recompute(_dbp(root), root)
    noop = build_index(_dbp(root), root)
    assert noop.files_scanned == 0
    assert noop.broken_links_flagged == len(flagged), "the count is the whole index's, not this build's"

    _full_build_from(root, snapshot)
    assert _broken(root) == flagged


def test_parentless_step_count_uses_the_edges_to_index(tmp_path: Path) -> None:
    root = _project(tmp_path)
    build_index(_dbp(root), root)
    statements: list[str] = []
    with db._connect(_dbp(root)) as conn:
        conn.set_trace_callback(statements.append)
        db.count_parentless_step_nodes_by_location_conn(conn)
        conn.set_trace_callback(None)
        query = next(s for s in statements if "composes" in s)
        plan = " ".join(str(tuple(r)) for r in conn.execute(f"EXPLAIN QUERY PLAN {query}"))
    assert "idx_edges_to" in plan


def test_one_file_edit_builds_rename_pools_from_that_file_only(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path)
    for i in range(5):
        (root / "pkg" / f"extra_{i}.py").write_text(f"def h{i}():\n    return {i}\n", encoding="utf-8")
    build_index(_dbp(root), root)
    build_index(_dbp(root), root)

    pools: list[set[str]] = []
    real = db.get_nodes_at_locations_conn

    def _spy(conn, locations, node_types=()):
        pools.append(set(locations))
        return real(conn, locations, node_types)

    monkeypatch.setattr(builder.db, "get_nodes_at_locations_conn", _spy)
    (root / "pkg" / "mod.py").write_text(_MOD.replace("item * 2", "item * 3"), encoding="utf-8")
    build_index(_dbp(root), root)
    assert pools == [{"pkg/mod.py"}]

    pools.clear()
    noop = build_index(_dbp(root), root)
    assert noop.files_scanned == 0
    assert pools == [], "a no-op build forms no rename pool"
    assert_matches_full_recompute(_dbp(root), root)


def test_noop_build_rescans_no_ts_file(tmp_path: Path) -> None:
    from axiom_graph.scanners.js_scanner import HAS_TREE_SITTER

    if not HAS_TREE_SITTER:
        pytest.skip("tree-sitter (the js extra) is not installed")
    root = _project(tmp_path)
    (root / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\njs_paths = ["web/*.ts"]\n', encoding="utf-8"
    )
    web = root / "web"
    web.mkdir()
    (web / "app.ts").write_text("export function add(a: number, b: number): number {\n  return a + b;\n}\n")
    (web / "util.ts").write_text("export function twice(x: number): number {\n  return x * 2;\n}\n")
    build_index(_dbp(root), root)
    build_index(_dbp(root), root)

    noop = builder.build(root)
    assert noop["js_scanned"] == 0
    assert noop["js_skipped_mtime"] == 2
