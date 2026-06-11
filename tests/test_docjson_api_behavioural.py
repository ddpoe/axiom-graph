"""Tier-A behavioural tests for the docjson public API.

One end-to-end test per public doc tool, all driving
``axiom_graph.docjson.api.*`` directly.  These tests close the
fixture-bypass gap that hid the LINKED_STALE bug: every behavioural path
exercised here mirrors what the MCP wrapper would invoke.

Tied to ADR-019 user story US-5 (test-faithfulness restored).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom_graph.docjson.api import (
    axiom_graph_add_link,
    axiom_graph_add_section,
    axiom_graph_delete_doc,
    axiom_graph_delete_link,
    axiom_graph_delete_section,
    axiom_graph_read_doc,
    axiom_graph_update_doc_meta,
    axiom_graph_update_section,
    axiom_graph_write_doc,
    parse_section_id,
    save_and_reindex,
)
from axiom_graph.index import builder, db
from axiom_graph.index.paths import db_path as _db_path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal project root with axiom-graph.toml and docs/."""
    (tmp_path / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "proj"\n',
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    # Initialize the index (empty project — no code) so require_db succeeds.
    builder.build(tmp_path)
    return tmp_path


def _write_arch_doc(project: Path) -> str:
    """Helper: write a fresh `proj::docs.arch` doc and return its node id."""
    doc = {
        "id": "arch",
        "title": "Architecture",
        "sections": [
            {"id": "overview", "heading": "Overview", "content": "Initial."},
            {"id": "api", "heading": "API"},
        ],
    }
    res = axiom_graph_write_doc(str(project), doc)
    assert "Wrote" in res, res
    return "proj::docs.arch"


def test_us5_write_then_read_roundtrip(project: Path) -> None:
    """E2E: write_doc + read_doc roundtrip, sections intact and indexed."""
    doc_id = _write_arch_doc(project)
    md = axiom_graph_read_doc(str(project), doc_id)
    assert "# Architecture" in md
    assert "Overview" in md
    assert "API" in md


def test_us5_read_specific_section(project: Path) -> None:
    """read_doc with section= filters to that slug only."""
    doc_id = _write_arch_doc(project)
    md = axiom_graph_read_doc(str(project), doc_id, section="overview")
    assert "Overview" in md
    assert "API" not in md


def test_us5_update_section_content_persists_to_disk(project: Path) -> None:
    """update_section writes content to JSON file and re-indexes."""
    doc_id = _write_arch_doc(project)
    res = axiom_graph_update_section(
        str(project),
        f"{doc_id}::overview",
        content="Updated body.",
    )
    assert "Updated" in res
    md = axiom_graph_read_doc(str(project), doc_id, section="overview")
    assert "Updated body." in md


def test_us5_add_section_nested_with_depth(project: Path) -> None:
    """add_section under a parent_id nests at depth 1 and indexes."""
    doc_id = _write_arch_doc(project)
    res = axiom_graph_add_section(
        str(project),
        doc_id,
        "tables",
        heading="Tables",
        content="Schema.",
        parent_id="api",
    )
    assert "Added" in res
    md = axiom_graph_read_doc(str(project), doc_id)
    assert "Tables" in md


def test_us5_delete_section_with_children_cascades(project: Path) -> None:
    """delete_section removes the section and all nested children + DB rows."""
    doc_id = _write_arch_doc(project)
    axiom_graph_add_section(str(project), doc_id, "tables", heading="Tables", parent_id="api")
    res = axiom_graph_delete_section(str(project), f"{doc_id}::api")
    assert "Deleted" in res
    md = axiom_graph_read_doc(str(project), doc_id)
    assert "Tables" not in md
    assert "API" not in md
    # Overview survives.
    assert "Overview" in md


def test_us5_add_link_creates_documents_edge(project: Path) -> None:
    """add_link records a documents edge in the DB."""
    doc_id = _write_arch_doc(project)
    res = axiom_graph_add_link(
        str(project),
        f"{doc_id}::overview",
        node_id="proj::some.module",
    )
    assert "Added" in res
    db_p = _db_path(str(project))
    edges = db.all_edges(db_p)
    docs_edges = [
        e
        for e in edges
        if e.edge_type == "documents" and e.from_id == f"{doc_id}::overview" and e.to_id == "proj::some.module"
    ]
    assert len(docs_edges) == 1


def test_us5_delete_link_removes_edge(project: Path) -> None:
    """delete_link removes the documents edge."""
    doc_id = _write_arch_doc(project)
    axiom_graph_add_link(str(project), f"{doc_id}::overview", node_id="proj::some.module")
    res = axiom_graph_delete_link(str(project), f"{doc_id}::overview", node_id="proj::some.module")
    assert "Removed" in res
    db_p = _db_path(str(project))
    edges = db.all_edges(db_p)
    docs_edges = [
        e
        for e in edges
        if e.edge_type == "documents" and e.from_id == f"{doc_id}::overview" and e.to_id == "proj::some.module"
    ]
    assert docs_edges == []


def test_us5_delete_doc_removes_file_and_rows(project: Path) -> None:
    """delete_doc unlinks the JSON file and deletes index rows."""
    doc_id = _write_arch_doc(project)
    res = axiom_graph_delete_doc(str(project), doc_id)
    assert "Deleted" in res
    json_file = project / "docs" / "arch.json"
    assert not json_file.exists()
    db_p = _db_path(str(project))
    assert db.get_node(db_p, doc_id) is None


def test_us5_update_doc_meta_changes_title_no_section_staleness(project: Path) -> None:
    """update_doc_meta with a new title patches top-level only."""
    doc_id = _write_arch_doc(project)
    res = axiom_graph_update_doc_meta(str(project), doc_id, title="Architecture v2")
    assert "Updated" in res
    md = axiom_graph_read_doc(str(project), doc_id)
    assert "# Architecture v2" in md
    # Sections survive intact.
    assert "Overview" in md


def test_us3_save_and_reindex_callable_from_api(project: Path, tmp_path: Path) -> None:
    """save_and_reindex is now exported from docjson.api (US-3)."""
    doc_id = _write_arch_doc(project)
    db_p = _db_path(str(project))
    json_file = project / "docs" / "arch.json"
    data = json.loads(json_file.read_text(encoding="utf-8"))
    data["sections"].append({"id": "extra", "heading": "Extra"})
    save_and_reindex(data, json_file, db_p, project, "proj")
    md = axiom_graph_read_doc(str(project), doc_id)
    assert "Extra" in md


def test_parse_section_id_round_trip() -> None:
    """parse_section_id splits a fully-qualified id into the four parts."""
    parsed = parse_section_id("proj::docs.arch::overview")
    assert parsed == ("proj", "arch", "overview", "proj::docs.arch")


# ---------------------------------------------------------------------------
# Cycle pev-2026-05-02: auto-mark on docjson write tools.
#
# Behavioural tests for the writer-is-verifier semantic added in
# save_and_reindex.  When an existing node's hash changes as a result of a
# docjson write tool call, the writer is implicitly the verifier -- an
# AGENT_VERIFIED history row + node_verification snapshot is recorded for
# that node.  Newly-created and deleted nodes are not candidates.
# ---------------------------------------------------------------------------


def _history_change_types(db_p: Path, node_id: str) -> list[str]:
    """Return ordered change_type values from node_history for *node_id*."""
    import sqlite3

    with sqlite3.connect(db_p) as conn:
        rows = conn.execute(
            "SELECT change_type FROM node_history WHERE node_id = ? ORDER BY id",
            (node_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _verifications(db_p: Path, node_id: str) -> list[tuple[str, str | None, str | None]]:
    """Return (verified_by, code_hash_at, desc_hash_at) rows for *node_id*."""
    import sqlite3

    with sqlite3.connect(db_p) as conn:
        rows = conn.execute(
            "SELECT verified_by, code_hash_at, desc_hash_at FROM node_verification "
            "WHERE node_id = ? ORDER BY verified_at",
            (node_id,),
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def test_update_section_content_emits_agent_verified(project: Path) -> None:
    """US-1: update_section content change emits AGENT_VERIFIED at new hash."""
    doc_id = _write_arch_doc(project)
    section_id = f"{doc_id}::overview"
    db_p = _db_path(str(project))

    # Pre-state: section node exists, has only INITIAL history (from write_doc).
    pre_hist = _history_change_types(db_p, section_id)
    assert pre_hist == ["INITIAL"], pre_hist
    assert _verifications(db_p, section_id) == []

    # Act: update content.
    res = axiom_graph_update_section(str(project), section_id, content="Updated.")
    assert "Updated" in res

    # Assert: history now includes AGENT_VERIFIED.
    # Note: save_and_reindex calls upsert_node with discovery_only=True (the
    # default), which preserves the staleness baseline rather than writing a
    # CONTENT_ONLY row at upsert time.  The CONTENT_UPDATED transition would
    # appear later if a build/compute_staleness ran with the *old* hash on
    # the nodes table; here mark_node_clean is called BEFORE that pass, so
    # it resets the baseline to the new hash and the section appears VERIFIED
    # immediately.  History shows: INITIAL (from write_doc) -> AGENT_VERIFIED.
    post_hist = _history_change_types(db_p, section_id)
    assert "AGENT_VERIFIED" in post_hist, post_hist
    assert post_hist[-1] == "AGENT_VERIFIED", post_hist

    # Verification snapshot recorded under verified_by='agent' at the new hash.
    verifs = _verifications(db_p, section_id)
    assert verifs and verifs[-1][0] == "agent"
    node = db.get_node(db_p, section_id)
    assert verifs[-1][1] == node.code_hash


def test_update_section_heading_only_does_not_stale_section_or_siblings(project: Path) -> None:
    """Heading-only edit: section atomic stays VERIFIED (no hash flips), file composite gets AGENT_VERIFIED.

    Section atomic ``code_hash`` and ``desc_hash`` are both ``content_hash`` per
    the four-field invariant — heading edits don't flip them, so the section
    isn't stale and doesn't need auto-mark-cleaning.  The signal lives on the
    file-level composite node, whose file_hash changes with the bytes; that
    node is the one the writer auto-marks clean.
    """
    doc_id = _write_arch_doc(project)
    section_id = f"{doc_id}::overview"
    sibling_id = f"{doc_id}::api"
    db_p = _db_path(str(project))

    res = axiom_graph_update_section(str(project), section_id, heading="Overview v2")
    assert "Updated" in res

    # Section atomic never went stale, so no AGENT_VERIFIED row.
    sec_hist = _history_change_types(db_p, section_id)
    assert "AGENT_VERIFIED" not in sec_hist, sec_hist
    # Sibling untouched.
    sibling_hist = _history_change_types(db_p, sibling_id)
    assert "AGENT_VERIFIED" not in sibling_hist, sibling_hist
    # File-level composite gets auto-marked clean (its bytes changed).
    doc_hist = _history_change_types(db_p, doc_id)
    assert "AGENT_VERIFIED" in doc_hist, doc_hist


def test_add_link_does_not_emit_agent_verified(project: Path) -> None:
    """US-2/D-1: add_link must not auto-mark; only LINK_ADDED is recorded."""
    doc_id = _write_arch_doc(project)
    section_id = f"{doc_id}::overview"
    db_p = _db_path(str(project))

    res = axiom_graph_add_link(str(project), section_id, node_id="proj::some.module")
    assert "Added" in res

    post_hist = _history_change_types(db_p, section_id)
    assert "AGENT_VERIFIED" not in post_hist, post_hist
    assert "LINK_ADDED" in post_hist, post_hist
    assert _verifications(db_p, section_id) == []


def test_add_section_does_not_emit_agent_verified(project: Path) -> None:
    """US-2/D-5: add_section creates a new INITIAL node; no AGENT_VERIFIED."""
    doc_id = _write_arch_doc(project)
    db_p = _db_path(str(project))

    res = axiom_graph_add_section(
        str(project),
        doc_id,
        "tables",
        heading="Tables",
        content="x",
        parent_id="api",
    )
    assert "Added" in res

    new_section_id = f"{doc_id}::api.tables"
    post_hist = _history_change_types(db_p, new_section_id)
    assert post_hist == ["INITIAL"], post_hist
    assert _verifications(db_p, new_section_id) == []


def test_update_doc_meta_title_change_emits_agent_verified(project: Path) -> None:
    """US-1 doc-meta branch: title change auto-marks the doc-level node."""
    doc_id = _write_arch_doc(project)
    db_p = _db_path(str(project))

    pre_node = db.get_node(db_p, doc_id)
    pre_code = pre_node.code_hash

    res = axiom_graph_update_doc_meta(str(project), doc_id, title="Architecture v2")
    assert "Updated" in res

    post_hist = _history_change_types(db_p, doc_id)
    assert "AGENT_VERIFIED" in post_hist, post_hist

    post_node = db.get_node(db_p, doc_id)
    assert post_node.code_hash != pre_code

    verifs = _verifications(db_p, doc_id)
    assert verifs and verifs[-1][0] == "agent"
    assert verifs[-1][1] == post_node.code_hash


def test_update_section_then_baseline_matches_new_hash(project: Path) -> None:
    """US-2 end-to-end: after auto-mark, the baseline hash on the nodes table
    equals the new (post-write) section hash, so the next staleness pass
    will report CLEAN/VERIFIED rather than CONTENT_UPDATED.

    This is the operational fix for Cycle-2 Auditor churn: re-marking the
    same section across incarnations becomes a no-op because the baseline
    is already at the latest content.
    """
    doc_id = _write_arch_doc(project)
    section_id = f"{doc_id}::overview"
    db_p = _db_path(str(project))

    pre_node = db.get_node(db_p, section_id)
    pre_hash = pre_node.code_hash

    axiom_graph_update_section(str(project), section_id, content="Refined body.")

    post_node = db.get_node(db_p, section_id)
    # The baseline hash on the nodes table is now the post-write hash,
    # courtesy of mark_node_clean's update_node_baseline call.  This is
    # the contract that suppresses CONTENT_UPDATED on the next pass.
    assert post_node.code_hash != pre_hash, "Test setup expectation: content edit should produce a new hash"
    verifs = _verifications(db_p, section_id)
    assert verifs and verifs[-1][1] == post_node.code_hash, (
        "Verification snapshot must record the post-write hash so Pass 2 of staleness reads VERIFIED for the section."
    )


def test_update_section_then_human_mark_clean_preserves_sequence(project: Path) -> None:
    """US-4: AGENT_VERIFIED then MANUAL_VERIFIED both appear in history."""
    from axiom_graph.lifecycle.api import mark_clean_nodes

    doc_id = _write_arch_doc(project)
    section_id = f"{doc_id}::overview"
    db_p = _db_path(str(project))

    axiom_graph_update_section(str(project), section_id, content="Refined body.")
    # Manual stamp on top of auto-mark.
    mark_clean_nodes(db_p, project, [section_id], reason="reviewed by hand", verified_by="human")

    hist = _history_change_types(db_p, section_id)
    # Sequence: INITIAL (write_doc create) -> AGENT_VERIFIED (auto-mark on
    # update_section) -> MANUAL_VERIFIED (explicit human stamp).  The
    # CONTENT_UPDATED transition would appear later from compute_staleness;
    # here we assert the verification stamps both land in order and remain
    # distinguishable in history.
    initial_idx = next((i for i, t in enumerate(hist) if t == "INITIAL"), None)
    agent_idx = next((i for i, t in enumerate(hist) if t == "AGENT_VERIFIED"), None)
    human_idx = next((i for i, t in enumerate(hist) if t == "MANUAL_VERIFIED"), None)
    assert initial_idx is not None, hist
    assert agent_idx is not None, hist
    assert human_idx is not None, hist
    assert initial_idx < agent_idx < human_idx, hist

    # Note: node_verification is upsert-by-node_id, so only the most recent
    # snapshot remains (here: 'human').  History is the audit trail that
    # preserves both stamps in order -- already asserted above.
    verifs = _verifications(db_p, section_id)
    assert verifs and verifs[-1][0] == "human", verifs
