"""The v5 pair backfill writes a pair only where the clock rule already settles the link.

A stored ``VERIFIED`` that no check refreshed since its target changed -- a
frozen-doc section, or a row an older check left behind -- must stay on the
clock rule after the upgrade, so the next unfrozen check reads what
``check --full`` reads on the same index.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from axiom_annotations import workflow

from axiom_graph.db import _core, migrations
from axiom_graph.index import db
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes
from tests.fixtures.full_recompute import assert_matches_full_recompute, full_recompute_rows, stored_rows

_TOML = '[axiom_graph]\nproject_id = "proj"\n'
_FROZEN_TOML = _TOML + '\n[axiom_graph.staleness]\nfrozen_tags = ["frozen"]\n'
_SECTION = "proj::docs/spec::f"
_TARGET = "proj::mod::f"
_MODULE = "proj::mod"


def _downgrade_to_v4(conn: sqlite3.Connection) -> None:
    """Strip the v5 schema from an index, leaving the v4 shape."""
    for name in _core._SCAN_INDEX_NAMES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE node_verification_targets")
    conn.execute("ALTER TABLE nodes DROP COLUMN live_code_hash")
    conn.execute("ALTER TABLE nodes DROP COLUMN live_desc_hash")
    conn.execute("PRAGMA user_version = 4")


def _make_project(root: Path, *, frozen: bool) -> Path:
    """Write a module and a doc section linking its function; return the index path."""
    (root / "axiom-graph.toml").write_text(_FROZEN_TOML if frozen else _TOML, encoding="utf-8")
    (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "docs").mkdir()
    doc = {
        "title": "Spec",
        "tags": ["frozen"],
        "sections": [{"id": "f", "heading": "F", "content": "F.", "links": [{"node_id": _TARGET}]}],
    }
    (root / "docs" / "spec.json").write_text(json.dumps(doc), encoding="utf-8")
    return root / ".axiom_graph" / "graph.db"


def _verify_then_change_target(db_path: Path, root: Path) -> None:
    """Verify the section, then change and re-verify its target, so the target's change is newer."""
    build_index(db_path, root)
    mark_clean_nodes(db_path, root, [_SECTION], reason="reviewed", verified_by="human")
    (root / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    build_index(db_path, root)
    mark_clean_nodes(db_path, root, [_TARGET], reason="reviewed", verified_by="human")


def _pair_targets(db_path: Path, node_id: str) -> set[str]:
    """Return the targets *node_id* has a stored pair for."""
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT target_id FROM node_verification_targets WHERE node_id = ?", (node_id,))
        return {r[0] for r in rows}


def _link_status(db_path: Path, node_id: str) -> str:
    """Return the stored link status of *node_id*."""
    with db._connect(db_path) as conn:
        return conn.execute("SELECT link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()[0]


@workflow(
    purpose=(
        "A frozen section whose target changed after its verification gets no backfilled pair, so once its doc "
        "is unfrozen the next check flags it LINKED_STALE, as the clock rule and check --full do"
    )
)
def test_frozen_section_is_not_paired_past_a_newer_change(tmp_path: Path) -> None:
    db_path = _make_project(tmp_path, frozen=True)
    _verify_then_change_target(db_path, tmp_path)
    assert _link_status(db_path, _SECTION) == "VERIFIED", "a frozen section receives no new LINKED_STALE"
    with db._connect(db_path) as conn:
        _downgrade_to_v4(conn)

    assert migrations.run_migrations(db_path) == [5]
    assert _pair_targets(db_path, _SECTION) == set()

    (tmp_path / "axiom-graph.toml").write_text(_TOML, encoding="utf-8")
    compute_check_summary(db_path, tmp_path)
    assert _link_status(db_path, _SECTION) == "LINKED_STALE"
    assert_matches_full_recompute(db_path, tmp_path)


@workflow(
    purpose=(
        "A stored VERIFIED that no check refreshed after its target's newer change gets no pair; a verification "
        "newer than every change is paired; the next check matches check --full"
    )
)
def test_change_newer_than_verification_gets_no_pair(tmp_path: Path) -> None:
    db_path = _make_project(tmp_path, frozen=False)
    _verify_then_change_target(db_path, tmp_path)
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT link_status FROM nodes WHERE id = ?", (_SECTION,)).fetchone()[0] == "LINKED_STALE"
        # An older check left the row VERIFIED although the target changed after the verification.
        conn.execute("UPDATE nodes SET link_status = 'VERIFIED' WHERE id = ?", (_SECTION,))
        _downgrade_to_v4(conn)

    assert migrations.run_migrations(db_path) == [5]
    assert _pair_targets(db_path, _SECTION) == set()
    compute_check_summary(db_path, tmp_path, full=True)
    assert _link_status(db_path, _SECTION) == "LINKED_STALE"
    assert_matches_full_recompute(db_path, tmp_path)


def test_verification_newer_than_every_change_is_paired(tmp_path: Path) -> None:
    """Re-verifying the section after its target's change leaves it settled, so the backfill pairs it."""
    db_path = _make_project(tmp_path, frozen=False)
    _verify_then_change_target(db_path, tmp_path)
    mark_clean_nodes(db_path, tmp_path, [_SECTION], reason="reviewed", verified_by="human")
    with db._connect(db_path) as conn:
        conn.execute("DELETE FROM node_verification_targets")
        _downgrade_to_v4(conn)

    assert migrations.run_migrations(db_path) == [5]
    assert _pair_targets(db_path, _SECTION) == {_TARGET}
    compute_check_summary(db_path, tmp_path)
    assert _link_status(db_path, _SECTION) == "VERIFIED"
    assert_matches_full_recompute(db_path, tmp_path)


def test_module_verified_member_by_member_reads_its_file_after_the_upgrade(tmp_path: Path) -> None:
    """A module whose function alone was re-verified after an edit stores the file's live hash after one check.

    The module's baseline still fingerprints the file as first scanned.  The
    upgrade empties every live hash, so the first check re-hashes each file
    once and stores what ``check --full`` stores; the check after it hashes
    nothing.
    """
    db_path = _make_project(tmp_path, frozen=False)
    _verify_then_change_target(db_path, tmp_path)
    with db._connect(db_path) as conn:
        _downgrade_to_v4(conn)
        baseline = conn.execute("SELECT code_hash FROM nodes WHERE id = ?", (_MODULE,)).fetchone()[0]
        tracked = {r[0] for r in conn.execute("SELECT location FROM file_state WHERE hashed_fp IS NOT NULL")}
    assert "mod.py" in tracked

    assert migrations.run_migrations(db_path) == [5]
    first = compute_check_summary(db_path, tmp_path)
    assert set(first.refresh.files_hashed) == tracked

    live = stored_rows(db_path, [_MODULE])[_MODULE]
    assert live == full_recompute_rows(db_path, tmp_path, [_MODULE])[_MODULE]
    assert live[2] != baseline, "the module's live hash is the edited file's, not the first scan's"
    assert_matches_full_recompute(db_path, tmp_path)

    second = compute_check_summary(db_path, tmp_path)
    assert set(second.refresh.files_hashed) == set()
