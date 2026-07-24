"""Doc sections as first-class graph nodes: query surface, tool parity, lifecycle.

Covers the envelope-pattern behavior contracts: drift_query reaching
doc-quality signals, MCP doc-tool parity (order round-trip, nested IDs,
writer-is-verifier, sticky link-staleness), purge permanence,
vanished-section pruning, and composes-graph citizenship with envelope
subtree aggregation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.db.docs import DOC_SECTION_LONG_THRESHOLD, get_long_sections
from axiom_graph.docjson.api import (
    axiom_graph_add_section,
    axiom_graph_delete_section,
    axiom_graph_patch_section,
    axiom_graph_read_doc,
    axiom_graph_update_section,
)
from axiom_graph.index import builder, db
from axiom_graph.index.staleness import apply_composite_inheritance
from axiom_graph.lifecycle.api import compute_check_summary, purge_nodes

PROJ_TOML = '[axiom_graph]\nproject_id = "proj"\n'


def _write_project(root: Path, docs: dict[str, dict]) -> Path:
    """Write axiom-graph.toml + DocJSON files, init the DB, return db_path."""
    (root / "axiom-graph.toml").write_text(PROJ_TOML, encoding="utf-8")
    docs_dir = root / "docs"
    docs_dir.mkdir(exist_ok=True)
    for slug, data in docs.items():
        (docs_dir / f"{slug}.json").write_text(json.dumps(data), encoding="utf-8")
    ag = root / ".axiom_graph"
    ag.mkdir(exist_ok=True)
    db_path = ag / "graph.db"
    db.init_db(db_path)
    return db_path


def _set_link_status(db_path: Path, node_id: str, status: str) -> None:
    with db._connect(db_path) as conn:
        conn.execute("UPDATE nodes SET link_status = ? WHERE id = ?", (status, node_id))


def _drift_ids(project_root: Path, filter: str) -> set[str]:
    from axiom_graph.mcp_server import axiom_graph_drift_query

    out = axiom_graph_drift_query(str(project_root), filter=filter, format="ids", limit=1000)
    return {ln for ln in out.splitlines() if ln and not ln.startswith("[") and not ln.startswith("(")}


@workflow(purpose="Every doc-section health signal is reachable through the standard drift/check query surface")
def test_drift_query_reaches_doc_quality_and_link_signals(tmp_path: Path) -> None:
    """One query surface: over-long and link-stale sections both reachable, agreeing with check."""
    口 = Step(
        step_num=1,
        name="Build project with an over-long section and a section documenting code",
        purpose="Real build so sections are ordinary graph nodes with a real documents edge to code",
    )
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    db_path = _write_project(
        tmp_path,
        {
            "spec": {
                "title": "Spec",
                "sections": [
                    {"id": "long", "heading": "Long", "content": "x" * (DOC_SECTION_LONG_THRESHOLD + 100)},
                    {
                        "id": "other",
                        "heading": "Other",
                        "content": "documents f",
                        "links": [{"node_id": "proj::mod::f"}],
                    },
                ],
            }
        },
    )
    builder.build(tmp_path, discovery_only=False)

    口 = Step(
        step_num=2,
        name="Drift the documented code and rebuild",
        purpose="LINKED_STALE arises through the real path: the linked code changes and the full rebuild records it",
    )
    other_id = "proj::docs.spec::other"
    long_id = "proj::docs.spec::long"
    time.sleep(0.05)
    (tmp_path / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    builder.build(tmp_path, discovery_only=False)

    口 = Step(
        step_num=3,
        name="Check derives and persists both signals",
        purpose="The staleness recompute flags the documenting section from the recorded code change",
    )
    summary = compute_check_summary(db_path, tmp_path)
    assert summary is not None
    assert summary.link_counts.get("LINKED_STALE", 0) > 0

    口 = Step(
        step_num=4,
        name="Query both signals through drift_query",
        purpose="Doc-quality and link staleness flow through the standard query surface",
    )
    assert long_id in _drift_ids(tmp_path, "DOC_SECTION_LONG")
    assert other_id in _drift_ids(tmp_path, "LINKED_STALE")

    口 = Step(
        step_num=5,
        name="Counts agree with check",
        purpose="No parallel store: a fresh check recompute preserves LINKED_STALE and matches drift_query row-for-row",
    )
    summary = compute_check_summary(db_path, tmp_path)
    assert summary is not None
    linked_stale_ids = _drift_ids(tmp_path, "LINKED_STALE")
    assert other_id in linked_stale_ids
    assert summary.link_counts.get("LINKED_STALE", 0) == len(linked_stale_ids) > 0
    assert summary.doc_quality_count == len(get_long_sections(db_path)) == len(_drift_ids(tmp_path, "DOC_SECTION_LONG"))


@workflow(
    purpose="Doc CRUD tools keep identical behavior: order round-trip, nested IDs, writer-is-verifier, sticky link-staleness"
)
def test_doc_tool_parity_roundtrip(tmp_path: Path) -> None:
    """Doc tools behave as before: order round-trips, nested IDs resolve, edits auto-mark, unrelated link-staleness sticks."""
    口 = Step(
        step_num=1, name="Build a nested doc", purpose="Sections incl. a dot-path child on a normally-built project"
    )
    db_path = _write_project(
        tmp_path,
        {
            "arch": {
                "title": "Architecture",
                "sections": [
                    {"id": "overview", "heading": "Overview", "content": "the overview"},
                    {
                        "id": "database-layer",
                        "heading": "Database Layer",
                        "content": "db overview",
                        "sections": [{"id": "tables", "heading": "Tables", "content": "table details"}],
                    },
                    {"id": "appendix", "heading": "Appendix", "content": "appendix body"},
                ],
            }
        },
    )
    builder.build(tmp_path)
    doc_id = "proj::docs.arch"
    appendix_id = f"{doc_id}::appendix"
    nested_id = f"{doc_id}::database-layer.tables"
    _set_link_status(db_path, appendix_id, "LINKED_STALE")

    口 = Step(
        step_num=2,
        name="Update a nested dot-path section",
        purpose="Nested IDs resolve; the edited section auto-marks (writer-is-verifier)",
    )
    out = axiom_graph_update_section(str(tmp_path), nested_id, content="fresh table details")
    assert "Error" not in out
    with db._connect(db_path) as conn:
        row = conn.execute("SELECT own_status, level_2 FROM nodes WHERE id = ?", (nested_id,)).fetchone()
    assert row is not None
    assert row["level_2"] == "fresh table details"
    assert row["own_status"] == "VERIFIED"

    口 = Step(
        step_num=3,
        name="Unrelated link-staleness sticks",
        purpose="Editing one section never clears another node's LINKED_STALE",
    )
    with db._connect(db_path) as conn:
        link = conn.execute("SELECT link_status FROM nodes WHERE id = ?", (appendix_id,)).fetchone()[0]
    assert link == "LINKED_STALE"

    口 = Step(step_num=4, name="Patch, add, delete", purpose="The remaining CRUD tools keep identical behavior")
    out = axiom_graph_patch_section(str(tmp_path), f"{doc_id}::overview", new_string="appended line", anchor="$")
    assert "Error" not in out
    rendered = axiom_graph_read_doc(str(tmp_path), doc_id)
    assert "appended line" in rendered
    out = axiom_graph_add_section(str(tmp_path), doc_id, "new-sec", "New Section", content="new body")
    assert "Error" not in out
    assert "new body" in axiom_graph_read_doc(str(tmp_path), doc_id)
    out = axiom_graph_delete_section(str(tmp_path), f"{doc_id}::new-sec")
    assert "Error" not in out

    口 = Step(
        step_num=5,
        name="Rendered order round-trips",
        purpose="read_doc order is document order after edits and a rebuild",
    )
    builder.build(tmp_path)
    rendered = axiom_graph_read_doc(str(tmp_path), doc_id)
    positions = [rendered.index(marker) for marker in ("Overview", "Database Layer", "Tables", "Appendix")]
    assert positions == sorted(positions), f"section order drifted: {positions}"
    assert "new body" not in rendered


@workflow(purpose="A purged doc section stays purged across rebuilds with its preserved tombstone intact")
def test_purged_section_stays_purged_across_builds(tmp_path: Path) -> None:
    """Purging a section is permanent: no NOT_FOUND resurrection on later builds."""
    口 = Step(
        step_num=1,
        name="Build then delete the doc file",
        purpose="Removing a doc file purges its sections at the next build",
    )
    db_path = _write_project(
        tmp_path,
        {"temp": {"title": "Temp", "sections": [{"id": "a", "heading": "A", "content": "aa"}]}},
    )
    builder.build(tmp_path)
    (tmp_path / "docs" / "temp.json").unlink()
    builder.build(tmp_path)
    sec_id = "proj::docs.temp::a"

    口 = Step(
        step_num=2,
        name="Section purged with preserved tombstone",
        purpose="Deletion is recorded as a preserved DELETED history row",
    )
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT 1 FROM nodes WHERE id = ?", (sec_id,)).fetchone() is None
        assert (
            conn.execute(
                "SELECT 1 FROM node_history WHERE node_id = ? AND change_type = 'DELETED' AND preserved = 1",
                (sec_id,),
            ).fetchone()
            is not None
        )
    # Purging an already-removed node is a clean no-op, not an error.
    results = purge_nodes(db_path, [sec_id], "test cleanup")
    assert not results[0].purged
    assert results[0].reason == "not_found_in_index"

    口 = Step(
        step_num=3, name="Build twice more", purpose="The section must stay gone — no orphan store can resurrect it"
    )
    builder.build(tmp_path)
    builder.build(tmp_path)
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT 1 FROM nodes WHERE id = ?", (sec_id,)).fetchone() is None
        tomb = conn.execute(
            "SELECT 1 FROM node_history WHERE node_id = ? AND change_type = 'DELETED' AND preserved = 1",
            (sec_id,),
        ).fetchone()
    assert tomb is not None, "preserved DELETED tombstone must survive rebuilds"


@workflow(purpose="Removing a section via raw JSON edit prunes its node and edges on the next build")
def test_vanished_section_pruned_on_build(tmp_path: Path) -> None:
    db_path = _write_project(
        tmp_path,
        {
            "prune": {
                "title": "Prune",
                "sections": [
                    {"id": "keep", "heading": "Keep", "content": "stays"},
                    {"id": "drop", "heading": "Drop", "content": "goes"},
                ],
            }
        },
    )
    builder.build(tmp_path)
    drop_id = "proj::docs.prune::drop"
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT 1 FROM nodes WHERE id = ?", (drop_id,)).fetchone() is not None

    # Raw JSON edit (not a doc tool): rewrite the file without 'drop'.
    (tmp_path / "docs" / "prune.json").write_text(
        json.dumps({"title": "Prune", "sections": [{"id": "keep", "heading": "Keep", "content": "stays"}]}),
        encoding="utf-8",
    )
    builder.build(tmp_path)

    with db._connect(db_path) as conn:
        assert conn.execute("SELECT 1 FROM nodes WHERE id = ?", (drop_id,)).fetchone() is None
        dangling = conn.execute("SELECT 1 FROM edges WHERE from_id = ? OR to_id = ?", (drop_id, drop_id)).fetchone()
        keep = conn.execute("SELECT 1 FROM nodes WHERE id = 'proj::docs.prune::keep'").fetchone()
    assert dangling is None, "pruned section must take its edges with it"
    assert keep is not None


@workflow(
    purpose="Sections are graph citizens: composes traversal works, no self-loops, and the envelope aggregates status over its whole subtree"
)
def test_graph_citizenship_composes_and_subtree_aggregation(tmp_path: Path) -> None:
    db_path = _write_project(
        tmp_path,
        {
            "nest": {
                "title": "Nest",
                "sections": [
                    {
                        "id": "parent",
                        "heading": "Parent",
                        "content": "p",
                        "sections": [{"id": "child", "heading": "Child", "content": "c"}],
                    }
                ],
            }
        },
    )
    builder.build(tmp_path)
    doc_id = "proj::docs.nest"
    parent_id = f"{doc_id}::parent"
    child_id = f"{doc_id}::parent.child"

    with db._connect(db_path) as conn:
        composes = [
            (r[0], r[1]) for r in conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type='composes'").fetchall()
        ]
    # Outbound traversal from the parent section reaches its child.
    assert (parent_id, child_id) in composes
    assert (doc_id, parent_id) in composes
    # Ingestion never writes self-loop composes edges.
    assert all(f != t for f, t in composes)

    # Envelope inherited status aggregates over the whole subtree: a stale
    # grandchild (child of an atomic mid-tree section) surfaces on the doc.
    statuses = {child_id: ("CONTENT_UPDATED", "VERIFIED")}
    apply_composite_inheritance(statuses, db_path)
    env_own, _env_link = statuses[doc_id]
    assert env_own == "CONTENT_UPDATED", "envelope must see the stale grandchild, not just direct children"
