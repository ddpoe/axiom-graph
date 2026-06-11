"""Tests for ADR-010 nested DocJSON sections (Layers 1–5).

Covers:
- Scanner: recursive walk, dot-path IDs, composes edges, depth enforcement
- Schema: parent_id and depth columns in doc_sections
- Staleness: atomic_process parents get LINKED_STALE, composite parents worst-child
- MCP: axiom_graph_update_section dot-path traversal, axiom_graph_add_section, depth validation
- Viz: render_doc_api includes parent_id and depth
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom_annotations import workflow, Step

from axiom_graph.index import builder, db
from axiom_graph.index.staleness import compute_staleness
from axiom_graph.docjson import parse as json_doc_scanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_doc(docs_dir: Path, filename: str, title: str, sections: list[dict]) -> Path:
    """Write a DocJSON file and return its path."""
    docs_dir.mkdir(exist_ok=True)
    doc_path = docs_dir / filename
    doc_path.write_text(
        json.dumps({"title": title, "sections": sections}, indent=2),
        encoding="utf-8",
    )
    return doc_path


def _build_full(project_root: Path, project_id: str = "proj") -> dict:
    return builder.build(project_root, project_id=project_id, discovery_only=False)


def _build_discovery(project_root: Path, project_id: str = "proj") -> dict:
    return builder.build(project_root, project_id=project_id, discovery_only=True)


NESTED_DOC = {
    "title": "Architecture",
    "sections": [
        {
            "id": "database-layer",
            "heading": "Database Layer",
            "content": "Overview of the DB.",
            "sections": [
                {"id": "tables", "heading": "Tables", "content": "Table details."},
                {"id": "migrations", "heading": "Migrations", "content": "Migration info."},
            ],
        },
        {"id": "api-layer", "heading": "API Layer", "content": "REST endpoints."},
    ],
}


# ---------------------------------------------------------------------------
# Layer 2 — Scanner: recursive walk, dot-path IDs, composes edges
# ---------------------------------------------------------------------------


def test_scanner_nested_sections_dot_path_ids(tmp_path):
    """Nested sections get dot-path IDs: doc::parent.child."""
    f = tmp_path / "docs" / "arch.json"
    f.parent.mkdir()
    f.write_text(json.dumps(NESTED_DOC))

    nodes, _, _, _ = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    ids = {n.id for n in nodes}
    assert "proj::docs.arch::database-layer" in ids
    assert "proj::docs.arch::database-layer.tables" in ids
    assert "proj::docs.arch::database-layer.migrations" in ids
    assert "proj::docs.arch::api-layer" in ids


def test_scanner_nested_composes_edges(tmp_path):
    """Scanner emits composes edges: doc→parent, parent→child."""
    f = tmp_path / "docs" / "arch.json"
    f.parent.mkdir()
    f.write_text(json.dumps(NESTED_DOC))

    _, edges, _, _ = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    composes = [(e.from_id, e.to_id) for e in edges if e.edge_type == "composes"]
    # doc → top-level sections
    assert ("proj::docs.arch", "proj::docs.arch::database-layer") in composes
    assert ("proj::docs.arch", "proj::docs.arch::api-layer") in composes
    # parent section → child sections
    assert ("proj::docs.arch::database-layer", "proj::docs.arch::database-layer.tables") in composes
    assert ("proj::docs.arch::database-layer", "proj::docs.arch::database-layer.migrations") in composes


def test_scanner_nested_sec_recs_parent_id_and_depth(tmp_path):
    """Section records include correct parent_id and depth."""
    f = tmp_path / "docs" / "arch.json"
    f.parent.mkdir()
    f.write_text(json.dumps(NESTED_DOC))

    _, _, _, sec_recs = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    by_id = {r["id"]: r for r in sec_recs}

    parent = by_id["proj::docs.arch::database-layer"]
    assert parent["parent_id"] is None
    assert parent["depth"] == 0

    child = by_id["proj::docs.arch::database-layer.tables"]
    assert child["parent_id"] == "proj::docs.arch::database-layer"
    assert child["depth"] == 1


def test_scanner_position_scoped_to_siblings(tmp_path):
    """Position is scoped to siblings within the same parent."""
    f = tmp_path / "docs" / "arch.json"
    f.parent.mkdir()
    f.write_text(json.dumps(NESTED_DOC))

    _, _, _, sec_recs = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    by_id = {r["id"]: r for r in sec_recs}

    # Top-level positions
    assert by_id["proj::docs.arch::database-layer"]["position"] == 0
    assert by_id["proj::docs.arch::api-layer"]["position"] == 1

    # Child positions (scoped to parent)
    assert by_id["proj::docs.arch::database-layer.tables"]["position"] == 0
    assert by_id["proj::docs.arch::database-layer.migrations"]["position"] == 1


def test_scanner_depth_limit_warns_and_skips(tmp_path):
    """Sections beyond max depth (3 levels) are skipped with a warning."""
    deep_doc = {
        "title": "Deep Doc",
        "sections": [
            {
                "id": "l0",
                "heading": "L0",
                "content": "",
                "sections": [
                    {
                        "id": "l1",
                        "heading": "L1",
                        "content": "",
                        "sections": [
                            {
                                "id": "l2",
                                "heading": "L2",
                                "content": "",
                                "sections": [
                                    {"id": "l3", "heading": "L3 (too deep)", "content": ""},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    f = tmp_path / "docs" / "deep.json"
    f.parent.mkdir()
    f.write_text(json.dumps(deep_doc))

    with pytest.warns(UserWarning, match="exceeds max nesting depth"):
        nodes, _, _, _ = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    ids = {n.id for n in nodes}
    assert "proj::docs.deep::l0.l1.l2" in ids
    assert "proj::docs.deep::l0.l1.l2.l3" not in ids  # skipped


def test_scanner_child_section_node_type_atomic(tmp_path):
    """All section nodes (including nested) are atomic_process."""
    f = tmp_path / "docs" / "arch.json"
    f.parent.mkdir()
    f.write_text(json.dumps(NESTED_DOC))

    nodes, _, _, _ = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    section_nodes = [n for n in nodes if "::" in n.id and n.id.count("::") == 2]
    for n in section_nodes:
        assert n.node_type == "atomic_process"


def test_scanner_heading_level_auto_derives_from_depth(tmp_path):
    """Section level auto-derives from depth when not explicitly set."""
    f = tmp_path / "docs" / "arch.json"
    f.parent.mkdir()
    f.write_text(json.dumps(NESTED_DOC))

    _, _, _, sec_recs = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    by_id = {r["id"]: r for r in sec_recs}
    assert by_id["proj::docs.arch::database-layer"]["level"] == 2  # depth 0 → level 2
    assert by_id["proj::docs.arch::database-layer.tables"]["level"] == 3  # depth 1 → level 3


# ---------------------------------------------------------------------------
# Layer 1+2 — Schema: parent_id and depth columns round-trip through DB
# ---------------------------------------------------------------------------


def test_nested_sections_round_trip_through_db(mini_project: Path, db_path: Path):
    """Nested sections survive build → DB read with parent_id and depth."""
    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    sections = db.get_doc_sections(db_path, "proj::docs.arch")
    by_id = {s["id"]: s for s in sections}

    parent = by_id["proj::docs.arch::database-layer"]
    assert parent["parent_id"] is None
    assert parent["depth"] == 0

    child = by_id["proj::docs.arch::database-layer.tables"]
    assert child["parent_id"] == "proj::docs.arch::database-layer"
    assert child["depth"] == 1


def test_get_doc_sections_returns_depth_first_order(mini_project: Path, db_path: Path):
    """get_doc_sections returns sections in depth-first order."""
    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    sections = db.get_doc_sections(db_path, "proj::docs.arch")
    ids = [s["id"] for s in sections]

    # Depth-first: parent, then children, then next sibling
    assert ids == [
        "proj::docs.arch::database-layer",
        "proj::docs.arch::database-layer.tables",
        "proj::docs.arch::database-layer.migrations",
        "proj::docs.arch::api-layer",
    ]


# ---------------------------------------------------------------------------
# Layer 3 — Staleness: atomic parent → LINKED_STALE
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Verify that editing a nested child section's content marks it "
        "CONTENT_UPDATED and its parent section gets LINKED_STALE (not "
        "worst-child inheritance), while the doc file gets composite "
        "inheritance."
    ),
)
def test_nested_child_stale_parent_linked_stale(mini_project: Path, db_path: Path):
    docs_dir = mini_project / "docs"

    口 = Step(
        step_num=1,
        name="Write nested doc and build baseline",
        purpose="Create a doc with nested sections to establish hash baselines",
    )
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    口 = Step(
        step_num=2,
        name="Edit child section content",
        purpose="Change a nested child's content to trigger CONTENT_UPDATED",
    )
    doc_path = docs_dir / "arch.json"
    data = json.loads(doc_path.read_text(encoding="utf-8"))
    data["sections"][0]["sections"][0]["content"] = "Completely rewritten tables."
    doc_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    口 = Step(
        step_num=3,
        name="Discovery build + compute staleness",
        purpose="Re-scan and check staleness signals propagate correctly",
    )
    _build_discovery(mini_project)
    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    口 = Step(
        step_num=4,
        name="Assert staleness chain",
        purpose="Child=CONTENT_UPDATED, parent section=LINKED_STALE, doc file=LINKED_STALE",
    )
    assert statuses.get("proj::docs.arch::database-layer.tables")[0] == "CONTENT_UPDATED"
    assert statuses.get("proj::docs.arch::database-layer")[1] == "LINKED_STALE", (
        f"Expected parent section LINKED_STALE, got {statuses.get('proj::docs.arch::database-layer')}"
    )
    doc_status = statuses.get("proj::docs.arch")
    own, link = doc_status[0], doc_status[1]
    assert own in ("CONTENT_UPDATED",) or link in ("LINKED_STALE",), (
        f"Expected doc file to inherit staleness, got {doc_status}"
    )


def test_nested_parent_own_stale_preserved_over_linked(mini_project: Path, db_path: Path):
    """Parent section's own CONTENT_UPDATED is preserved (higher than LINKED_STALE)."""
    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    # Change both parent and child content
    doc_path = docs_dir / "arch.json"
    data = json.loads(doc_path.read_text(encoding="utf-8"))
    data["sections"][0]["content"] = "Rewritten parent overview."
    data["sections"][0]["sections"][0]["content"] = "Rewritten child tables."
    doc_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    _build_discovery(mini_project)
    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    # Parent's own CONTENT_UPDATED (severity 3) > LINKED_STALE (severity 2)
    assert statuses.get("proj::docs.arch::database-layer")[0] == "CONTENT_UPDATED"


def test_nested_all_children_clean_parent_clean(mini_project: Path, db_path: Path):
    """When all children are CLEAN, the parent section stays CLEAN."""
    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    # No changes — everything should be CLEAN
    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    assert statuses.get("proj::docs.arch::database-layer") == ("VERIFIED", "VERIFIED", [])
    assert statuses.get("proj::docs.arch::database-layer.tables") == ("VERIFIED", "VERIFIED", [])


# ---------------------------------------------------------------------------
# Layer 4 — MCP tools
# ---------------------------------------------------------------------------


def test_update_section_dot_path(mini_project: Path, db_path: Path):
    """axiom_graph_update_section works with dot-path section IDs."""
    from axiom_graph.mcp_server import axiom_graph_update_section

    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    result = axiom_graph_update_section(
        str(mini_project),
        "proj::docs.arch::database-layer.tables",
        content="Updated tables content.",
    )
    assert "Updated content" in result

    # Verify the file was actually updated
    data = json.loads((docs_dir / "arch.json").read_text(encoding="utf-8"))
    assert data["sections"][0]["sections"][0]["content"] == "Updated tables content."


def test_add_section_top_level(mini_project: Path, db_path: Path):
    """axiom_graph_add_section adds a top-level section."""
    from axiom_graph.mcp_server import axiom_graph_add_section

    docs_dir = mini_project / "docs"
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "intro", "heading": "Introduction", "content": "Intro."},
        ],
    )
    _build_full(mini_project)

    result = axiom_graph_add_section(
        str(mini_project),
        "proj::docs.arch",
        "new-section",
        "New Section",
        content="New content.",
    )
    assert "Added section" in result

    data = json.loads((docs_dir / "arch.json").read_text(encoding="utf-8"))
    assert len(data["sections"]) == 2
    assert data["sections"][1]["id"] == "new-section"


def test_add_section_nested_under_parent(mini_project: Path, db_path: Path):
    """axiom_graph_add_section adds a child section under a parent."""
    from axiom_graph.mcp_server import axiom_graph_add_section

    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    result = axiom_graph_add_section(
        str(mini_project),
        "proj::docs.arch",
        "indexes",
        "Indexes",
        content="Index details.",
        parent_id="database-layer",
        after="tables",
    )
    assert "Added section" in result

    data = json.loads((docs_dir / "arch.json").read_text(encoding="utf-8"))
    child_ids = [s["id"] for s in data["sections"][0]["sections"]]
    assert child_ids == ["tables", "indexes", "migrations"]


def test_add_section_rejects_excessive_depth(mini_project: Path, db_path: Path):
    """axiom_graph_add_section rejects nesting beyond max depth."""
    from axiom_graph.mcp_server import axiom_graph_add_section

    docs_dir = mini_project / "docs"
    deep_sections = [
        {
            "id": "l0",
            "heading": "L0",
            "content": "",
            "sections": [
                {
                    "id": "l1",
                    "heading": "L1",
                    "content": "",
                    "sections": [
                        {"id": "l2", "heading": "L2", "content": ""},
                    ],
                }
            ],
        }
    ]
    _write_doc(docs_dir, "deep.json", "Deep Doc", deep_sections)
    _build_full(mini_project)

    result = axiom_graph_add_section(
        str(mini_project),
        "proj::docs.deep",
        "l3",
        "L3 Too Deep",
        parent_id="l0.l1.l2",
    )
    assert "ERROR" in result
    assert "depth" in result.lower()


def test_write_doc_rejects_excessive_depth(mini_project: Path, db_path: Path):
    """axiom_graph_write_doc rejects docs with > 3 nesting levels."""
    from axiom_graph.mcp_server import axiom_graph_write_doc

    deep_doc = {
        "title": "Too Deep",
        "sections": [
            {
                "id": "l0",
                "heading": "L0",
                "sections": [
                    {
                        "id": "l1",
                        "heading": "L1",
                        "sections": [
                            {
                                "id": "l2",
                                "heading": "L2",
                                "sections": [{"id": "l3", "heading": "L3"}],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    result = axiom_graph_write_doc(str(mini_project), json.dumps(deep_doc))
    assert "ERROR" in result
    assert "depth" in result.lower()


def test_add_link_dot_path(mini_project: Path, db_path: Path):
    """axiom_graph_add_link works with dot-path section IDs."""
    from axiom_graph.mcp_server import axiom_graph_add_link

    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    # Create a dummy code node to link to
    dummy_node = json_doc_scanner.AxiomNode(
        id="proj::some.module::func",
        node_type="atomic_process",
        subtype=None,
        title="func",
        location="some/module.py",
        source="test",
        code_hash="abc123",
        desc_hash="def456",
        level_0="module",
        level_1="func",
    )
    db.upsert_node(db_path, dummy_node)

    result = axiom_graph_add_link(
        str(mini_project),
        "proj::docs.arch::database-layer.tables",
        "proj::some.module::func",
    )
    assert "Added 1 link" in result

    # Verify the link was written to JSON
    data = json.loads((docs_dir / "arch.json").read_text(encoding="utf-8"))
    child_links = data["sections"][0]["sections"][0].get("links", [])
    assert any(lk["node_id"] == "proj::some.module::func" for lk in child_links)


# ---------------------------------------------------------------------------
# Layer 5 — Viz backend
# ---------------------------------------------------------------------------


def test_render_doc_api_includes_parent_id_and_depth(mini_project: Path, db_path: Path):
    """render_doc_api structured sections include parent_id and depth."""
    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    from axiom_graph.viz.server import render_doc_api

    # Patch _db to use our test db
    import axiom_graph.viz.server as viz_mod

    original_db = viz_mod._db
    viz_mod._db = lambda: db_path
    try:
        result = render_doc_api("proj::docs.arch")
    finally:
        viz_mod._db = original_db

    secs_by_id = {s["id"]: s for s in result["sections"]}

    parent = secs_by_id["proj::docs.arch::database-layer"]
    assert parent["parent_id"] is None
    assert parent["depth"] == 0

    child = secs_by_id["proj::docs.arch::database-layer.tables"]
    assert child["parent_id"] == "proj::docs.arch::database-layer"
    assert child["depth"] == 1


# ---------------------------------------------------------------------------
# Backward compatibility — flat docs still work
# ---------------------------------------------------------------------------


def test_flat_doc_unchanged_behavior(tmp_path):
    """Existing flat docs (no nested sections) work identically."""
    flat_doc = {
        "title": "Flat Doc",
        "sections": [
            {"id": "intro", "heading": "Intro", "content": "Hello."},
            {"id": "usage", "heading": "Usage", "content": "Use it."},
        ],
    }
    f = tmp_path / "docs" / "flat.json"
    f.parent.mkdir()
    f.write_text(json.dumps(flat_doc))

    nodes, edges, _, sec_recs = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    # Same IDs as before
    ids = {n.id for n in nodes}
    assert "proj::docs.flat::intro" in ids
    assert "proj::docs.flat::usage" in ids

    # All top-level: parent_id=None, depth=0
    for rec in sec_recs:
        assert rec["parent_id"] is None
        assert rec["depth"] == 0


# ---------------------------------------------------------------------------
# Rename section ID (new_id parameter on axiom_graph_update_section)
# ---------------------------------------------------------------------------


def test_rename_section_updates_file_and_db(mini_project: Path, db_path: Path):
    """axiom_graph_update_section with new_id renames the section on disk and in the DB."""
    from axiom_graph.mcp_server import axiom_graph_update_section

    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    result = axiom_graph_update_section(
        str(mini_project),
        "proj::docs.arch::api-layer",
        new_id="rest-api",
    )
    assert "id (api-layer → rest-api)" in result

    # Verify JSON on disk
    data = json.loads((docs_dir / "arch.json").read_text(encoding="utf-8"))
    top_ids = [s["id"] for s in data["sections"]]
    assert "rest-api" in top_ids
    assert "api-layer" not in top_ids

    # Verify DB section was renamed
    secs = db.get_doc_sections(db_path, "proj::docs.arch")
    sec_ids = {s["id"] for s in secs}
    assert "proj::docs.arch::rest-api" in sec_ids
    assert "proj::docs.arch::api-layer" not in sec_ids


def test_rename_section_rejects_invalid_id_and_collision(mini_project: Path, db_path: Path):
    """Rename rejects bad format and duplicate sibling IDs."""
    from axiom_graph.mcp_server import axiom_graph_update_section

    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    # Invalid format
    result = axiom_graph_update_section(
        str(mini_project),
        "proj::docs.arch::api-layer",
        new_id="Bad ID!",
    )
    assert "ERROR" in result

    # Collision with existing sibling
    result = axiom_graph_update_section(
        str(mini_project),
        "proj::docs.arch::api-layer",
        new_id="database-layer",
    )
    assert "ERROR" in result
    assert "already exists" in result


def test_rename_parent_cascades_children(mini_project: Path, db_path: Path):
    """Renaming a parent section cascades new IDs to all children."""
    from axiom_graph.mcp_server import axiom_graph_update_section

    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC["sections"])
    _build_full(mini_project)

    result = axiom_graph_update_section(
        str(mini_project),
        "proj::docs.arch::database-layer",
        new_id="db",
    )
    assert "id (database-layer → db)" in result

    # Children should now have db.tables and db.migrations
    secs = db.get_doc_sections(db_path, "proj::docs.arch")
    sec_ids = {s["id"] for s in secs}
    assert "proj::docs.arch::db" in sec_ids
    assert "proj::docs.arch::db.tables" in sec_ids
    assert "proj::docs.arch::db.migrations" in sec_ids
    # Old IDs gone
    assert "proj::docs.arch::database-layer" not in sec_ids
    assert "proj::docs.arch::database-layer.tables" not in sec_ids
