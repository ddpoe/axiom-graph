"""Tests for ``documents`` edge reconciliation in ``build()``.

The build pass treats DocJSON ``links`` arrays as the source of truth for
``documents`` edges.  Whenever a DocJSON file is rewritten outside the
tool-mediated ``add_link``/``delete_link`` path (raw editor, bulk
find-replace, manual edits, etc.) the next ``build()`` must delete any
``documents`` edge in the DB whose target is no longer in the section's
``links`` array — including the empty-array case.

Tier 2 (subsystem) and Tier 3 (e2e user-story) tests live below; tier 1
unit coverage of the DB primitives is implicit through these flows.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.db import edges as db_edges
from axiom_graph.docjson import api as docjson_api
from axiom_graph.index import builder, db
from axiom_graph.index.staleness import find_broken_links
from axiom_graph.models import AxiomEdge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_doc(project_root: Path, filename: str, sections: list[dict], *, title: str = "Doc") -> Path:
    """Write a DocJSON file under ``project_root/docs/`` and return its path."""
    docs_dir = project_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    doc_path = docs_dir / filename
    doc_path.write_text(
        json.dumps({"title": title, "sections": sections}, indent=2),
        encoding="utf-8",
    )
    return doc_path


def _write_code_module(project_root: Path, module_name: str, body: str) -> Path:
    """Write a tiny .py file at project_root/<module>.py."""
    src = project_root / f"{module_name}.py"
    src.write_text(body, encoding="utf-8")
    return src


def _outbound_documents(db_path: Path, from_id: str) -> set[str]:
    """Return the set of documents-edge to_ids for ``from_id``."""
    with db._connect(db_path) as conn:
        return db_edges.get_outbound_documents_targets_conn(conn, from_id)


def _history_rows_for(db_path: Path, node_id: str, change_type: str) -> list[dict]:
    """Return history rows matching change_type for the given node."""
    rows = db.get_history(db_path, node_id, limit=100)
    return [r for r in rows if r["change_type"] == change_type]


def _patch_doc_links(doc_path: Path, section_id: str, new_links: list[dict]) -> None:
    """Rewrite the ``links`` array of the top-level section ``section_id``.

    Mimics a raw external edit (not via add_link/delete_link).
    """
    data = json.loads(doc_path.read_text(encoding="utf-8"))
    for sec in data.get("sections", []):
        if sec.get("id") == section_id:
            sec["links"] = new_links
            break
    else:  # pragma: no cover -- defensive
        raise AssertionError(f"section {section_id!r} not in {doc_path}")
    doc_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tier 3 — user-story-level e2e scenarios
# ---------------------------------------------------------------------------


@workflow(
    purpose="External edit removes one link from a two-link section; rebuild reconciles documents edges to match JSON",
)
def test_external_edit_removes_one_link_drops_one_edge(mini_project, db_path):
    """US-1: Two-link section -> external rewrite removes one -> exactly one edge remains.

    Tier 3 -- mirrors the workflow a contributor follows when bulk-rewriting
    DocJSON files (e.g. repointing links after a node rename).
    """
    口 = Step(step_num=1, name="Seed two code targets and two-link section", purpose="setup")
    _write_code_module(mini_project, "mod_a", "def func_a():\n    pass\n")
    _write_code_module(mini_project, "mod_b", "def func_b():\n    pass\n")
    doc_path = _write_doc(
        mini_project,
        "guide.json",
        [
            {
                "id": "intro",
                "heading": "Intro",
                "content": "Overview.",
                "links": [
                    {"node_id": "proj::mod_a::func_a"},
                    {"node_id": "proj::mod_b::func_b"},
                ],
            }
        ],
    )

    口 = Step(step_num=2, name="Initial build seeds two documents edges", purpose="baseline state")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    section_id = "proj::docs.guide::intro"
    edges_before = _outbound_documents(db_path, section_id)
    assert edges_before == {"proj::mod_a::func_a", "proj::mod_b::func_b"}

    口 = Step(step_num=3, name="External edit removes one link from JSON", purpose="simulate raw edit")
    _patch_doc_links(
        doc_path,
        "intro",
        [{"node_id": "proj::mod_a::func_a"}],
    )

    口 = Step(step_num=4, name="Rebuild reconciles edge set to match JSON", purpose="exercise reconciler")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    edges_after = _outbound_documents(db_path, section_id)
    assert edges_after == {"proj::mod_a::func_a"}, f"Expected one edge after reconciliation, got: {edges_after}"

    口 = Step(
        step_num=5, name="find_broken_links returns nothing for this section", purpose="verify BROKEN_LINK cleared"
    )
    broken = find_broken_links(db_path)
    assert section_id not in broken


@workflow(
    purpose="External edit removes a link that pointed at a deleted node; rebuild clears orphan edge and the BROKEN_LINK flag",
)
def test_cortex_diff_repro_orphan_edge_to_deleted_target(mini_project, db_path):
    """US-1 / cortex-diff repro: dangling link removal clears BROKEN_LINK after rebuild.

    Tier 3 -- this is the exact scenario from the cycle request: a doc link
    points at a code node that was deleted in a prior cycle; the doc is
    bulk-rewritten to drop the dangling link.  Before the fix, the orphan
    edge persisted in the DB and find_broken_links continued to report.
    """
    口 = Step(step_num=1, name="Seed two real code targets and a doc with one dangling link", purpose="setup")
    _write_code_module(mini_project, "real_mod", "def real_func():\n    pass\n")
    doc_path = _write_doc(
        mini_project,
        "diff.json",
        [
            {
                "id": "cortex-diff",
                "heading": "Cortex diff",
                "content": "Diff feature notes.",
                "links": [
                    {"node_id": "proj::real_mod::real_func"},
                    # This target intentionally does NOT exist — simulating
                    # a link that survived a node deletion.
                    {"node_id": "proj::ghost_mod::removed_func"},
                ],
            }
        ],
    )

    口 = Step(
        step_num=2, name="Initial build creates both edges; one is dangling", purpose="baseline broken-link state"
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    section_id = "proj::docs.diff::cortex-diff"
    assert _outbound_documents(db_path, section_id) == {
        "proj::real_mod::real_func",
        "proj::ghost_mod::removed_func",
    }
    broken_before = find_broken_links(db_path)
    assert broken_before.get(section_id) == "proj::ghost_mod::removed_func"

    口 = Step(step_num=3, name="External edit drops the dangling link", purpose="simulate raw edit")
    _patch_doc_links(
        doc_path,
        "cortex-diff",
        [{"node_id": "proj::real_mod::real_func"}],
    )

    口 = Step(
        step_num=4, name="Rebuild reconciles and clears the orphan; BROKEN_LINK gone", purpose="exercise reconciler"
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    assert _outbound_documents(db_path, section_id) == {"proj::real_mod::real_func"}

    broken_after = find_broken_links(db_path)
    assert section_id not in broken_after


@workflow(
    purpose="External edit empties a section's links array entirely; rebuild deletes every outbound documents edge from that section",
)
def test_empty_links_array_drops_all_outbound_edges(mini_project, db_path):
    """US-1 (empty-array invariant): links=[] -> zero outbound edges + LINK_REMOVED row.

    Tier 3 -- this is the load-bearing test for the empty-array invariant.
    The reconciler MUST detect sections whose ``links`` array is empty
    (so they emit zero documents edges this build).  If the scanned-section
    signal were derived from ``all_edges`` from-ids, this case would fail
    silently and leave orphan edges in place.
    """
    口 = Step(step_num=1, name="Seed code target and one-link section", purpose="setup")
    _write_code_module(mini_project, "mod_x", "def func_x():\n    pass\n")
    doc_path = _write_doc(
        mini_project,
        "ref.json",
        [
            {
                "id": "main",
                "heading": "Main",
                "content": "Body.",
                "links": [{"node_id": "proj::mod_x::func_x"}],
            }
        ],
    )

    口 = Step(step_num=2, name="Initial build seeds one documents edge", purpose="baseline state")
    builder.build(mini_project, project_id="proj", discovery_only=False)
    section_id = "proj::docs.ref::main"
    assert _outbound_documents(db_path, section_id) == {"proj::mod_x::func_x"}

    口 = Step(
        step_num=3, name="External edit empties the links array (heading/content unchanged)", purpose="empty-array case"
    )
    _patch_doc_links(doc_path, "main", [])

    口 = Step(
        step_num=4,
        name="Rebuild reconciles section to zero outbound documents edges",
        purpose="exercise reconciler on empty links",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    assert _outbound_documents(db_path, section_id) == set()

    口 = Step(
        step_num=5, name="LINK_REMOVED history row exists with actor=build:reconcile", purpose="verify history emission"
    )
    link_removed_rows = _history_rows_for(db_path, section_id, "LINK_REMOVED")
    matching = [
        r
        for r in link_removed_rows
        if (
            json.loads(r["meta"] or "{}").get("actor") == "build:reconcile"
            and json.loads(r["meta"] or "{}").get("target") == "proj::mod_x::func_x"
        )
    ]
    assert matching, f"Expected a LINK_REMOVED row tagged actor=build:reconcile; got: {link_removed_rows}"


# ---------------------------------------------------------------------------
# Tier 2 — subsystem tests
# ---------------------------------------------------------------------------


@workflow(
    purpose="Tool-path add_link then delete_link round-trip is unchanged after reconciler lands",
)
def test_tool_path_add_then_delete_link_unchanged(mini_project, db_path):
    """US-2: agent-driven add_link/delete_link round-trip still works.

    History rows are tagged ``actor: "agent"``, NOT ``"build:reconcile"``.
    """
    _write_code_module(mini_project, "mod_y", "def func_y():\n    pass\n")
    _write_doc(
        mini_project,
        "tour.json",
        [
            {
                "id": "section-1",
                "heading": "Section 1",
                "content": "Body.",
                "links": [],
            }
        ],
    )

    builder.build(mini_project, project_id="proj", discovery_only=False)
    section_id = "proj::docs.tour::section-1"
    assert _outbound_documents(db_path, section_id) == set()

    add_result = docjson_api.axiom_graph_add_link(
        str(mini_project),
        section_id,
        node_id="proj::mod_y::func_y",
    )
    assert "Added 1 link" in add_result
    assert _outbound_documents(db_path, section_id) == {"proj::mod_y::func_y"}

    delete_result = docjson_api.axiom_graph_delete_link(
        str(mini_project),
        section_id,
        node_id="proj::mod_y::func_y",
    )
    assert "Removed 1 link" in delete_result
    assert _outbound_documents(db_path, section_id) == set()

    # Tool-path history rows must remain tagged "agent" — NOT "build:reconcile".
    rows = _history_rows_for(db_path, section_id, "LINK_REMOVED")
    assert rows, "delete_link should produce a LINK_REMOVED row"
    metas = [json.loads(r["meta"] or "{}") for r in rows]
    actors = {m.get("actor") for m in metas}
    assert "agent" in actors
    assert "build:reconcile" not in actors


@workflow(
    purpose="delete_link on a non-existent target returns the canonical 'No matching links' message and writes no history",
)
def test_tool_path_delete_link_absent_target_returns_message(mini_project, db_path):
    """US-2: delete_link of absent node_id -> 'No matching links found'."""
    _write_doc(
        mini_project,
        "empty.json",
        [
            {
                "id": "sec",
                "heading": "Sec",
                "content": "Body.",
                "links": [],
            }
        ],
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    section_id = "proj::docs.empty::sec"

    result = docjson_api.axiom_graph_delete_link(
        str(mini_project),
        section_id,
        node_id="proj::nowhere::missing",
    )
    assert "No matching links found" in result

    # No LINK_REMOVED row should have been written.
    rows = _history_rows_for(db_path, section_id, "LINK_REMOVED")
    assert rows == [], f"Expected no LINK_REMOVED rows, got: {rows}"


@workflow(
    purpose="Reconciliation pass is scoped strictly to documents edges; validates/composes survive",
)
def test_non_documents_edges_preserved(mini_project, db_path):
    """US-3: validates and composes edges from a scanned section are not touched.

    Seed validates + composes edges with the same from_id as a scanned section;
    rebuild; both must still be in the DB.
    """
    _write_code_module(mini_project, "mod_z", "def func_z():\n    pass\n")
    _write_doc(
        mini_project,
        "scope.json",
        [
            {
                "id": "sec",
                "heading": "Sec",
                "content": "Body.",
                "links": [{"node_id": "proj::mod_z::func_z"}],
            }
        ],
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    section_id = "proj::docs.scope::sec"

    # Inject a synthetic validates and composes edge with the same from_id as
    # the scanned section.  These are off-ontology (sections don't usually
    # emit validates) but the reconciler must not touch them by edge_type.
    with db._connect(db_path) as conn:
        for et, to in [
            ("validates", "proj::mod_z::func_z"),
            ("composes", "proj::docs.scope::child-faux"),
        ]:
            edge = AxiomEdge(
                id=f"{section_id}::{et}::{to}",
                edge_type=et,
                from_id=section_id,
                to_id=to,
            )
            db_edges.upsert_edge_conn(conn, edge)

    builder.build(mini_project, project_id="proj", discovery_only=False)

    # documents edge survives because JSON still has the link.
    assert _outbound_documents(db_path, section_id) == {"proj::mod_z::func_z"}

    # validates + composes edges must survive — they live in different
    # edge_type rows and are out of the reconciler's scope.
    with db._connect(db_path) as conn:
        survivors = conn.execute(
            "SELECT edge_type, to_id FROM edges WHERE from_id = ? AND edge_type IN ('validates', 'composes')",
            (section_id,),
        ).fetchall()
    pairs = {(r["edge_type"], r["to_id"]) for r in survivors}
    assert ("validates", "proj::mod_z::func_z") in pairs
    assert ("composes", "proj::docs.scope::child-faux") in pairs


@workflow(
    purpose="mtime-skipped DocJSON files are NOT reconciled; their previously-seeded orphan documents edges persist",
)
def test_mtime_skipped_files_not_touched(mini_project, db_path):
    """Safety: sections inside mtime-skipped files keep their existing edges.

    Build doc -> directly insert an orphan documents edge from one of the
    sections -> build again WITHOUT touching the file -> orphan persists
    (because the section is not in scanned_section_ids for the second build).
    """
    _write_code_module(mini_project, "mod_q", "def func_q():\n    pass\n")
    _write_doc(
        mini_project,
        "stable.json",
        [
            {
                "id": "sec",
                "heading": "Sec",
                "content": "Body.",
                "links": [{"node_id": "proj::mod_q::func_q"}],
            }
        ],
    )

    # First build (writes section + edges, records mtime).
    builder.build(mini_project, project_id="proj", discovery_only=True)
    section_id = "proj::docs.stable::sec"
    assert _outbound_documents(db_path, section_id) == {"proj::mod_q::func_q"}

    # Inject an orphan documents edge that is NOT in the JSON links array.
    orphan_target = "proj::ghost::vanished"
    with db._connect(db_path) as conn:
        edge = AxiomEdge(
            id=f"{section_id}::documents::{orphan_target}",
            edge_type="documents",
            from_id=section_id,
            to_id=orphan_target,
        )
        db_edges.upsert_edge_conn(conn, edge)
    assert orphan_target in _outbound_documents(db_path, section_id)

    # Rebuild WITHOUT touching the file — mtime fast-pass skips it.
    # Because the section is not in scanned_section_ids, the reconciler
    # must leave the orphan edge in place.
    builder.build(mini_project, project_id="proj", discovery_only=True)

    surviving = _outbound_documents(db_path, section_id)
    assert orphan_target in surviving, (
        f"Orphan edge from mtime-skipped section was incorrectly reconciled. Edges now: {surviving}"
    )
