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

from axiom_annotations import workflow

from axiom_graph.docjson.api import (
    axiom_graph_add_link,
    axiom_graph_add_section,
    axiom_graph_delete_doc,
    axiom_graph_delete_link,
    axiom_graph_delete_section,
    axiom_graph_patch_section,
    axiom_graph_read_doc,
    axiom_graph_update_doc_meta,
    axiom_graph_update_section,
    axiom_graph_write_doc,
    parse_section_id,
    save_and_reindex,
)
from axiom_graph.docjson import parse as json_doc_scanner
from axiom_graph.index import builder, db
from axiom_graph.index.doc_ids import derive_doc_id
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, compute_check_summary

from tests.fixtures import doc_trees


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
    """Helper: write a fresh `proj::docs/arch` doc and return its node id."""
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
    return "proj::docs/arch"


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
    json_file = project / "docs" / "arch.docjson"
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
    json_file = project / "docs" / "arch.docjson"
    data = json.loads(json_file.read_text(encoding="utf-8"))
    data["sections"].append({"id": "extra", "heading": "Extra"})
    save_and_reindex(data, json_file, db_p, project, "proj")
    md = axiom_graph_read_doc(str(project), doc_id)
    assert "Extra" in md


def test_parse_section_id_round_trip() -> None:
    """parse_section_id splits a fully-qualified id into the four parts."""
    doc_id = derive_doc_id("proj", "docs", "arch.json")
    parsed = parse_section_id(f"{doc_id}::overview", ["docs"])
    assert parsed == ("proj", "arch", "overview", doc_id)


@workflow(
    purpose="A document ID holds exactly one '::' and its section IDs exactly two however deep the document sits, and a nested dot-path still resolves",
)
def test_deep_document_id_grammar_and_nested_section_resolution(tmp_path: Path) -> None:
    """Deep paths keep the ``::`` arity and nested dot-paths still resolve."""
    doc_trees.write_toml(tmp_path, ["docs"])
    doc_trees.write_doc(
        tmp_path,
        "docs/a/b/c/deep.json",
        title="Deep",
        sections=[
            {
                "id": "parent",
                "heading": "Parent",
                "content": "Top.",
                "sections": [{"id": "child", "heading": "Child", "content": "Nested."}],
            }
        ],
    )
    builder.build(tmp_path)

    doc_id = derive_doc_id("proj", "docs", "a/b/c/deep.json")
    assert doc_id.count("::") == 1
    section_id = f"{doc_id}::parent.child"
    assert section_id.count("::") == 2

    parsed = parse_section_id(section_id, ["docs"])
    assert not isinstance(parsed, str), parsed
    assert parsed[3] == doc_id
    assert parsed[2] == "parent.child"

    res = axiom_graph_update_section(str(tmp_path), section_id, content="Rewritten.")
    assert "Updated" in res, res
    assert "Rewritten." in axiom_graph_read_doc(str(tmp_path), doc_id, section="child")


@workflow(purpose="A code node's id is refused as a section id rather than being misread as a document")
def test_parse_section_id_rejects_a_code_node_id() -> None:
    """A code-node ID has the same ``::`` arity but names no document."""
    parsed = parse_section_id("proj::axiom_graph.docjson.api::parse_section_id", ["docs", ".pev"])
    assert isinstance(parsed, str)
    assert parsed.startswith("ERROR")


@workflow(
    purpose="Update, patch, add, and delete resolve a section id whose document lives under a non-primary docs root",
)
def test_section_operations_resolve_under_a_non_primary_docs_root(tmp_path: Path) -> None:
    """Every section-mutating entry point works outside the primary root."""
    doc_trees.write_toml(tmp_path, ["docs", ".pev"])
    (tmp_path / "docs").mkdir(exist_ok=True)
    builder.build(tmp_path)

    doc = {
        "title": "Policy",
        "sections": [{"id": "overview", "heading": "Overview", "content": "Initial."}],
    }
    assert "Wrote" in axiom_graph_write_doc(str(tmp_path), doc, docs_root=".pev")
    doc_id = derive_doc_id("proj", ".pev", "policy.json")
    assert db.get_node(_db_path(str(tmp_path)), doc_id) is not None

    assert "Updated" in axiom_graph_update_section(str(tmp_path), f"{doc_id}::overview", content="Body.")
    assert "Patched" in axiom_graph_patch_section(str(tmp_path), f"{doc_id}::overview", "More.", anchor="$")
    assert "Added" in axiom_graph_add_section(str(tmp_path), doc_id, "extra", heading="Extra")
    assert "Deleted" in axiom_graph_delete_section(str(tmp_path), f"{doc_id}::extra")

    md = axiom_graph_read_doc(str(tmp_path), doc_id)
    assert "Body." in md
    assert "More." in md
    assert "Extra" not in md


@workflow(purpose="write_doc reports the doc id it wrote, and that id is the one the index holds")
@pytest.mark.parametrize(
    ("docs_root", "slug", "expected"),
    [
        (None, "adrs/016-nested", "proj::docs/adrs/016-nested"),
        (".pev", "cycles/c-1", "proj::.pev/cycles/c-1"),
    ],
)
def test_write_doc_reports_the_doc_id_it_wrote(tmp_path: Path, docs_root: str | None, slug: str, expected: str) -> None:
    """A caller can take the canonical doc id from the result instead of rebuilding it."""
    doc_trees.write_toml(tmp_path, ["docs", ".pev"])
    (tmp_path / "docs").mkdir(exist_ok=True)
    builder.build(tmp_path)

    doc = {
        "id": slug,
        "title": "Reported",
        "sections": [{"id": "overview", "heading": "Overview", "content": "Body."}],
    }
    res = axiom_graph_write_doc(str(tmp_path), doc, docs_root=docs_root)

    reported = [line.split(":", 1)[1].strip() for line in res.splitlines() if line.strip().startswith("doc id")]
    assert reported == [expected], res
    assert db.get_node(_db_path(str(tmp_path)), expected) is not None
    assert "Updated" in axiom_graph_update_section(str(tmp_path), f"{reported[0]}::overview", content="Edited.")


# ---------------------------------------------------------------------------
# Auto-mark on docjson write tools.
#
# Behavioural tests for the writer-is-verifier semantic in save_and_reindex.
# When a docjson write tool creates a node, or changes a node's content, the
# writer is implicitly the verifier -- an AGENT_VERIFIED history row +
# node_verification snapshot is recorded for that node.  Untouched nodes and
# link-only changes are not candidates.
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

    # Pre-state: write_doc created the section and, as its writer, verified it.
    pre_hist = _history_change_types(db_p, section_id)
    assert pre_hist == ["INITIAL", "AGENT_VERIFIED"], pre_hist
    pre_node = db.get_node(db_p, section_id)
    assert _verifications(db_p, section_id)[-1][1] == pre_node.code_hash

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
    assert post_hist.count("AGENT_VERIFIED") == 2, post_hist
    assert post_hist[-1] == "AGENT_VERIFIED", post_hist

    # Verification snapshot recorded under verified_by='agent' at the new hash.
    verifs = _verifications(db_p, section_id)
    assert verifs and verifs[-1][0] == "agent"
    node = db.get_node(db_p, section_id)
    assert verifs[-1][1] == node.code_hash
    assert node.code_hash != pre_node.code_hash


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

    sec_before = _history_change_types(db_p, section_id)
    sibling_before = _history_change_types(db_p, sibling_id)
    doc_before = _history_change_types(db_p, doc_id)

    res = axiom_graph_update_section(str(project), section_id, heading="Overview v2")
    assert "Updated" in res

    # Section atomic never went stale, so no new AGENT_VERIFIED row.
    assert _history_change_types(db_p, section_id) == sec_before
    # Sibling untouched.
    assert _history_change_types(db_p, sibling_id) == sibling_before
    # File-level composite gets auto-marked clean (its bytes changed).
    doc_hist = _history_change_types(db_p, doc_id)
    assert doc_hist.count("AGENT_VERIFIED") == doc_before.count("AGENT_VERIFIED") + 1, doc_hist


def test_add_link_does_not_emit_agent_verified(project: Path) -> None:
    """US-2/D-1: add_link must not auto-mark: no AGENT_VERIFIED, no new verification.  The link names a node the
    index lacks, so the write's closing refresh records the section's BROKEN_LINK now, as ``check --full`` would."""
    from tests.fixtures.full_recompute import assert_matches_full_recompute

    doc_id = _write_arch_doc(project)
    section_id = f"{doc_id}::overview"
    db_p = _db_path(str(project))

    pre_hist = _history_change_types(db_p, section_id)
    pre_verifs = _verifications(db_p, section_id)

    res = axiom_graph_add_link(str(project), section_id, node_id="proj::some.module")
    assert "Added" in res

    post_hist = _history_change_types(db_p, section_id)
    assert post_hist == [*pre_hist, "BECAME_BROKEN_LINK", "LINK_ADDED"], post_hist
    assert "AGENT_VERIFIED" not in post_hist[len(pre_hist) :]
    assert _verifications(db_p, section_id) == pre_verifs
    assert_matches_full_recompute(db_p, project, [section_id])


def test_add_section_emits_agent_verified_for_the_new_section(project: Path) -> None:
    """add_section creates a new node and, as its writer, verifies it at its hash."""
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
    assert post_hist == ["INITIAL", "AGENT_VERIFIED"], post_hist
    verifs = _verifications(db_p, new_section_id)
    assert verifs and verifs[-1][0] == "agent"
    assert verifs[-1][1] == db.get_node(db_p, new_section_id).code_hash


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


def test_write_doc_reads_a_bare_string_link_as_a_link_object(project: Path) -> None:
    """A ``links`` entry given as a node-id string is written as ``{"node_id": ...}`` and registered."""
    doc = {
        "id": "linked",
        "title": "Linked",
        "sections": [{"id": "overview", "heading": "Overview", "content": "Body.", "links": ["proj::docs/other"]}],
    }

    res = axiom_graph_write_doc(str(project), doc)

    assert "Wrote" in res, res
    saved = json.loads((project / "docs" / "linked.docjson").read_text(encoding="utf-8"))
    assert saved["sections"][0]["links"] == [{"node_id": "proj::docs/other"}]
    edges = db.all_edges(_db_path(str(project)))
    assert any(e.from_id == "proj::docs/linked::overview" and e.to_id == "proj::docs/other" for e in edges)


_X = "proj::docs/other"


@pytest.mark.parametrize(
    ("links", "named"),
    [
        pytest.param([42], "42", id="number"),
        pytest.param([{"node_id": 7}], '{"node_id": 7}', id="non-string-id"),
        pytest.param(_X, "links must be a list", id="not-a-list"),
        pytest.param({}, "links must be a list", id="empty-dict"),
        pytest.param([{"target": _X, "type": "documents"}], '"target"', id="target-key"),
        pytest.param([{}], "{}", id="empty-entry"),
        pytest.param([{"id": _X}], '{"id"', id="id-key"),
        pytest.param([{"node_id": ""}], '{"node_id": ""}', id="empty-node-id"),
        pytest.param([{"node_id": "  "}], '{"node_id": "  "}', id="blank-node-id"),
        pytest.param([""], '""', id="empty-string"),
        pytest.param([None], "null", id="null-entry"),
        pytest.param([[_X]], f'["{_X}"]', id="nested-list"),
        pytest.param([_X, {"target": _X}], '"target"', id="good-then-bad"),
    ],
)
def test_write_doc_refuses_a_malformed_link_and_writes_nothing(project: Path, links: object, named: str) -> None:
    """A ``links`` value of any other shape is an error naming the section, the entry and the accepted shapes."""
    doc = {
        "id": "bad-links",
        "title": "Bad",
        "sections": [{"id": "overview", "heading": "Overview", "content": "Body.", "links": links}],
    }

    res = axiom_graph_write_doc(str(project), doc)

    assert res.startswith("ERROR:"), res
    assert "'overview'" in res and named in res, res
    assert '{"node_id": "<id>"} or a node-id string' in res
    if isinstance(links, list) and any(isinstance(e, dict) and "node_id" not in e for e in links):
        assert 'use "node_id"; the link type is always documents' in res, res
    assert "nothing was written" in res
    assert not (project / "docs" / "bad-links.docjson").exists()
    assert db.get_node(_db_path(str(project)), "proj::docs/bad-links") is None


def test_overwrite_with_a_malformed_link_leaves_the_file_unchanged(project: Path) -> None:
    """Overwriting a doc with a bad link entry is refused and the existing bytes stay as they were."""
    _write_arch_doc(project)
    arch = project / "docs" / "arch.docjson"
    before = arch.read_bytes()
    doc = {"id": "arch", "title": "Architecture", "sections": [{"id": "overview", "heading": "O", "links": [{}]}]}

    res = axiom_graph_write_doc(str(project), doc)

    assert res.startswith("ERROR:"), res
    assert arch.read_bytes() == before


def test_malformed_link_in_a_nested_section_is_named_by_dot_path(project: Path) -> None:
    """The error locates a bad entry in a nested section by its full dot-path and points at the ``node_id`` key."""
    doc = {
        "id": "nested",
        "title": "Nested",
        "sections": [
            {"id": "parent", "heading": "P", "sections": [{"id": "child", "heading": "C", "links": [{"to": _X}]}]}
        ],
    }

    res = axiom_graph_write_doc(str(project), doc)

    assert res.startswith("ERROR:"), res
    assert "'parent.child'" in res, res
    assert 'use "node_id"; the link type is always documents' in res, res


def _hand_written(project: Path, name: str, links: list) -> Path:
    """Write a DocJSON file directly (not through a doc tool) with one linked section and one plain one."""
    doc = {
        "title": name.title(),
        "sections": [
            {"id": "overview", "heading": "Overview", "content": "Body.", "links": links},
            {"id": "plain", "heading": "Plain", "content": "No links."},
        ],
    }
    path = project / "docs" / f"{name}.docjson"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_link_tools_refuse_a_section_holding_a_malformed_link(project: Path) -> None:
    """add_link and delete_link on a section whose file holds a bad entry are refused and write nothing."""
    path = _hand_written(project, "hand", [_X, {"target": "proj::docs/third"}])
    builder.build(project)
    before = path.read_bytes()

    added = axiom_graph_add_link(str(project), "proj::docs/hand::overview", node_id="proj::docs/fourth")
    removed = axiom_graph_delete_link(str(project), "proj::docs/hand::overview", node_id=_X)

    for res in (added, removed):
        assert res.startswith("ERROR:"), res
        assert '"target"' in res and "nothing was written" in res, res
    assert path.read_bytes() == before


@workflow(
    purpose="A hand-edited DocJSON file with malformed links entries is indexed whole: only the bad entries are "
    "dropped, each with a build warning naming the file, section and entry, and the good link keeps its edge; a "
    "document that does not parse is named in a warning, a JSON data file is not"
)
def test_build_skips_only_the_malformed_link_entries_and_warns(project: Path) -> None:
    _hand_written(project, "hand", [_X, {"target": "proj::docs/third", "type": "documents"}, {"node_id": 123}])
    (project / "docs" / "broken.docjson").write_text('{"title": "Broken", "sections": [', encoding="utf-8")
    (project / "docs" / "legacy.json").write_text('{"title": "Legacy", "sections": [', encoding="utf-8")
    (project / "docs" / "layout.json").write_text('{"columns": 3}', encoding="utf-8")

    db_path = _db_path(str(project))
    summary = build_index(db_path, project)

    for nid in ("proj::docs/hand", "proj::docs/hand::overview", "proj::docs/hand::plain"):
        assert db.get_node(db_path, nid) is not None, nid
    from_overview = [e.to_id for e in db.all_edges(db_path) if e.from_id == "proj::docs/hand::overview"]
    assert from_overview == [_X]
    link_warnings = [w for w in summary.warnings if "docs/hand.docjson" in w and "links entry" in w]
    assert len(link_warnings) == 2, summary.warnings
    assert any("'overview'" in w and '"target"' in w for w in link_warnings), link_warnings
    assert any("'overview'" in w and '{"node_id": 123}' in w for w in link_warnings), link_warnings
    # A document that does not parse is named, with either extension; a JSON data file beside the docs is not.
    assert any("docs/broken.docjson was not indexed" in w for w in summary.warnings), summary.warnings
    assert any("docs/legacy.json was not indexed" in w for w in summary.warnings), summary.warnings
    assert not [w for w in summary.warnings if "layout.json" in w], summary.warnings


def test_indexed_doc_that_gains_a_malformed_link_by_hand_stays_found(project: Path) -> None:
    """A bad links entry added to an indexed doc by hand does not turn its sections NOT_FOUND on check."""
    path = _hand_written(project, "hand", [_X])
    builder.build(project)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sections"][0]["links"].append({"node_id": 123})
    path.write_text(json.dumps(data), encoding="utf-8")

    statuses = compute_check_summary(_db_path(str(project)), project).statuses

    for nid in ("proj::docs/hand", "proj::docs/hand::overview", "proj::docs/hand::plain"):
        assert statuses[nid][0] != "NOT_FOUND", (nid, statuses[nid])


def test_section_edit_on_a_doc_holding_a_non_string_node_id_succeeds(project: Path) -> None:
    """Editing another section of a doc whose links hold ``{"node_id": 123}`` by hand is not broken by that entry."""
    path = _hand_written(project, "hand", [_X])
    builder.build(project)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sections"][0]["links"].append({"node_id": 123})
    path.write_text(json.dumps(data), encoding="utf-8")

    res = axiom_graph_update_section(str(project), "proj::docs/hand::plain", content="Edited.")

    assert "Updated" in res, res
    assert json.loads(path.read_text(encoding="utf-8"))["sections"][1]["content"] == "Edited."


def test_normalize_links_strict_raises_and_lenient_drops_and_reports() -> None:
    """Strict mode raises on a bad entry; lenient mode drops just that entry and reports it, keeping extra keys."""
    links = [_X, {"node_id": _X, "relationship": "documents"}, {"target": _X}, {"node_id": " "}]

    with pytest.raises(json_doc_scanner.MalformedLinkError, match='"target"'):
        json_doc_scanner.normalize_links(links, "section 'a'")
    dropped: list[str] = []
    kept = json_doc_scanner.normalize_links(links, "section 'a'", dropped=dropped)

    assert kept == [{"node_id": _X}, {"node_id": _X, "relationship": "documents"}]
    assert len(dropped) == 2 and '"target"' in dropped[0] and '{"node_id": " "}' in dropped[1], dropped
    assert json_doc_scanner.normalize_links(None, "x") == [] and json_doc_scanner.normalize_links([], "x") == []
    with pytest.raises(json_doc_scanner.MalformedLinkError):
        json_doc_scanner.normalize_links(0, "x")


def test_bare_string_link_written_by_hand_is_indexed_and_editable(project: Path) -> None:
    """A doc file whose ``links`` holds a node-id string indexes the link, and the link tools still edit it."""
    doc = {"title": "Hand", "sections": [{"id": "overview", "heading": "Overview", "links": ["proj::docs/other"]}]}
    (project / "docs" / "hand.docjson").write_text(json.dumps(doc), encoding="utf-8")
    builder.build(project)
    db_path = _db_path(str(project))
    assert any(
        e.from_id == "proj::docs/hand::overview" and e.to_id == "proj::docs/other" for e in db.all_edges(db_path)
    )

    added = axiom_graph_add_link(str(project), "proj::docs/hand::overview", node_id="proj::docs/third")
    removed = axiom_graph_delete_link(str(project), "proj::docs/hand::overview", node_id="proj::docs/other")

    assert not added.startswith("ERROR"), added
    assert not removed.startswith("ERROR") and "No matching" not in removed, removed
    saved = json.loads((project / "docs" / "hand.docjson").read_text(encoding="utf-8"))
    assert [lk["node_id"] for lk in saved["sections"][0]["links"]] == ["proj::docs/third"]
