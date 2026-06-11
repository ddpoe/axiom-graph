"""Regression: ``build_index`` must honor ``config.staleness.frozen_tags``.

The frozen-tag machinery is implemented in ``_get_linked_stale_ids`` /
``compute_staleness`` and is exercised by ``tests/test_transitive_linked_stale.py``
and ``tests/test_drift_query.py`` -- but those call the staleness engine (or the
read-time check/drift_query layers) directly with ``frozen_tags=`` supplied.

None of them drive ``build_index``, which is the authoritative path that
*persists* ``nodes.link_status``.  ``build_index`` (axiom_graph/lifecycle/api.py)
historically threaded ``transitive_tags`` but dropped ``frozen_tags``, so every
real ``axiom-graph build`` / ``axiom_graph_build`` wrote ``LINKED_STALE`` onto
frozen ADR/plan/PEV docs exactly as if ``frozen_tags`` were empty.

This test closes that gap: a frozen-tagged DocJSON section linked to drifted
code must persist ``link_status = VERIFIED`` after ``build_index``.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom_graph.db.staleness import get_stale_doc_sections
from axiom_graph.index import db
from axiom_graph.lifecycle.api import build_index


def _write_docjson(path: Path, *, tags: list[str], links: list[dict]) -> None:
    """Write a one-section DocJSON file with the given doc-level tags and links."""
    path.write_text(
        json.dumps(
            {
                "title": "ADR-001",
                "tags": tags,
                "sections": [
                    {
                        "id": "ctx",
                        "heading": "Context",
                        "content": "Context body.",
                        "links": links,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_build_index_honors_frozen_tags_for_linked_stale(mini_project: Path, db_path: Path):
    """A frozen-tagged section linked to drifted code stays VERIFIED after build_index."""
    root = mini_project
    (root / "mod.py").write_text("def fn():\n    return 1\n", encoding="utf-8")

    docs_dir = root / "docs"
    docs_dir.mkdir()
    doc_file = docs_dir / "adr-001.json"
    _write_docjson(doc_file, tags=["adr"], links=[])

    (root / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.staleness]\nfrozen_tags = ["adr"]\n',
        encoding="utf-8",
    )

    # --- Build 1: discover the real code-node id the module scanner produces ---
    build_index(db_path, root, project_id="proj")
    code_ids = [n.id for n in db.all_nodes(db_path) if n.location == "mod.py" and n.node_type == "atomic_process"]
    assert code_ids, "expected a code node for mod.py after build"
    code_id = code_ids[0]

    # --- Build 2: link the frozen ADR section to that code node ---
    _write_docjson(doc_file, tags=["adr"], links=[{"node_id": code_id}])
    build_index(db_path, root, project_id="proj")

    section_id = "proj::docs.adr-001::ctx"
    documents_edges = [
        e
        for e in db.all_edges(db_path)
        if e.edge_type == "documents" and e.from_id == section_id and e.to_id == code_id
    ]
    assert documents_edges, "documents edge (section -> code) must exist; check node-id / link format"

    # --- Arm Pass 1: record a code-content change on the linked code node ---
    with db._connect(db_path) as conn:
        conn.execute(
            "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (code_id, db._now_utc(), "CONTENT_ONLY", "abc", "{}", 0),
        )

    # --- Build 3: compute + persist staleness with the change in place ---
    build_index(db_path, root, project_id="proj")

    # Guard: the LINKED_STALE mechanism is genuinely armed (Pass 1 inventory
    # returns this section).  get_stale_doc_sections is frozen-agnostic, so this
    # holds under both the bug and the fix -- it proves the section WOULD be
    # LINKED_STALE if frozen_tags were ignored.
    stale_section_ids = {r["section_id"] for r in get_stale_doc_sections(db_path)}
    assert section_id in stale_section_ids, (
        "test setup: frozen section's linked code must appear stale in Pass 1 inventory"
    )

    # The behavior under test: build_index must honor frozen_tags, so the frozen
    # ADR section is NOT persisted as LINKED_STALE.
    with db._connect(db_path) as conn:
        sec_row = conn.execute("SELECT link_status FROM nodes WHERE id = ?", (section_id,)).fetchone()
    assert sec_row is not None, "section node must exist in the index"
    assert sec_row["link_status"] == "VERIFIED", (
        f"frozen ADR section should not be LINKED_STALE after build_index, "
        f"got link_status={sec_row['link_status']!r} -- build_index is ignoring "
        f"config.staleness.frozen_tags"
    )
