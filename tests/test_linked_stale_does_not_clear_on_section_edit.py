"""LINKED_STALE clearing semantics under the cycle pev-2026-05-02 auto-mark hook.

Originally a regression test for ADR-017 ("LINKED_STALE only cleared by
mark_clean"), with the supplementary load-bearing constraint that editing
a doc section is not the same as verifying it.

Cycle pev-2026-05-02 changed that semantic for explicit-save callers:
``save_and_reindex`` now treats a write that actually changes existing-node
hashes as the verification (writer-is-verifier per decision D-3 of that
cycle).  Concretely, ``axiom_graph_update_section`` on section S calls
``mark_node_clean`` on S, which writes ``node_verification.verified_at``
and consequently clears LINKED_STALE on S itself via Pass 2 of
``_get_linked_stale_ids`` -- NOT a regression, the intended new behaviour.

The cascade invariant is preserved: auto-mark on child C does NOT clear
LINKED_STALE on parent P or peer linked nodes; only S clears.  The
auto-mark hook is gated on hash-change detection, so excluded tools
(``add_link`` / ``add_section`` / first-time ``write_doc``) still leave
LINKED_STALE sticky.

This test now drives ``axiom_graph_update_section`` and asserts the
auto-clear pathway: section was LINKED_STALE before update, is no longer
LINKED_STALE after update.  It MUST drive the docjson API and not the
``_write_doc + _build`` fixture path; going through ``_build_full`` would
set ``discovery_only=False`` and refresh ``nodes.updated_at`` as a side-
effect, masking the auto-mark assertion.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from axiom_annotations import workflow, Step

from axiom_graph.db.staleness import get_stale_doc_sections
from axiom_graph.index import builder
from axiom_graph.index.staleness import _get_linked_stale_ids
from axiom_graph.docjson.api import axiom_graph_update_section
from axiom_graph.lifecycle.mcp_tools import axiom_graph_mark_clean


@workflow(
    purpose=(
        "Auto-mark cycle: editing a doc section via axiom_graph_update_section "
        "writes the writer-is-verifier snapshot through save_and_reindex's "
        "auto-mark hook, which causes _get_linked_stale_ids Pass 2 to remove "
        "the section. Proves the new clearing semantic per cycle "
        "pev-2026-05-02 decision D-3."
    ),
)
def test_linked_stale_clears_after_update_section_via_auto_mark(mini_project: Path, db_path: Path):
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    src_dir = mini_project / "src"
    src_dir.mkdir(exist_ok=True)

    口 = Step(
        step_num=1,
        name="Write code module + DocJSON section linking to it",
        purpose=("Establish a section -> code 'documents' edge so the LINKED_STALE join has something to traverse"),
        outputs="src/mod.py with foo(); docs/spec.json with section linking to it",
    )
    code_path = src_dir / "mod.py"
    code_path.write_text("def foo():\n    return 0\n", encoding="utf-8")

    doc_path = docs_dir / "spec.json"
    doc_path.write_text(
        json.dumps(
            {
                "title": "Spec",
                "sections": [
                    {
                        "id": "overview",
                        "heading": "Overview",
                        "content": "Initial section content.",
                        "links": [{"node_id": "proj::src.mod::foo"}],
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    口 = Step(
        step_num=2,
        name="Initial full build",
        purpose=("Set the staleness baseline for both the code node and the section shadow row in nodes"),
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    口 = Step(
        step_num=3,
        name="Edit code, full build to record CONTENT_ONLY history",
        purpose=(
            "Generate a node_history row for the code node — the precondition for LINKED_STALE on the linked section"
        ),
    )
    time.sleep(0.05)
    code_path.write_text("def foo():\n    return 42\n", encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    section_id = "proj::docs.spec::overview"

    口 = Step(
        step_num=4,
        name="Sanity: confirm section is currently LINKED_STALE",
        purpose=(
            "Without this assertion the test could pass spuriously by failing "
            "to set up the LINKED_STALE state in the first place"
        ),
    )
    stale_before = get_stale_doc_sections(db_path)
    stale_section_ids_before = {row["section_id"] for row in stale_before}
    assert section_id in stale_section_ids_before, (
        f"Test setup failed: expected {section_id} to be LINKED_STALE before "
        f"update_section call. Got: {stale_section_ids_before}"
    )
    ls_map_before = _get_linked_stale_ids(db_path)
    assert section_id in ls_map_before, (
        f"Test setup failed: expected {section_id} in _get_linked_stale_ids "
        f"before any clear attempt. Got: {sorted(ls_map_before)}"
    )

    口 = Step(
        step_num=5,
        name="Call axiom_graph_update_section through the production MCP path",
        purpose=(
            "Goes through save_and_reindex which now (cycle pev-2026-05-02) "
            "snapshots pre-state hashes, runs the rescan/upsert, and emits "
            "AGENT_VERIFIED via mark_node_clean for every existing-node hash "
            "that changed. Writing new content to a section IS the "
            "verification; node_verification.verified_at gets written."
        ),
    )
    time.sleep(0.05)
    result = axiom_graph_update_section(
        project_root=str(mini_project),
        section_id=section_id,
        content="Section content updated to acknowledge code change.",
    )
    assert "ERROR" not in result, f"update_section failed: {result}"

    口 = Step(
        step_num=6,
        name="Assert LINKED_STALE clears on the edited section after auto-mark",
        purpose=(
            "Per cycle pev-2026-05-02 decision D-3, save_and_reindex's auto-mark "
            "hook fires when an existing-node hash changes -- the writer is the "
            "verifier. mark_node_clean writes node_verification.verified_at, "
            "which Pass 2 of _get_linked_stale_ids reads to filter out the "
            "section. get_stale_doc_sections (Pass 1, verification-agnostic) "
            "still reports the section; the clearing happens at Pass 2."
        ),
        outputs="section_id absent from _get_linked_stale_ids; still present in get_stale_doc_sections (Pass 1)",
    )
    stale_after_edit = get_stale_doc_sections(db_path)
    stale_section_ids_after_edit = {row["section_id"] for row in stale_after_edit}
    assert section_id in stale_section_ids_after_edit, (
        f"Pass-1 invariant violated: {section_id} dropped from "
        f"get_stale_doc_sections after update_section. The raw Pass 1 query is "
        f"verification-agnostic; clearing belongs to Pass 2 of "
        f"_get_linked_stale_ids. Stale sections: {stale_section_ids_after_edit}"
    )
    ls_map_after_edit = _get_linked_stale_ids(db_path)
    assert section_id not in ls_map_after_edit, (
        f"Auto-mark regression: {section_id} still in _get_linked_stale_ids "
        f"after update_section. The save_and_reindex auto-mark hook should "
        f"have written a node_verification row that Pass 2 reads. "
        f"LS map: {sorted(ls_map_after_edit)}"
    )

    口 = Step(
        step_num=7,
        name="A subsequent explicit mark_clean(human) is still a meaningful no-op",
        purpose=(
            "After auto-mark, the section is already cleared from Pass 2. "
            "Calling mark_clean with verified_by='human' adds another "
            "verification snapshot (MANUAL_VERIFIED in history) but does "
            "not unbreak the section any further -- it stays cleared, "
            "and the human stamp remains distinguishable in history."
        ),
        outputs="section_id still absent from _get_linked_stale_ids; mark_clean returns success",
    )
    time.sleep(0.05)
    mark_result = axiom_graph_mark_clean(
        project_root=str(mini_project),
        node_id=section_id,
        reason="Verified prose still matches updated foo() implementation.",
    )
    assert "ERROR" not in mark_result, f"mark_clean failed: {mark_result}"

    ls_map_after_clean = _get_linked_stale_ids(db_path)
    assert section_id not in ls_map_after_clean, (
        f"Post-human-mark regression: {section_id} reappeared in "
        f"_get_linked_stale_ids. LS map: {sorted(ls_map_after_clean)}"
    )


@workflow(
    purpose=(
        "Cascade safety (US-5 / D-3): editing one LINKED_STALE section via "
        "axiom_graph_update_section auto-marks ONLY that section. Other "
        "linked nodes that were also LINKED_STALE before the edit (parent / "
        "sibling sections that documents the same drifted code) MUST stay "
        "LINKED_STALE because save_and_reindex's auto-mark hook only writes "
        "a node_verification row for nodes whose own hash actually changed. "
        "Proves ADR-018's cascade-correctness invariant survives the "
        "writer-is-verifier rewrite from cycle pev-2026-05-02."
    ),
)
def test_linked_stale_cascade_parent_persists_after_child_auto_mark(mini_project: Path, db_path: Path):
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    src_dir = mini_project / "src"
    src_dir.mkdir(exist_ok=True)

    口 = Step(
        step_num=1,
        name="Write code module + DocJSON with two sections both linking to it",
        purpose=(
            "Establishes two independent section -> code 'documents' edges so "
            "Pass 1 of _get_linked_stale_ids will flag BOTH sections as "
            "LINKED_STALE when the linked code drifts. P plays the parent / "
            "other-linked-node role; C plays the saved-section role."
        ),
        outputs="src/mod.py with foo(); docs/spec.json with sections P (parent) and C (child) both linking foo",
    )
    code_path = src_dir / "mod.py"
    code_path.write_text("def foo():\n    return 0\n", encoding="utf-8")

    doc_path = docs_dir / "spec.json"
    doc_path.write_text(
        json.dumps(
            {
                "title": "Spec",
                "sections": [
                    {
                        "id": "parent",
                        "heading": "Parent",
                        "content": "Parent section content (also documents foo).",
                        "links": [{"node_id": "proj::src.mod::foo"}],
                    },
                    {
                        "id": "child",
                        "heading": "Child",
                        "content": "Child section content (also documents foo).",
                        "links": [{"node_id": "proj::src.mod::foo"}],
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    口 = Step(
        step_num=2,
        name="Initial full build",
        purpose=("Establish baseline node_history rows + shadow rows for both sections and the code node"),
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    口 = Step(
        step_num=3,
        name="Drift the code, full build to record CONTENT_ONLY history on foo()",
        purpose=(
            "Generates a node_history row for the code node — the precondition "
            "that makes BOTH P and C LINKED_STALE in Pass 1 of "
            "_get_linked_stale_ids."
        ),
    )
    time.sleep(0.05)
    code_path.write_text("def foo():\n    return 42\n", encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    parent_id = "proj::docs.spec::parent"
    child_id = "proj::docs.spec::child"

    口 = Step(
        step_num=4,
        name="Sanity: confirm BOTH sections are currently LINKED_STALE",
        purpose=(
            "Without this assertion the test could pass spuriously by failing "
            "to set up the cascade precondition (both nodes stale) in the "
            "first place. The whole point of the cascade test is the "
            "differential outcome between P and C after editing only C."
        ),
    )
    ls_map_before = _get_linked_stale_ids(db_path)
    assert parent_id in ls_map_before, (
        f"Test setup failed: expected {parent_id} in _get_linked_stale_ids "
        f"before the child edit. Got: {sorted(ls_map_before)}"
    )
    assert child_id in ls_map_before, (
        f"Test setup failed: expected {child_id} in _get_linked_stale_ids "
        f"before the child edit. Got: {sorted(ls_map_before)}"
    )

    口 = Step(
        step_num=5,
        name="Edit ONLY the child section via axiom_graph_update_section",
        purpose=(
            "Drives the writer-is-verifier path on C alone. save_and_reindex "
            "snapshots pre-state hashes, runs the rescan/upsert, and emits "
            "AGENT_VERIFIED via mark_node_clean for every existing-node "
            "whose hash actually changed -- which is C only. P's content / "
            "desc hash is untouched, so the auto-mark gate skips P."
        ),
    )
    time.sleep(0.05)
    result = axiom_graph_update_section(
        project_root=str(mini_project),
        section_id=child_id,
        content="Child section content updated to acknowledge code change.",
    )
    assert "ERROR" not in result, f"update_section failed: {result}"

    口 = Step(
        step_num=6,
        name="Assert C clears, P persists -- the cascade-safety invariant",
        purpose=(
            "Cascade correctness (ADR-018): the auto-mark hook only writes a "
            "node_verification snapshot for the node that was actually saved. "
            "Pass 2 of _get_linked_stale_ids therefore filters out C (its "
            "verified_at is newer than foo's latest change) but leaves P in "
            "the map (no verification row was ever written for P). The "
            "writer-is-verifier promotion does NOT propagate to peers."
        ),
        outputs="child_id absent from _get_linked_stale_ids; parent_id still present",
    )
    ls_map_after = _get_linked_stale_ids(db_path)
    assert child_id not in ls_map_after, (
        f"Auto-mark regression: {child_id} still LINKED_STALE after its own "
        f"update_section. Pass 2 should have read its node_verification row "
        f"and filtered it out. LS map: {sorted(ls_map_after)}"
    )
    assert parent_id in ls_map_after, (
        f"Cascade-safety regression (US-5 / D-3 / ADR-018): {parent_id} was "
        f"silently cleared by auto-mark on the child. The writer-is-verifier "
        f"hook must only emit AGENT_VERIFIED for the saved node, never for "
        f"sibling / parent linked nodes whose own hash did NOT change. "
        f"LS map after child edit: {sorted(ls_map_after)}"
    )

    # Pass 1 (raw, verification-agnostic) still reports both -- this is the
    # invariant boundary: clearing happens at Pass 2, not Pass 1.
    stale_after = get_stale_doc_sections(db_path)
    stale_section_ids_after = {row["section_id"] for row in stale_after}
    assert parent_id in stale_section_ids_after, (
        f"Pass-1 invariant violated: {parent_id} dropped from "
        f"get_stale_doc_sections. Pass 1 is verification-agnostic. "
        f"Stale sections: {stale_section_ids_after}"
    )
    assert child_id in stale_section_ids_after, (
        f"Pass-1 invariant violated: {child_id} dropped from "
        f"get_stale_doc_sections. Clearing belongs to Pass 2 of "
        f"_get_linked_stale_ids, not Pass 1. Stale sections: {stale_section_ids_after}"
    )
