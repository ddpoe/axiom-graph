"""Tests for hash-similarity rename detection.

Tier 1 — plain pytest:
    Unit tests for db.record_code_rename() in isolation.

Tier 2 — @workflow(purpose=...):
    Builder-level rename detection scenarios (subsystem behaviour).

Tier 3 — @workflow(purpose=...) + Step():
    Full PRD scenario matrix narrated as a stakeholder story.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.index import builder, db
from axiom_graph.models import AxiomNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upsert_code_node(db_path: Path, node_id: str, code_hash: str) -> None:
    """Insert a minimal atomic_process node for unit testing."""
    parts = node_id.split("::")
    name = parts[-1]
    node = AxiomNode(
        id=node_id,
        node_type="atomic_process",
        title=name,
        location="mod_a.py",
        source="ast",
        code_hash=code_hash,
        level_0=name,
        level_1=name,
    )
    db.upsert_node(db_path, node, discovery_only=False)


def _count_renames(db_path: Path) -> int:
    with db._connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM node_renames").fetchone()[0]


def _rename_rows(db_path: Path) -> list[dict]:
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM node_renames").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tier 1 — db.record_code_rename() unit tests
# ---------------------------------------------------------------------------


def test_record_code_rename_inserts_rename_row(mini_project, db_path):
    """record_code_rename creates a row in node_renames with correct old_id, new_id, file_path."""
    _upsert_code_node(db_path, "proj::mod_a::func_x", "hash_abc")
    _upsert_code_node(db_path, "proj::mod_b::func_x", "hash_abc")

    db.record_code_rename(db_path, "proj::mod_a::func_x", "proj::mod_b::func_x", "mod_a.py")

    with db._connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM node_renames WHERE old_id = ?",
            ("proj::mod_a::func_x",),
        ).fetchone()
    assert row is not None
    assert row["new_id"] == "proj::mod_b::func_x"
    assert row["file_path"] == "mod_a.py"


def test_record_code_rename_migrates_history(mini_project, db_path):
    """record_code_rename moves all node_history rows from old_id to new_id."""
    _upsert_code_node(db_path, "proj::mod_a::func_x", "hash_abc")
    _upsert_code_node(db_path, "proj::mod_b::func_x", "hash_abc")

    # Add an extra history row on top of the INITIAL one from upsert
    db.insert_history_row(db_path, "proj::mod_a::func_x", "CONTENT_ONLY")

    db.record_code_rename(db_path, "proj::mod_a::func_x", "proj::mod_b::func_x", "mod_a.py")

    # All history now lives under the new ID
    history_new = db.get_history(db_path, "proj::mod_b::func_x")
    assert len(history_new) > 0

    # Nothing left under the old ID
    history_old = db.get_history(db_path, "proj::mod_a::func_x")
    assert history_old == []


def test_record_code_rename_migrates_verification(mini_project, db_path):
    """record_code_rename moves verification from old_id to new_id and cleans up the old row."""
    _upsert_code_node(db_path, "proj::mod_a::func_x", "hash_abc")
    _upsert_code_node(db_path, "proj::mod_b::func_x", "hash_abc")

    db.upsert_verification(db_path, "proj::mod_a::func_x", "human", "hash_abc")
    assert db.get_verification(db_path, "proj::mod_a::func_x") is not None

    db.record_code_rename(db_path, "proj::mod_a::func_x", "proj::mod_b::func_x", "mod_a.py")

    assert db.get_verification(db_path, "proj::mod_b::func_x") is not None
    assert db.get_verification(db_path, "proj::mod_a::func_x") is None


def test_record_code_rename_no_crash_without_history(mini_project, db_path):
    """record_code_rename does not raise when old_id has no history rows."""
    # Insert nodes but no explicit history (upsert_node writes INITIAL automatically)
    _upsert_code_node(db_path, "proj::mod_a::func_x", "hash_abc")
    _upsert_code_node(db_path, "proj::mod_b::func_x", "hash_abc")

    # Remove the history rows upsert_node wrote so old_id starts with zero rows
    with db._connect(db_path) as conn:
        conn.execute("DELETE FROM node_history WHERE node_id = ?", ("proj::mod_a::func_x",))

    # Should not raise
    db.record_code_rename(db_path, "proj::mod_a::func_x", "proj::mod_b::func_x", "mod_a.py")

    with db._connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM node_renames WHERE old_id = ?",
            ("proj::mod_a::func_x",),
        ).fetchone()
    assert row is not None


def test_record_code_rename_duplicate_is_ignored(mini_project, db_path):
    """Calling record_code_rename twice with the same args is idempotent (INSERT OR IGNORE)."""
    _upsert_code_node(db_path, "proj::mod_a::func_x", "hash_abc")
    _upsert_code_node(db_path, "proj::mod_b::func_x", "hash_abc")

    db.record_code_rename(db_path, "proj::mod_a::func_x", "proj::mod_b::func_x", "mod_a.py")
    db.record_code_rename(db_path, "proj::mod_a::func_x", "proj::mod_b::func_x", "mod_a.py")

    with db._connect(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM node_renames WHERE old_id = ? AND new_id = ?",
            ("proj::mod_a::func_x", "proj::mod_b::func_x"),
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Tier 2 — builder rename detection (@workflow(purpose=...))
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify that builder detects a function move (same code_hash, new module) "
    "and increments nodes_renamed with a node_renames row and migrated history",
)
def test_builder_detects_simple_move(mini_project, db_path):
    """Delete mod_a, recreate identical function body in mod_b → nodes_renamed == 1, history migrated.

    The module-level code_hash is made intentionally different between mod_a and mod_b
    (each has a unique extra function) so that only the atomic function node matches,
    keeping nodes_renamed exactly 1.
    """
    func_body = "def shared_func():\n    return 42\n"

    # Build 1: mod_a has shared_func plus a unique extra so its module hash won't match mod_b
    (mini_project / "mod_a.py").write_text(func_body + "\ndef extra_a():\n    return 1\n")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Move: delete mod_a, create mod_b with same shared_func but different extra
    (mini_project / "mod_a.py").unlink()
    (mini_project / "mod_b.py").write_text(func_body + "\ndef extra_b():\n    return 2\n")

    result = builder.build(mini_project, project_id="proj", discovery_only=True)

    assert result["nodes_renamed"] == 1

    with db._connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM node_renames WHERE old_id = ?",
            ("proj::mod_a::shared_func",),
        ).fetchone()
    assert row is not None
    assert row["new_id"] == "proj::mod_b::shared_func"

    history = db.get_history(db_path, "proj::mod_b::shared_func")
    assert len(history) > 0, "History from mod_a::shared_func should be migrated to mod_b::shared_func"


@workflow(
    purpose="Verify that document nodes (now composite_process/subtype=docjson) participate "
    "in hash-similarity rename detection like any other process node",
)
def test_builder_rename_detects_moved_doc(mini_project, db_path):
    """Moving a JSON doc file to a new path should trigger rename detection.

    Doc files are now ``composite_process`` / ``subtype=docjson`` and
    participate in hash-similarity rename detection (ADR-009 Layer 7).
    """
    docs_dir = mini_project / "docs"
    docs_dir.mkdir()
    doc_content = {
        "title": "My Doc",
        "sections": [{"id": "s1", "heading": "Intro", "content": "Some text here."}],
    }
    (docs_dir / "mydoc.json").write_text(json.dumps(doc_content))

    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Replace the doc file with an identical one at a new path
    (docs_dir / "mydoc.json").unlink()
    (docs_dir / "newdoc.json").write_text(json.dumps(doc_content))

    result = builder.build(mini_project, project_id="proj", discovery_only=True)

    assert result["nodes_renamed"] > 0


@workflow(
    purpose="Verify that no rename fires when the moved function has a different body "
    "(code_hash changed) — it should remain as NOT_FOUND, not auto-renamed",
)
def test_builder_no_rename_when_code_hash_differs(mini_project, db_path):
    """Different function body in mod_b → code_hash mismatch → nodes_renamed == 0."""
    (mini_project / "mod_a.py").write_text("def func_x():\n    return 42\n")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    (mini_project / "mod_a.py").unlink()
    # Same name, different body → different code_hash
    (mini_project / "mod_b.py").write_text("def func_x():\n    return 99\n")

    result = builder.build(mini_project, project_id="proj", discovery_only=True)

    assert result["nodes_renamed"] == 0


@workflow(
    purpose="Verify that no rename fires when the original node is still present in the scan — "
    "a node in scanned_ids is never treated as a rename source",
)
def test_builder_no_rename_when_node_still_exists(mini_project, db_path):
    """mod_a::func_x still in scan → excluded from existing_code_nodes → nodes_renamed == 0."""
    func_src = "def func_x():\n    return 42\n"
    (mini_project / "mod_a.py").write_text(func_src)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Add mod_b with identical func_x body, but mod_a still exists
    (mini_project / "mod_b.py").write_text(func_src + "\ndef extra():\n    return 1\n")

    result = builder.build(mini_project, project_id="proj", discovery_only=True)

    assert result["nodes_renamed"] == 0


# ---------------------------------------------------------------------------
# Tier 3 — E2E scenario matrix (@workflow(purpose=...) + Step())
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "End-to-end PRD scenario matrix: init → move (two renames, history migrates) "
        "→ refactor (no rename, same ID new hash) → delete (purged, not renamed). "
        "Covers all four cases the rename-detection spec must correctly distinguish."
    ),
)
def test_rename_detection_scenario_matrix(tmp_path):
    """Full scenario matrix narrating every rename/non-rename/purge outcome in sequence."""

    口 = Step(
        step_num=1,
        name="Init",
        purpose="Establish baseline with two functions in mod_x",
        outputs="sc::mod_x::func_one and sc::mod_x::func_two indexed with INITIAL history",
    )
    func_one = "def func_one():\n    return 1\n"
    func_two = "def func_two():\n    return 2\n"
    (tmp_path / "mod_x.py").write_text(func_one + "\n" + func_two)

    r_init = builder.build(tmp_path, project_id="sc", discovery_only=False)
    db_path = tmp_path / ".axiom_graph" / "graph.db"

    assert db.get_node(db_path, "sc::mod_x::func_one") is not None
    assert db.get_node(db_path, "sc::mod_x::func_two") is not None
    assert not r_init["warnings"]

    口 = Step(
        step_num=2,
        name="Move (two renames)",
        purpose=(
            "Delete mod_x, recreate func_one in mod_y and func_two in mod_z. "
            "Both modules have unique extra functions so their module-level hashes differ "
            "from mod_x, ensuring only the two atomic function nodes are renamed."
        ),
        outputs="nodes_renamed==2, node_renames rows for both functions, history migrated",
    )
    (tmp_path / "mod_x.py").unlink()
    (tmp_path / "mod_y.py").write_text(func_one + "\ndef extra_y():\n    pass\n")
    (tmp_path / "mod_z.py").write_text(func_two + "\ndef extra_z():\n    pass\n")

    r_move = builder.build(tmp_path, project_id="sc", discovery_only=True)

    assert r_move["nodes_renamed"] == 2

    renames = {r["old_id"]: r["new_id"] for r in _rename_rows(db_path)}
    assert renames.get("sc::mod_x::func_one") == "sc::mod_y::func_one"
    assert renames.get("sc::mod_x::func_two") == "sc::mod_z::func_two"

    assert db.get_history(db_path, "sc::mod_y::func_one"), "History must be migrated to mod_y::func_one"
    assert db.get_history(db_path, "sc::mod_z::func_two"), "History must be migrated to mod_z::func_two"

    口 = Step(
        step_num=3,
        name="Refactor (no rename)",
        purpose=(
            "Modify func_one body in mod_y. The function stays in the same module — "
            "it is in scanned_ids — so no rename fires even though the code_hash changes."
        ),
        outputs="nodes_renamed==0, sc::mod_y::func_one has updated code_hash",
    )
    (tmp_path / "mod_y.py").write_text("def func_one():\n    return 999\n\ndef extra_y():\n    pass\n")
    r_refactor = builder.build(tmp_path, project_id="sc", discovery_only=False)

    assert r_refactor["nodes_renamed"] == 0

    口 = Step(
        step_num=4,
        name="Delete (purge, not rename)",
        purpose=(
            "Delete mod_y entirely. func_one (now with a different hash after refactor) "
            "has no matching code_hash anywhere in the new scan, so it is purged as "
            "NOT_FOUND — no new rename row is added."
        ),
        outputs="nodes_purged>0, nodes_renamed==0, no new node_renames row for func_one",
    )
    rename_count_before = _count_renames(db_path)
    (tmp_path / "mod_y.py").unlink()

    r_delete = builder.build(tmp_path, project_id="sc", discovery_only=True)

    assert r_delete["nodes_purged"] > 0
    assert r_delete["nodes_renamed"] == 0
    assert _count_renames(db_path) == rename_count_before, "Delete must not produce new node_renames rows"
    assert db.get_node(db_path, "sc::mod_y::func_one") is None, "Deleted node must be purged from the DB"
