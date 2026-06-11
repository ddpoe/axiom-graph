"""Doc section own-content staleness through compute_staleness.

Tier 3 flagship + Tier 1/Tier 2 tests. The ADR-009 headline feature:
doc sections (atomic_process/subtype=docjson) gain primary staleness
detection via code_hash and desc_hash (both = content_hash for sections;
heading-only edits surface at the file-level composite, not the section
atomic — see pev-instance-2026-05-16-docjson-section-desc-updated-phantom-cascade).

Covers:
- Doc section prose change → CONTENT_UPDATED, parent inherits
- Heading-only change → section VERIFIED, file CONTENT_UPDATED (no phantom cascade)
- Both changed → CONTENT_UPDATED
- Unchanged doc → CLEAN
- Removed section → NOT_FOUND
- MCP axiom_graph_update_section → check path (Tier 2)
"""

from __future__ import annotations

import json
from pathlib import Path


from axiom_annotations import workflow, Step

from axiom_graph.index import builder, db
from axiom_graph.index.staleness import compute_staleness


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


# ---------------------------------------------------------------------------
# Tier 3 — Doc section prose change → CONTENT_UPDATED, parent inherits
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Verify that editing a doc section's prose body is detected as "
        "CONTENT_UPDATED and that the parent composite doc inherits the status."
    ),
)
def test_doc_section_prose_change_content_stale(mini_project: Path, db_path: Path):
    docs_dir = mini_project / "docs"

    口 = Step(
        step_num=1,
        name="Write initial DocJSON file",
        purpose="Create a doc with one section to establish the hash baseline",
        outputs="arch.json with 'overview' section",
    )
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "Overview", "content": "Initial prose content."},
        ],
    )

    口 = Step(
        step_num=2,
        name="Run initial full build",
        purpose="Scan and upsert with discovery_only=False to set code_hash baseline for the section",
    )
    result = _build_full(mini_project)
    assert result["nodes_written"] > 0

    口 = Step(
        step_num=3,
        name="Edit section prose body",
        purpose="Change the section content but keep the heading identical — triggers CONTENT_UPDATED",
    )
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "Overview", "content": "Completely rewritten prose."},
        ],
    )

    口 = Step(
        step_num=4,
        name="Run discovery-only build",
        purpose="Re-scan — existing section hashes preserved in DB, file on disk has changed",
    )
    _build_discovery(mini_project)

    口 = Step(
        step_num=5,
        name="Compute staleness and assert CONTENT_UPDATED",
        purpose="Section's stored code_hash (prose body) differs from current file — CONTENT_UPDATED",
        outputs="Section node CONTENT_UPDATED, parent doc node inherits CONTENT_UPDATED",
    )
    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    section_id = "proj::docs.arch::overview"
    doc_id = "proj::docs.arch"
    assert statuses.get(section_id)[0] == "CONTENT_UPDATED", (
        f"Expected section CONTENT_UPDATED, got {statuses.get(section_id)}"
    )
    assert statuses.get(doc_id)[0] == "CONTENT_UPDATED", (
        f"Expected parent doc to inherit CONTENT_UPDATED, got {statuses.get(doc_id)}"
    )


# ---------------------------------------------------------------------------
# Tier 1 — Heading-only change → section VERIFIED, file CONTENT_UPDATED
# ---------------------------------------------------------------------------


def test_doc_section_heading_change_no_section_drift(mini_project: Path, db_path: Path):
    """Heading-only change → section atomic VERIFIED, file composite CONTENT_UPDATED.

    Section atomic ``desc_hash`` is ``content_hash`` (matches the shadow row's
    ``desc_hash``), so heading-only edits do not flip the section atomic.
    The signal still surfaces at the file-level composite because file bytes
    changed.  Prevents the phantom DESC_UPDATED cascade onto sibling sections.
    """
    docs_dir = mini_project / "docs"
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "Overview", "content": "Some prose."},
        ],
    )
    _build_full(mini_project)

    # Change only the heading
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "Architecture Overview", "content": "Some prose."},
        ],
    )
    _build_discovery(mini_project)

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    section_status = statuses.get("proj::docs.arch::overview")
    assert section_status[0] == "VERIFIED", f"Expected section VERIFIED after heading-only edit, got {section_status}"
    assert statuses.get("proj::docs.arch")[0] == "CONTENT_UPDATED", (
        "Expected file composite CONTENT_UPDATED because file bytes changed"
    )


# ---------------------------------------------------------------------------
# Tier 2 — Sibling immunity: heading edit on one section leaves siblings VERIFIED
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Verify that editing one section's heading in a multi-section DocJSON file "
        "leaves all sibling sections VERIFIED — no phantom DESC_UPDATED cascade "
        "(regression for pev-instance-2026-05-16-docjson-section-desc-updated-phantom-cascade)."
    ),
)
def test_doc_section_heading_edit_does_not_cascade_to_siblings(mini_project: Path, db_path: Path):
    docs_dir = mini_project / "docs"
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "alpha", "heading": "Alpha", "content": "Alpha prose."},
            {"id": "beta", "heading": "Beta", "content": "Beta prose."},
            {"id": "gamma", "heading": "Gamma", "content": "Gamma prose."},
            {"id": "delta", "heading": "Delta", "content": "Delta prose."},
        ],
    )
    _build_full(mini_project)

    # Edit only the 'beta' heading.  Body unchanged.  Other sections untouched.
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "alpha", "heading": "Alpha", "content": "Alpha prose."},
            {"id": "beta", "heading": "Beta!", "content": "Beta prose."},
            {"id": "gamma", "heading": "Gamma", "content": "Gamma prose."},
            {"id": "delta", "heading": "Delta", "content": "Delta prose."},
        ],
    )
    _build_discovery(mini_project)

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    # Every section (including the heading-edited one) stays VERIFIED at the atomic level.
    for sec_id in ("alpha", "beta", "gamma", "delta"):
        full_id = f"proj::docs.arch::{sec_id}"
        assert statuses.get(full_id)[0] == "VERIFIED", (
            f"Section {full_id} expected VERIFIED, got {statuses.get(full_id)}"
        )

    # Drift is reported at the file composite level only.
    assert statuses.get("proj::docs.arch")[0] == "CONTENT_UPDATED"


# ---------------------------------------------------------------------------
# Tier 2 — Body edit on one section: only that section CONTENT_UPDATED
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Verify that editing one section's content body marks only that section "
        "CONTENT_UPDATED — siblings stay VERIFIED (regression for "
        "pev-instance-2026-05-16-docjson-section-desc-updated-phantom-cascade)."
    ),
)
def test_doc_section_body_edit_does_not_cascade_to_siblings(mini_project: Path, db_path: Path):
    docs_dir = mini_project / "docs"
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "alpha", "heading": "Alpha", "content": "Alpha prose."},
            {"id": "beta", "heading": "Beta", "content": "Beta prose."},
            {"id": "gamma", "heading": "Gamma", "content": "Gamma prose."},
        ],
    )
    _build_full(mini_project)

    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "alpha", "heading": "Alpha", "content": "Alpha prose."},
            {"id": "beta", "heading": "Beta", "content": "Beta prose REWRITTEN."},
            {"id": "gamma", "heading": "Gamma", "content": "Gamma prose."},
        ],
    )
    _build_discovery(mini_project)

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    assert statuses.get("proj::docs.arch::beta")[0] == "CONTENT_UPDATED"
    assert statuses.get("proj::docs.arch::alpha")[0] == "VERIFIED"
    assert statuses.get("proj::docs.arch::gamma")[0] == "VERIFIED"


# ---------------------------------------------------------------------------
# Tier 1 — Both changed → CONTENT_UPDATED
# ---------------------------------------------------------------------------


def test_doc_section_both_changed_content_stale(mini_project: Path, db_path: Path):
    """Both heading and prose changed → CONTENT_UPDATED (higher severity)."""
    docs_dir = mini_project / "docs"
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "Overview", "content": "Original prose."},
        ],
    )
    _build_full(mini_project)

    # Change both heading AND content
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "New Heading", "content": "Rewritten prose."},
        ],
    )
    _build_discovery(mini_project)

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    assert statuses.get("proj::docs.arch::overview")[0] == "CONTENT_UPDATED"


# ---------------------------------------------------------------------------
# Tier 1 — Unchanged doc → CLEAN
# ---------------------------------------------------------------------------


def test_doc_section_unchanged_clean(mini_project: Path, db_path: Path):
    """Re-scan an identical doc file → section stays CLEAN."""
    docs_dir = mini_project / "docs"
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "Overview", "content": "Stable prose."},
        ],
    )
    _build_full(mini_project)

    # Re-build without changing anything (discovery_only skips via mtime,
    # but staleness check should still see CLEAN)
    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    assert statuses.get("proj::docs.arch::overview") == ("VERIFIED", "VERIFIED", [])


# ---------------------------------------------------------------------------
# Tier 1 — Removed section → NOT_FOUND
# ---------------------------------------------------------------------------


def test_doc_section_removed_structural_drift(mini_project: Path, db_path: Path):
    """Remove a section from the JSON → NOT_FOUND."""
    docs_dir = mini_project / "docs"
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "Overview", "content": "Some prose."},
            {"id": "details", "heading": "Details", "content": "Detail prose."},
        ],
    )
    _build_full(mini_project)

    # Remove the 'details' section
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "Overview", "content": "Some prose."},
        ],
    )
    _build_discovery(mini_project)

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    assert statuses.get("proj::docs.arch::details")[0] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Tier 2 — axiom_graph_update_section → check path
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "Verify that updating a doc section via MCP axiom_graph_update_section "
        "and then running axiom_graph_check detects CONTENT_UPDATED."
    ),
)
def test_update_section_then_check_content_stale(mini_project: Path, db_path: Path):
    """Exercises the MCP axiom_graph_update_section → check path."""
    docs_dir = mini_project / "docs"
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "Overview", "content": "Original prose."},
        ],
    )
    _build_full(mini_project)

    # Simulate what axiom_graph_update_section does: rewrite the section content
    # directly in the JSON file.
    doc_path = docs_dir / "arch.json"
    data = json.loads(doc_path.read_text(encoding="utf-8"))
    data["sections"][0]["content"] = "Updated via axiom_graph_update_section."
    doc_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Run discovery build (what axiom_graph_build does by default)
    _build_discovery(mini_project)

    # Compute staleness (what axiom_graph_check does)
    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    assert statuses.get("proj::docs.arch::overview")[0] == "CONTENT_UPDATED", (
        f"Expected CONTENT_UPDATED after section update, got {statuses.get('proj::docs.arch::overview')}"
    )
