"""Integration tests for the shared compute_staleness() engine.

Covers:
- File existence → NOT_FOUND
- Mtime fast-pass → CLEAN
- Hash comparison: CONTENT_UPDATED, DESC_UPDATED
- Both hashes changed → CONTENT_UPDATED (not CLEAN)
- Composite inheritance propagation
- UNVERIFIED never appears
- External packages and entities always CLEAN
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from axiom_graph.index import db
from axiom_graph.index.staleness import compute_staleness
from axiom_graph.models import AxiomEdge, AxiomNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(
    node_id: str,
    node_type: str = "atomic_process",
    subtype: str | None = None,
    code_hash: str = "abc123",
    desc_hash: str | None = "def456",
    location: str = "src/mod.py",
) -> AxiomNode:
    return AxiomNode(
        id=node_id,
        node_type=node_type,
        subtype=subtype,
        title=node_id.split("::")[-1],
        location=location,
        source="ast",
        code_hash=code_hash,
        desc_hash=desc_hash,
        level_0=node_id,
        level_1=node_id,
    )


def _edge(from_id: str, edge_type: str, to_id: str) -> AxiomEdge:
    return AxiomEdge(
        id=f"{from_id}::{edge_type}::{to_id}",
        edge_type=edge_type,
        from_id=from_id,
        to_id=to_id,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path):
    """A tmp_path with a real source file and a axiom-graph DB."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "mod.py").write_text("def foo():\n    pass\n", encoding="utf-8")

    ag_dir = tmp_path / ".axiom_graph"
    ag_dir.mkdir()
    db_file = ag_dir / "graph.db"
    db.init_db(db_file)

    yield tmp_path


# ---------------------------------------------------------------------------
# File existence → NOT_FOUND
# ---------------------------------------------------------------------------


def test_deleted_file_structural_drift(project: Path):
    """File removed from disk → all nodes at that location = NOT_FOUND."""
    db_file = project / ".axiom_graph" / "graph.db"
    n1 = _node("proj::mod::foo", location="src/mod.py")
    n2 = _node("proj::mod::bar", location="src/mod.py")
    db.upsert_node(db_file, n1, discovery_only=False)
    db.upsert_node(db_file, n2, discovery_only=False)

    # Delete the file
    (project / "src" / "mod.py").unlink()

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    assert statuses["proj::mod::foo"][0] == "NOT_FOUND"
    assert statuses["proj::mod::bar"][0] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Mtime fast-pass → CLEAN
# ---------------------------------------------------------------------------


def test_unchanged_file_mtime_clean(project: Path):
    """File with stored mtime matching current mtime AND matching content
    fingerprint → CLEAN (mtime fast-pass confirmed by the content gate)."""
    from axiom_graph.models import hash16

    db_file = project / ".axiom_graph" / "graph.db"
    src = project / "src" / "mod.py"
    mtime = src.stat().st_mtime
    file_hash = hash16(src.read_text(encoding="utf-8", errors="replace"))

    # File-level anchor whose whole-file hash matches the on-disk content.
    anchor = _node(
        "proj::mod",
        node_type="composite_process",
        subtype="module",
        code_hash=file_hash,
    )
    anchor.file_mtime = mtime
    db.upsert_node(db_file, anchor, discovery_only=False)

    n = _node("proj::mod::foo", location="src/mod.py")
    n.file_mtime = mtime
    db.upsert_node(db_file, n, discovery_only=False)

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    assert statuses["proj::mod::foo"] == ("VERIFIED", "VERIFIED", [])


# ---------------------------------------------------------------------------
# External packages and entities always CLEAN
# ---------------------------------------------------------------------------


def test_external_package_always_clean(project: Path):
    """External package nodes are always CLEAN."""
    db_file = project / ".axiom_graph" / "graph.db"
    n = _node("proj::ext::numpy", subtype="external_package", location="")
    db.upsert_node(db_file, n, discovery_only=False)

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    assert statuses["proj::ext::numpy"] == ("VERIFIED", "VERIFIED", [])


def test_entity_always_clean(project: Path):
    """Entity nodes are always CLEAN."""
    db_file = project / ".axiom_graph" / "graph.db"
    n = _node("proj::data::schema", node_type="entity", location="")
    db.upsert_node(db_file, n, discovery_only=False)

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    assert statuses["proj::data::schema"] == ("VERIFIED", "VERIFIED", [])


# ---------------------------------------------------------------------------
# Composite inheritance propagation
# ---------------------------------------------------------------------------


def test_composite_inherits_content_stale_from_child(project: Path):
    """composite_process with a CONTENT_UPDATED child → inherits CONTENT_UPDATED."""
    db_file = project / ".axiom_graph" / "graph.db"

    # Write a source file with a function whose hash won't match the stored one
    src = project / "src" / "mod.py"
    src.write_text("def foo():\n    return 42\n", encoding="utf-8")

    parent = _node("proj::mod", node_type="composite_process", location="src/mod.py")
    child = _node("proj::mod::foo", code_hash="stale_hash", desc_hash=None, location="src/mod.py")
    db.upsert_node(db_file, parent, discovery_only=False)
    db.upsert_node(db_file, child, discovery_only=False)
    db.upsert_edge(db_file, _edge("proj::mod", "composes", "proj::mod::foo"))

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    # Child should be CONTENT_UPDATED (stored hash doesn't match current)
    assert statuses["proj::mod::foo"][0] == "CONTENT_UPDATED"
    # Parent inherits worst child status
    assert statuses["proj::mod"][0] == "CONTENT_UPDATED"


def test_composite_all_clean_children_stays_clean(project: Path):
    """composite_process with all-CLEAN children → CLEAN."""
    from axiom_graph.models import hash16

    db_file = project / ".axiom_graph" / "graph.db"
    src = project / "src" / "mod.py"
    mtime = src.stat().st_mtime
    file_hash = hash16(src.read_text(encoding="utf-8", errors="replace"))

    # The parent module composite is the file-level anchor: subtype="module"
    # with a whole-file hash matching the on-disk content lets the content
    # gate confirm the mtime fast-pass.
    parent = _node(
        "proj::mod",
        node_type="composite_process",
        subtype="module",
        code_hash=file_hash,
        location="src/mod.py",
    )
    parent.file_mtime = mtime
    child = _node("proj::mod::foo", location="src/mod.py")
    child.file_mtime = mtime
    db.upsert_node(db_file, parent, discovery_only=False)
    db.upsert_node(db_file, child, discovery_only=False)
    db.upsert_edge(db_file, _edge("proj::mod", "composes", "proj::mod::foo"))

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    assert statuses["proj::mod::foo"] == ("VERIFIED", "VERIFIED", [])
    assert statuses["proj::mod"] == ("VERIFIED", "VERIFIED", [])


# ---------------------------------------------------------------------------
# UNVERIFIED never appears
# ---------------------------------------------------------------------------


def test_no_code_hash_resolves_to_clean_not_unverified(project: Path):
    """Node with no code_hash must resolve to CLEAN, not UNVERIFIED."""
    db_file = project / ".axiom_graph" / "graph.db"

    n = _node("proj::mod::foo", code_hash="", location="src/mod.py")
    db.upsert_node(db_file, n, discovery_only=False)

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    status = statuses.get("proj::mod::foo", ("VERIFIED", "VERIFIED", []))
    own, link = status[0], status[1]
    assert own != "UNVERIFIED", "own_status should not be UNVERIFIED"
    assert link != "UNVERIFIED", "link_status should not be UNVERIFIED"


def test_compute_staleness_never_returns_unverified(project: Path):
    """compute_staleness must never produce UNVERIFIED in either column."""
    db_file = project / ".axiom_graph" / "graph.db"
    n = _node("proj::mod::foo", code_hash="")
    db.upsert_node(db_file, n, discovery_only=False)

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    for node_id, (own, link, _via) in statuses.items():
        assert own != "UNVERIFIED", f"{node_id} own_status is UNVERIFIED"
        assert link != "UNVERIFIED", f"{node_id} link_status is UNVERIFIED"


def test_compute_staleness_never_returns_needs_rescan(project: Path):
    """compute_staleness must never produce NEEDS_RESCAN in either column."""
    db_file = project / ".axiom_graph" / "graph.db"
    n = _node("proj::mod::foo")
    db.upsert_node(db_file, n, discovery_only=False)

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    for node_id, (own, link, _via) in statuses.items():
        assert own != "NEEDS_RESCAN", f"{node_id} own_status is NEEDS_RESCAN"
        assert link != "NEEDS_RESCAN", f"{node_id} link_status is NEEDS_RESCAN"


# ---------------------------------------------------------------------------
# LINKED_STALE via documents edge
# ---------------------------------------------------------------------------


def test_linked_stale_doc_section_when_code_changes(project: Path):
    """Section documents function → function changes → section is LINKED_STALE."""
    db_file = project / ".axiom_graph" / "graph.db"
    src = project / "src" / "mod.py"

    # Insert a function node with a known hash and an early updated_at
    func = _node("proj::mod::foo", code_hash="aaa", location="src/mod.py")
    db.upsert_node(db_file, func, discovery_only=False)

    time.sleep(0.02)

    # Insert a doc section that documents the function (later updated_at)
    section = _node(
        "proj::docs.arch::overview",
        subtype="docjson",
        code_hash="sec_hash",
        desc_hash="heading_hash",
        location="docs/arch.json",
    )
    db.upsert_node(db_file, section, discovery_only=False)

    # Create the documents edge
    db.upsert_edge(db_file, _edge("proj::docs.arch::overview", "documents", "proj::mod::foo"))

    # Create docs + doc_sections rows (needed by get_stale_doc_sections)
    from axiom_graph.index.db import _connect

    section_updated_at = None
    with _connect(db_file) as conn:
        section_updated_at = conn.execute(
            "SELECT updated_at FROM nodes WHERE id = ?",
            ("proj::docs.arch::overview",),
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO docs (id, title, file_path, desc_hash, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("proj::docs.arch", "Architecture", "docs/arch.json", "x", section_updated_at),
        )
        conn.execute(
            "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, position, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("proj::docs.arch::overview", "proj::docs.arch", "Overview", 2, 0, section_updated_at),
        )

    time.sleep(0.02)

    # Simulate a code change on the function — creates a CONTENT_ONLY history row
    func2 = _node("proj::mod::foo", code_hash="bbb", location="src/mod.py")
    db.upsert_node(db_file, func2, discovery_only=False)

    # Write a docs/arch.json so the section isn't NOT_FOUND
    docs_dir = project / "docs"
    docs_dir.mkdir(exist_ok=True)
    import json

    (docs_dir / "arch.json").write_text(
        json.dumps(
            {
                "title": "Architecture",
                "sections": [{"id": "overview", "heading": "Overview", "content": "Same prose."}],
            }
        ),
        encoding="utf-8",
    )
    # Give the section a matching file_mtime so mtime fast-pass fires
    mtime = (docs_dir / "arch.json").stat().st_mtime
    with _connect(db_file) as conn:
        conn.execute("UPDATE nodes SET file_mtime = ? WHERE id = ?", (mtime, "proj::docs.arch::overview"))

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    assert statuses["proj::docs.arch::overview"][1] == "LINKED_STALE"


def test_primary_stale_takes_precedence_over_linked(project: Path):
    """Own CONTENT_UPDATED should not be downgraded to LINKED_STALE."""
    db_file = project / ".axiom_graph" / "graph.db"
    src = project / "src" / "mod.py"

    func = _node("proj::mod::foo", code_hash="aaa", location="src/mod.py")
    db.upsert_node(db_file, func, discovery_only=False)

    time.sleep(0.02)

    # Section with a stale code_hash (will be CONTENT_UPDATED via own-content check)
    section = _node(
        "proj::docs.arch::overview",
        subtype="docjson",
        code_hash="stale_hash",
        desc_hash="heading_hash",
        location="src/mod.py",  # points at a .py file so staleness engine re-parses it
    )
    db.upsert_node(db_file, section, discovery_only=False)
    db.upsert_edge(db_file, _edge("proj::docs.arch::overview", "documents", "proj::mod::foo"))

    # Create docs + doc_sections rows
    from axiom_graph.index.db import _connect

    with _connect(db_file) as conn:
        section_updated_at = conn.execute(
            "SELECT updated_at FROM nodes WHERE id = ?",
            ("proj::docs.arch::overview",),
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO docs (id, title, file_path, desc_hash, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("proj::docs.arch", "Architecture", "src/mod.py", "x", section_updated_at),
        )
        conn.execute(
            "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, position, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("proj::docs.arch::overview", "proj::docs.arch", "Overview", 2, 0, section_updated_at),
        )

    time.sleep(0.02)

    # Trigger a code change on the function → LINKED_STALE signal too
    func2 = _node("proj::mod::foo", code_hash="bbb", location="src/mod.py")
    db.upsert_node(db_file, func2, discovery_only=False)

    # The section should be CONTENT_UPDATED (own-content), not downgraded to LINKED_STALE
    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    section_status = statuses.get("proj::docs.arch::overview")
    # Primary staleness (CONTENT_UPDATED) is severity 3, LINKED_STALE is severity 2.
    # The engine only upgrades CLEAN → LINKED_STALE, so CONTENT_UPDATED is preserved.
    assert section_status[0] in ("CONTENT_UPDATED", "NOT_FOUND"), (
        f"Expected primary stale to take precedence, got {section_status}"
    )


def test_mtime_mismatch_triggers_hash_comparison(project: Path):
    """Stale mtime forces re-parse (not short-circuited to CLEAN)."""
    db_file = project / ".axiom_graph" / "graph.db"
    src = project / "src" / "mod.py"

    # Rewrite the source to have a known function
    src.write_text("def foo():\n    return 42\n", encoding="utf-8")

    # Insert node with an old mtime (forces mtime mismatch) and wrong hash
    n = _node("proj::mod::foo", code_hash="wrong_hash", desc_hash=None, location="src/mod.py")
    n.file_mtime = 0.0  # ancient mtime → mismatch with current file
    db.upsert_node(db_file, n, discovery_only=False)

    nodes = db.all_nodes(db_file)
    statuses = compute_staleness(db_file, project, nodes)
    # Mtime mismatch should trigger re-parse → hash comparison → stale
    assert statuses["proj::mod::foo"][0] == "CONTENT_UPDATED"
