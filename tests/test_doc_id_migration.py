"""Tests for the doc-ID migration planner, gate, and execute driver.

Tier 2 -- @workflow(purpose=...):
    Prose buckets, section-targeted link rewriting, single-pass batch cost.

Tier 3 -- @workflow + Step():
    Preview writes nothing; a projected collision makes execute unreachable;
    a mid-batch failure aborts and restores; section verification survives.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.config import db_path_for
from axiom_graph.index import db
from axiom_graph.lifecycle import api as lifecycle_api

from tests.fixtures import doc_trees


def _db_digest(path) -> str:
    """Return a content digest of the database file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_state(root) -> dict[str, tuple[int, str]]:
    """Return every DocJSON file's size and digest, keyed by relative path."""
    out: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*.json")):
        data = path.read_bytes()
        out[path.relative_to(root).as_posix()] = (len(data), hashlib.sha256(data).hexdigest())
    return out


def _build(root):
    """Build an index for *root* and return the DB path."""
    path = db_path_for(root)
    lifecycle_api.build_index(path, root, discovery_only=True)
    return path


def _verification_ids(db_path) -> set[str]:
    """Return every node ID carrying a verification row."""
    with db._connect(db_path) as conn:
        return {r["node_id"] for r in conn.execute("SELECT node_id FROM node_verification").fetchall()}


# ---------------------------------------------------------------------------
# Tier 3 -- US-1: preview writes nothing
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify preview mode reports every document and section identity while leaving the database and every DocJSON file byte-identical",
)
def test_preview_reports_all_identities_and_writes_nothing(tmp_path):
    """A dry run is a pure read: it enumerates sections too, and changes nothing."""
    口 = Step(
        step_num=1,
        name="Build a multi-root tree",
        purpose="Index a project with two configured docs roots and nested sections",
    )
    root = doc_trees.clean_multi_root(tmp_path / "proj")
    doc_trees.write_doc(
        root,
        "specs/nested.json",
        title="Nested",
        sections=[{"id": "a", "heading": "A", "content": "x", "sections": [{"id": "b", "heading": "B"}]}],
    )
    db_path = _build(root)

    口 = Step(
        step_num=2,
        name="Snapshot the database and the doc tree",
        purpose="Capture byte-level state so any write is detectable",
    )
    db_before = _db_digest(db_path)
    tree_before = _tree_state(root)

    口 = Step(
        step_num=3,
        name="Run the migration in preview mode",
        purpose="Compute the plan without executing",
    )
    plan = lifecycle_api.plan_doc_id_migration(db_path, root)

    口 = Step(
        step_num=4,
        name="Assert the report covers documents and sections",
        purpose="The migration unit is every node identity, not just the envelopes",
    )
    assert plan.document_count == 4
    assert plan.section_count == 6
    assert plan.total_nodes == plan.document_count + plan.section_count
    assert ("proj::docs.adrs.013-envelope", "proj::docs/adrs/013-envelope") in [
        (m.old_id, m.new_id) for m in plan.documents
    ]
    assert ("proj::docs.nested::a.b", "proj::specs/nested::a.b") in [(m.old_id, m.new_id) for m in plan.sections]
    assert plan.revert_supported is False

    口 = Step(
        step_num=5,
        name="Assert nothing was written",
        purpose="The database is byte-identical and no DocJSON file changed",
    )
    assert _db_digest(db_path) == db_before
    assert _tree_state(root) == tree_before


# ---------------------------------------------------------------------------
# Tier 3 -- US-2: a projected collision makes execute unreachable
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a doc-id collision inside one root is named with both source files and makes execute mode refuse without writing",
)
def test_collision_names_both_files_and_blocks_execute(tmp_path):
    """The gate is a hard stop: a duplicate identity cannot be executed past."""
    口 = Step(
        step_num=1,
        name="Build a tree that collides inside one root",
        purpose="``adrs/013-x.json`` and ``adrs.013-x.json`` derive one doc id today",
    )
    root = doc_trees.class2_collision(tmp_path / "proj")
    db_path = _build(root)
    db_before = _db_digest(db_path)

    口 = Step(
        step_num=2,
        name="Preview and read the collision verdict",
        purpose="The colliding pair is named with both file paths",
    )
    plan = lifecycle_api.plan_doc_id_migration(db_path, root)
    assert plan.blocked
    collision = next(c for c in plan.blocking_collisions if c.doc_id == "proj::docs.adrs.013-x")
    assert collision.sources == ["docs/adrs.013-x.json", "docs/adrs/013-x.json"]
    assert collision.kind == "within_root"

    口 = Step(
        step_num=3,
        name="Attempt execute and verify it refuses",
        purpose="No backup, no write, no partial migration while a collision exists",
    )
    result = lifecycle_api.execute_doc_id_migration(db_path, root)
    assert result.executed is False
    assert result.reason == "collision"
    assert result.backup_path is None
    assert _db_digest(db_path) == db_before

    口 = Step(
        step_num=4,
        name="Verify the cross-root class blocks too",
        purpose="Both collision classes reach the same refusal",
    )
    other = doc_trees.class1_collision(tmp_path / "cross")
    other_db = _build(other)
    cross = lifecycle_api.execute_doc_id_migration(other_db, other)
    assert cross.executed is False
    assert cross.reason == "collision"


@workflow(
    purpose="Verify ordinary JSON data files under a docs root neither gain a projected identity nor block the migration, while unreadable and section-less documents stay in the report",
)
def test_data_files_under_a_docs_root_do_not_block_the_migration(tmp_path):
    """The gate is unbypassable, so it must only fire on real documents."""
    root = doc_trees.data_files_beside_docs(tmp_path / "proj")
    db_path = _build(root)
    plan = lifecycle_api.plan_doc_id_migration(db_path, root)

    # The two data files derive one doc id between them and one carries a
    # dotted stem — neither may reach the gate or the advisory.
    assert plan.blocked is False
    assert plan.blocking_collisions == []
    assert plan.dotted_filenames == []
    assert {m.file_path for m in plan.documents} == {"docs/real.json", "docs/section-less.json"}
    assert "docs/data.chart.json" not in plan.unreadable
    assert "docs/data/chart.json" not in plan.unreadable

    # The problems an operator should still hear about are still reported.
    assert plan.unreadable == ["docs/broken.json", "docs/section-less.json"]

    result = lifecycle_api.execute_doc_id_migration(db_path, root)
    assert result.executed is True
    assert result.documents_migrated == 2
    assert result.sections_migrated == 1


# ---------------------------------------------------------------------------
# Tier 2 -- US-3: the not-patched bucket
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify the plan reports doc-id prose references in DocJSON section content and elsewhere in the repository as two distinguishable buckets with file and line",
)
def test_prose_reference_buckets_are_distinguishable(tmp_path):
    """Prose references are enumerated with file and line; links are not counted."""
    root = doc_trees.prose_references(tmp_path / "proj")
    db_path = _build(root)

    plan = lifecycle_api.plan_doc_id_migration(db_path, root)

    in_content = {(r.file_path, r.text) for r in plan.prose_in_docjson_content}
    elsewhere = {(r.file_path, r.text) for r in plan.prose_elsewhere}

    assert ("docs/guide.json", "proj::docs.target::payload") in in_content
    assert ("README.md", "proj::docs.target") in elsewhere
    assert ("skills/usage.md", "proj::docs.guide::howto") in elsewhere
    assert not (in_content & elsewhere)

    # Every reference carries a usable location and a deliberate section /
    # document classification -- a document id is a prefix of its section ids.
    for ref in plan.prose_in_docjson_content + plan.prose_elsewhere:
        assert ref.line >= 1
    section_ref = next(r for r in plan.prose_in_docjson_content if r.text.endswith("::payload"))
    assert section_ref.is_section_reference is True
    assert section_ref.doc_id == "proj::docs.target"
    envelope_ref = next(r for r in plan.prose_elsewhere if r.file_path == "README.md")
    assert envelope_ref.is_section_reference is False
    assert envelope_ref.doc_id == "proj::docs.target"

    # The links[].node_id entry in the same file is rewritten by the
    # migration, so it must not appear in a "not patched" bucket.
    assert sum(1 for r in plan.prose_in_docjson_content if r.file_path == "docs/guide.json") == 1


# ---------------------------------------------------------------------------
# Tier 3 -- US-3: backup taken, mid-batch failure aborts cleanly
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify execute backs the index up before writing and that a failure partway through the batch aborts the whole run instead of leaving a partially-migrated index",
)
def test_batch_failure_aborts_and_restores_from_the_backup(tmp_path, monkeypatch):
    """A mid-batch failure leaves the index exactly as it was, from the backup."""
    口 = Step(
        step_num=1,
        name="Build a tree with several documents",
        purpose="Enough documents that a failure can land partway through the batch",
    )
    root = doc_trees.many_docs(tmp_path / "proj", 5)
    db_path = _build(root)
    ids_before = {r["id"] for r in _all_doc_ids(db_path)}

    口 = Step(
        step_num=2,
        name="Engineer a failure on the third document",
        purpose="Fail inside the batch, after some documents have already moved",
    )
    real = db.record_doc_rename_conn
    calls = {"n": 0}

    def _boom(conn, old_id, new_id, file_path):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("engineered batch failure")
        return real(conn, old_id, new_id, file_path)

    monkeypatch.setattr(db, "record_doc_rename_conn", _boom)

    口 = Step(
        step_num=3,
        name="Execute and observe the abort",
        purpose="The run reports where it stopped and that the backup was restored",
    )
    result = lifecycle_api.execute_doc_id_migration(db_path, root)
    assert result.executed is False
    assert result.reason == "aborted"
    assert result.aborted_at is not None
    assert "engineered batch failure" in (result.error or "")
    assert result.restored_from_backup is True

    口 = Step(
        step_num=4,
        name="Verify the backup exists and the index is unmigrated",
        purpose="The echoed backup is a usable restore and no document moved",
    )
    assert result.backup_path is not None and result.backup_path.exists()
    assert {r["id"] for r in _all_doc_ids(db_path)} == ids_before
    assert not any(doc_id.startswith("proj::docs/") for doc_id in ids_before)


def _all_doc_ids(db_path):
    """Return the ``docs`` table rows."""
    with db._connect(db_path) as conn:
        return conn.execute("SELECT id FROM docs").fetchall()


# ---------------------------------------------------------------------------
# Tier 3 -- US-4: section verification survives the migration
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a section that was verified before a doc-id migration is still verified afterwards under its new section id",
)
def test_section_verification_survives_the_migration(tmp_path):
    """Verification is carried for sections, not only for document envelopes."""
    口 = Step(
        step_num=1,
        name="Build a tree and verify a section",
        purpose="Record verification on a section node, not just its parent document",
    )
    root = doc_trees.nested_sections(tmp_path / "proj")
    db_path = _build(root)
    section_id = "proj::docs.tree::root.child"
    doc_id = "proj::docs.tree"
    outcome = lifecycle_api.mark_clean_nodes(
        db_path,
        root,
        [doc_id, section_id],
        "fixture",
        verified_by="human",
    )
    assert sorted(outcome.marked) == sorted([doc_id, section_id]), outcome
    assert section_id in _verification_ids(db_path)

    口 = Step(
        step_num=2,
        name="Execute the migration",
        purpose="Move every document and section identity",
    )
    result = lifecycle_api.execute_doc_id_migration(db_path, root)
    assert result.executed is True, result
    assert result.documents_migrated == 1
    assert result.sections_migrated == 4

    口 = Step(
        step_num=3,
        name="Assert the section is still verified under its new id",
        purpose="The section's verification row moved with it and the old id is gone",
    )
    verified = _verification_ids(db_path)
    assert "proj::docs/tree::root.child" in verified
    assert "proj::docs/tree" in verified
    assert section_id not in verified


# ---------------------------------------------------------------------------
# Tier 2 -- US-4: section-targeted links, code links untouched
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a doc-id migration rewrites links that target another document's section, and leaves code-targeted links alone",
)
def test_section_targeted_links_are_rewritten_and_code_links_are_not(tmp_path):
    """Most doc links point at sections; an envelope-only rewrite misses them."""
    root = doc_trees.linked_sections(tmp_path / "proj")
    db_path = _build(root)

    result = lifecycle_api.execute_doc_id_migration(db_path, root)
    assert result.executed is True, result
    assert result.files_patched == 1

    source = json.loads((root / "docs" / "source.json").read_text(encoding="utf-8"))
    refs = [link["node_id"] for link in source["sections"][0]["links"]]
    assert "proj::docs/target::payload" in refs
    assert "proj::pkg.mod::func" in refs
    envelope = source["sections"][1]["links"][0]["node_id"]
    assert envelope == "proj::docs/target"


# ---------------------------------------------------------------------------
# Tier 2 -- US-4: the batch walks the tree once
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a whole-tree doc-id migration reads each DocJSON file once for the entire rename map rather than once per rename",
)
def test_batch_link_rewrite_walks_the_tree_once(tmp_path):
    """Rewrite cost scales with the tree, not with documents times files."""
    doc_count = 12
    root = doc_trees.many_docs(tmp_path / "proj", doc_count)
    db_path = _build(root)

    result = lifecycle_api.execute_doc_id_migration(db_path, root)

    assert result.executed is True, result
    assert result.documents_migrated == doc_count
    assert result.doc_files_read == doc_count
    assert result.doc_files_read < doc_count * doc_count


# ---------------------------------------------------------------------------
# Tier 1 -- rekey counts and rename ordering
# ---------------------------------------------------------------------------


def test_rekey_counts_sections_independently_of_the_envelope_row(tmp_path):
    """A missing envelope row must not cost the section count one section."""
    root = doc_trees.nested_sections(tmp_path / "proj")
    db_path = _build(root)

    with db._connect(db_path) as conn:
        sections = conn.execute("SELECT COUNT(*) AS n FROM nodes WHERE id LIKE 'proj::docs.tree::%'").fetchone()["n"]
        assert sections == 4
        conn.execute("DELETE FROM nodes WHERE id = 'proj::docs.tree'")

    with db._connect(db_path) as conn:
        counts = db.rekey_doc_identity(conn, "proj::docs.tree", "proj::docs/tree", "docs/tree.json")

    assert counts.envelope is False
    assert counts.sections == sections


# ---------------------------------------------------------------------------
# Tier 1 -- rename ordering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mapping,expect_order",
    [
        ({"a": "x", "b": "y"}, True),
        ({"a": "b", "b": "y"}, True),
        ({"a": "b", "b": "a"}, False),
    ],
)
def test_rename_order_moves_blockers_first_and_detects_cycles(mapping, expect_order):
    """A rename onto a live identity is sequenced behind it; a swap has no order."""
    order = lifecycle_api._order_doc_renames(mapping)
    if not expect_order:
        assert order is None
        return
    assert order is not None and sorted(order) == sorted(mapping)
    seen: set[str] = set()
    for old in order:
        assert mapping[old] not in (set(mapping) - seen - {old})
        seen.add(old)
