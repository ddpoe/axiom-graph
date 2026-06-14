"""Tests for broken link detection (ADR-013 Layer 1 + Layer 2).

Tier 1 -- plain pytest:
    find_broken_links() unit tests, BROKEN_LINK severity ordering,
    flag-don't-drop deletion semantics (inbound documents edges kept).

Tier 2 -- @workflow(purpose=...):
    record_staleness overlay, builder post-purge detection,
    rename carve-out (remap before delete leaves no broken link).

Tier 3 -- @workflow + Step():
    fresh-vs-incremental build broken-link parity.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.index import builder, db
from axiom_graph.index.staleness import (
    _LINK_SEVERITY,
    find_broken_links,
    record_staleness,
)
from axiom_graph.models import AxiomEdge, AxiomNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upsert_node(db_path: Path, node_id: str, **kwargs) -> None:
    """Insert a minimal node for testing."""
    parts = node_id.split("::")
    name = parts[-1]
    defaults = dict(
        id=node_id,
        node_type="atomic_process",
        subtype="docjson",
        title=name,
        location="docs/test.json",
        source="json_doc_scanner",
        code_hash="hash_aaa",
        level_0=name,
        level_1=name,
    )
    defaults.update(kwargs)
    node = AxiomNode(**defaults)
    db.upsert_node(db_path, node, discovery_only=False)


def _upsert_code_node(db_path: Path, node_id: str, code_hash: str = "hash_bbb") -> None:
    """Insert a minimal code node for testing."""
    parts = node_id.split("::")
    name = parts[-1]
    node = AxiomNode(
        id=node_id,
        node_type="atomic_process",
        title=name,
        location="mod.py",
        source="ast",
        code_hash=code_hash,
        level_0=name,
        level_1=name,
    )
    db.upsert_node(db_path, node, discovery_only=False)


def _insert_edge(db_path: Path, edge_type: str, from_id: str, to_id: str) -> None:
    """Insert an edge."""
    edge = AxiomEdge(
        id=f"{from_id}::{edge_type}::{to_id}",
        edge_type=edge_type,
        from_id=from_id,
        to_id=to_id,
    )
    db.upsert_edge(db_path, edge)


# ---------------------------------------------------------------------------
# Tier 1 -- find_broken_links() unit tests
# ---------------------------------------------------------------------------


def test_find_broken_links_returns_empty_when_all_targets_exist(mini_project, db_path):
    """No broken links when all edge targets exist as nodes."""
    _upsert_node(db_path, "proj::docs.test::s1")
    _upsert_code_node(db_path, "proj::mod::func_a")
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::mod::func_a")

    broken = find_broken_links(db_path)
    assert broken == {}


def test_find_broken_links_detects_dangling_documents_edge(mini_project, db_path):
    """A documents edge whose to_id has no node should be detected."""
    _upsert_node(db_path, "proj::docs.test::s1")
    # No target node -- edge points to void
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::mod::gone_func")

    broken = find_broken_links(db_path)
    assert "proj::docs.test::s1" in broken
    assert broken["proj::docs.test::s1"] == "proj::mod::gone_func"


def test_find_broken_links_detects_dangling_validates_edge(mini_project, db_path):
    """A validates edge whose to_id has no node should be detected."""
    _upsert_code_node(db_path, "proj::tests.test_mod::test_func")
    # No target node
    _insert_edge(db_path, "validates", "proj::tests.test_mod::test_func", "proj::mod::gone")

    broken = find_broken_links(db_path)
    assert "proj::tests.test_mod::test_func" in broken


def test_find_broken_links_ignores_composes_edges(mini_project, db_path):
    """composes edges should NOT be checked for broken links (per constraints)."""
    _upsert_node(db_path, "proj::docs.test")
    # Dangling composes edge -- should be ignored
    _insert_edge(db_path, "composes", "proj::docs.test", "proj::docs.test::nonexistent")

    broken = find_broken_links(db_path)
    assert broken == {}


def test_broken_link_severity_ordering(mini_project, db_path):
    """BROKEN_LINK should be the highest link-dimension severity."""
    assert _LINK_SEVERITY["BROKEN_LINK"] > _LINK_SEVERITY["LINKED_STALE"]
    assert _LINK_SEVERITY["BROKEN_LINK"] > _LINK_SEVERITY["VERIFIED"]


# ---------------------------------------------------------------------------
# Tier 2 -- record_staleness overlay
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify that record_staleness overlays BROKEN_LINK status on nodes with dangling edges",
)
def test_record_staleness_overlays_broken_links(git_project, git_db_path):
    """A doc section with a dangling documents edge should get BROKEN_LINK status."""
    # Create the doc section node
    _upsert_node(git_db_path, "proj::docs.test::s1")
    # Create a dangling edge (target does not exist)
    _insert_edge(git_db_path, "documents", "proj::docs.test::s1", "proj::mod::gone")

    # Create a minimal file so NOT_FOUND doesn't override
    docs_dir = git_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    doc_content = {
        "title": "Test",
        "sections": [{"id": "s1", "heading": "Section 1", "content": "text"}],
    }
    (docs_dir / "test.json").write_text(json.dumps(doc_content))

    nodes = db.all_nodes(git_db_path)
    statuses = record_staleness(git_db_path, git_project, nodes)

    assert statuses.get("proj::docs.test::s1")[1] == "BROKEN_LINK"


@workflow(
    purpose="Verify that BROKEN_LINK does not override NOT_FOUND (higher severity)",
)
def test_broken_link_does_not_override_structural_drift(git_project, git_db_path):
    """NOT_FOUND is higher severity than BROKEN_LINK and should take precedence."""
    # Create doc section pointing to non-existent file
    _upsert_node(git_db_path, "proj::docs.test::s1", location="docs/nonexistent.json")
    _insert_edge(git_db_path, "documents", "proj::docs.test::s1", "proj::mod::also_gone")

    nodes = db.all_nodes(git_db_path)
    statuses = record_staleness(git_db_path, git_project, nodes)

    # NOT_FOUND should win over BROKEN_LINK
    assert statuses.get("proj::docs.test::s1")[0] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Tier 1 -- flag-don't-drop: node deletion keeps inbound documents edges
# ---------------------------------------------------------------------------


def _edge_tuples(db_path: Path) -> set[tuple[str, str, str]]:
    """All edges as (edge_type, from_id, to_id) tuples."""
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT edge_type, from_id, to_id FROM edges").fetchall()
    return {(r["edge_type"], r["from_id"], r["to_id"]) for r in rows}


def _link_removed_targets(db_path: Path) -> set[str]:
    """Targets of all LINK_REMOVED history rows."""
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT meta FROM node_history WHERE change_type = 'LINK_REMOVED'").fetchall()
    return {json.loads(r["meta"])["target"] for r in rows}


def test_delete_node_by_id_keeps_inbound_documents_edge(mini_project, db_path):
    """Deleting a linked node keeps the inbound documents edge (no LINK_REMOVED)."""
    _upsert_node(db_path, "proj::docs.test::s1")
    _upsert_code_node(db_path, "proj::mod::func_a")
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::mod::func_a")

    with db._connect(db_path) as conn:
        db.delete_node_by_id(conn, "proj::mod::func_a")

    assert ("documents", "proj::docs.test::s1", "proj::mod::func_a") in _edge_tuples(db_path)
    # The existing detector now sees the dangling edge -- no new flagging code
    assert find_broken_links(db_path).get("proj::docs.test::s1") == "proj::mod::func_a"
    # The link was NOT removed, so no LINK_REMOVED history is recorded for it
    assert "proj::mod::func_a" not in _link_removed_targets(db_path)


def test_delete_node_by_id_still_removes_inbound_validates_edge(mini_project, db_path):
    """validates edges are scanner-derived: a fresh build skips dangling ones
    (builder edge upsert), so deletion must drop them too for parity."""
    _upsert_code_node(db_path, "proj::tests.test_mod::test_func")
    _upsert_code_node(db_path, "proj::mod::func_a")
    _insert_edge(db_path, "validates", "proj::tests.test_mod::test_func", "proj::mod::func_a")

    with db._connect(db_path) as conn:
        db.delete_node_by_id(conn, "proj::mod::func_a")

    assert _edge_tuples(db_path) == set()
    assert find_broken_links(db_path) == {}
    # Genuinely deleted edge -> LINK_REMOVED is recorded
    assert "proj::mod::func_a" in _link_removed_targets(db_path)


def test_delete_node_by_id_removes_outbound_documents_edges(mini_project, db_path):
    """Outbound edges of a deleted node are meaningless and are removed."""
    _upsert_node(db_path, "proj::docs.test::s1")
    _upsert_code_node(db_path, "proj::mod::func_a")
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::mod::func_a")

    with db._connect(db_path) as conn:
        db.delete_node_by_id(conn, "proj::docs.test::s1")

    assert _edge_tuples(db_path) == set()
    assert find_broken_links(db_path) == {}


def test_delete_nodes_by_location_keeps_inbound_documents_edge(mini_project, db_path):
    """Location purge keeps inbound documents edges from surviving sources,
    while internal edges between deleted nodes are removed."""
    _upsert_code_node(db_path, "proj::mod::func_a")
    _upsert_code_node(db_path, "proj::mod::func_b")
    _upsert_node(db_path, "proj::docs.test::s1")
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::mod::func_a")
    _insert_edge(db_path, "depends_on", "proj::mod::func_a", "proj::mod::func_b")

    with db._connect(db_path) as conn:
        deleted = db.delete_nodes_by_location(conn, "mod.py")

    assert deleted == 2
    assert _edge_tuples(db_path) == {("documents", "proj::docs.test::s1", "proj::mod::func_a")}
    assert find_broken_links(db_path).get("proj::docs.test::s1") == "proj::mod::func_a"
    assert "proj::mod::func_a" not in _link_removed_targets(db_path)


def test_delete_nodes_by_location_contract_preserved_with_git_sha(mini_project, db_path):
    """Functionality-preservation: passing ``git_sha`` must not change the contract.

    The SHA/span meta enrichment (D-5) amends a hot, well-tested path. This
    asserts the load-bearing behaviour is byte-for-byte identical to the
    no-SHA call: same return count, the inbound ``documents`` edge from a
    surviving source is KEPT (flag-don't-drop), the internal edge between
    deleted nodes is removed, the source is flagged BROKEN_LINK, and the kept
    edge gets NO LINK_REMOVED. It then asserts the NEW behaviour: the SHA is
    written into both the DELETED meta and the ``git_sha`` column, and the
    node's ``level_3_location`` span is preserved in the meta.
    """
    import json

    _upsert_code_node(db_path, "proj::mod::func_a")
    _upsert_code_node(db_path, "proj::mod::func_b")
    _upsert_node(db_path, "proj::docs.test::s1")
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::mod::func_a")
    _insert_edge(db_path, "depends_on", "proj::mod::func_a", "proj::mod::func_b")

    with db._connect(db_path) as conn:
        deleted = db.delete_nodes_by_location(conn, "mod.py", "feedface1234")

    # --- Contract preserved (identical to the no-SHA test) ---
    assert deleted == 2
    assert _edge_tuples(db_path) == {("documents", "proj::docs.test::s1", "proj::mod::func_a")}
    assert find_broken_links(db_path).get("proj::docs.test::s1") == "proj::mod::func_a"
    assert "proj::mod::func_a" not in _link_removed_targets(db_path)

    # --- New behaviour: SHA + span preserved on the DELETED rows ---
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT node_id, git_sha, meta FROM node_history WHERE change_type = 'DELETED'").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["git_sha"] == "feedface1234"
        meta = json.loads(r["meta"])
        assert meta["git_sha"] == "feedface1234"
        # level_3_location key is always present (may be None for nodes without
        # a span, but these code nodes have one from upsert).
        assert "level_3_location" in meta


def test_delete_nodes_by_location_removes_documents_edge_when_both_ends_deleted(mini_project, db_path):
    """No signal is needed when the source dies in the same purge as the target."""
    _upsert_node(db_path, "proj::docs.test::s1")
    _upsert_node(db_path, "proj::docs.test::s2")
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::docs.test::s2")

    with db._connect(db_path) as conn:
        db.delete_nodes_by_location(conn, "docs/test.json")

    assert _edge_tuples(db_path) == set()
    assert find_broken_links(db_path) == {}


def test_delete_doc_by_id_keeps_inbound_documents_edge(mini_project, db_path):
    """Doc deletion keeps inbound documents edges from other docs' sections."""
    _upsert_node(db_path, "proj::docs.dead::s1", location="docs/dead.json")
    _upsert_node(db_path, "proj::docs.alive::s1", location="docs/alive.json")
    _upsert_code_node(db_path, "proj::mod::func_a")
    # Inbound: a surviving doc section links to the doomed doc's section
    _insert_edge(db_path, "documents", "proj::docs.alive::s1", "proj::docs.dead::s1")
    # Outbound: the doomed section links to code -- must be removed
    _insert_edge(db_path, "documents", "proj::docs.dead::s1", "proj::mod::func_a")

    with db._connect(db_path) as conn:
        db.delete_doc_by_id(conn, "proj::docs.dead")

    assert _edge_tuples(db_path) == {("documents", "proj::docs.alive::s1", "proj::docs.dead::s1")}
    assert find_broken_links(db_path).get("proj::docs.alive::s1") == "proj::docs.dead::s1"
    assert "proj::docs.dead::s1" not in _link_removed_targets(db_path)


# ---------------------------------------------------------------------------
# Tier 2 -- rename carve-out: remap runs before delete, no false broken link
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify a rename followed by purge of the old node produces no broken link, "
    "because edge remap runs before the delete sees any inbound documents edge",
)
def test_rename_then_delete_old_node_leaves_no_broken_link(mini_project, db_path):
    """Rename carve-out: a genuine rename must not trip flag-don't-drop."""
    _upsert_code_node(db_path, "proj::mod::func_old", code_hash="hash_abc")
    _upsert_code_node(db_path, "proj::mod::func_new", code_hash="hash_abc")
    _upsert_node(db_path, "proj::docs.test::s1")
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::mod::func_old")

    # Rename remaps the edge to func_new BEFORE any purge runs
    db.record_code_rename(db_path, "proj::mod::func_old", "proj::mod::func_new", "mod.py")
    with db._connect(db_path) as conn:
        db.delete_node_by_id(conn, "proj::mod::func_old")

    assert find_broken_links(db_path) == {}
    assert ("documents", "proj::docs.test::s1", "proj::mod::func_new") in _edge_tuples(db_path)


# ---------------------------------------------------------------------------
# Tier 3 -- parity: incremental build == from-scratch build
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify that after a linked file is deleted, an incremental build and a "
    "from-scratch build of the same tree report identical broken links, so local "
    "check and CI's fresh-build gate can never silently disagree",
)
def test_fresh_vs_incremental_build_broken_link_parity(mini_project, db_path, tmp_path_factory):
    """The regression guard for the whole class: incremental index operations
    must preserve the invariants a full build would compute."""
    口 = Step(
        step_num=1,
        name="Author project",
        purpose="A DocJSON section declares a documents link to a function in src/mod.py",
    )
    src = mini_project / "src"
    src.mkdir()
    (src / "mod.py").write_text("def target():\n    return 1\n", encoding="utf-8")
    docs = mini_project / "docs"
    docs.mkdir()
    doc = {
        "title": "Guide",
        "sections": [
            {
                "id": "s1",
                "heading": "Section 1",
                "content": "Documents target.",
                "links": [{"node_id": "proj::src.mod::target"}],
            }
        ],
    }
    (docs / "guide.json").write_text(json.dumps(doc), encoding="utf-8")

    口 = Step(step_num=2, name="Initial build", purpose="Index both files; link resolves, no broken links")
    builder.build(mini_project, project_id="proj", discovery_only=False)
    assert find_broken_links(db_path) == {}

    口 = Step(step_num=3, name="Delete the linked file", purpose="Remove src/mod.py so the link target disappears")
    (src / "mod.py").unlink()

    口 = Step(
        step_num=4,
        name="Incremental rebuild",
        purpose="Purge deletes the mod nodes; the doc file is unchanged (mtime fast-pass) and is NOT rescanned",
    )
    builder.build(mini_project, project_id="proj", discovery_only=True)
    broken_incremental = find_broken_links(db_path)

    口 = Step(
        step_num=5,
        name="From-scratch build of the same tree",
        purpose="Copy the tree without .axiom_graph and build a fresh index, like CI's gate does",
    )
    fresh_root = tmp_path_factory.mktemp("fresh_parity")
    shutil.copytree(
        mini_project,
        fresh_root,
        ignore=shutil.ignore_patterns(".axiom_graph", ".git"),
        dirs_exist_ok=True,
    )
    builder.build(fresh_root, project_id="proj", discovery_only=False)
    broken_fresh = find_broken_links(fresh_root / ".axiom_graph" / "graph.db")

    口 = Step(
        step_num=6,
        name="Assert parity",
        purpose="Incremental and fresh builds report identical broken links, and the breakage is visible",
    )
    assert broken_incremental == broken_fresh
    assert broken_incremental.get("proj::docs.guide::s1") == "proj::src.mod::target"
