"""The PEV doc-scope hook scopes section writes to the cycle manifest under path-form doc ids.

``hooks/pev-doc-scope.sh`` recovers a section's document by stripping the last
``::segment`` from its section id, and compares the result with the manifest id
stored in ``.pev-state.json``. Doc ids join path segments with ``/`` and keep the
docs root as their prefix, so these tests pin that the strip still yields the
document for such ids, including nested section dot-paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from axiom_annotations import workflow
from pev_hook_helpers import BASH, HOOKS_DIR, bash_has_jq, run_hook

pytestmark = [
    pytest.mark.skipif(not HOOKS_DIR.is_dir(), reason="PEV plugin source not present"),
    pytest.mark.skipif(BASH is None, reason="bash not available"),
    pytest.mark.skipif(not bash_has_jq(), reason="jq not available to the hooks"),
]

CYCLE_DOC_ID = "proj::docs/pev/cycles/pev-2026-01-01-example"


def _write_state(root: Path, cycle_doc_id: str = CYCLE_DOC_ID) -> None:
    (root / ".pev-state.json").write_text(
        json.dumps({"cycle_id": "pev-2026-01-01-example", "cycle_doc_id": cycle_doc_id, "worktree_path": str(root)}),
        encoding="utf-8",
    )


def _payload(root: Path, tool: str, tool_input: dict) -> dict:
    return {
        "agent_type": "pev:pev-builder",
        "agent_id": "doc-scope-test",
        "tool_name": f"mcp__axiom-graph__{tool}",
        "tool_input": tool_input,
        "cwd": str(root),
    }


@pytest.mark.parametrize("tool", ["axiom_graph_update_section", "axiom_graph_patch_section"])
@pytest.mark.parametrize("section", ["status", "builder.manifest", "architect.test-plan"])
def test_section_write_on_the_cycle_manifest_is_allowed(tmp_path: Path, tool: str, section: str) -> None:
    _write_state(tmp_path)
    result = run_hook(
        "pev-doc-scope.sh", _payload(tmp_path, tool, {"section_id": f"{CYCLE_DOC_ID}::{section}"}), tmp_path
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "other_doc",
    [
        "proj::docs/pev/cycles/pev-2026-01-02-sibling",
        "proj::docs/pev/cycles",
        "proj::.pev/test-policy",
        "proj::docs/features/x/prd",
    ],
)
def test_section_write_on_another_document_is_blocked(tmp_path: Path, other_doc: str) -> None:
    _write_state(tmp_path)
    payload = _payload(tmp_path, "axiom_graph_update_section", {"section_id": f"{other_doc}::status"})
    result = run_hook("pev-doc-scope.sh", payload, tmp_path)
    assert result.returncode == 2
    assert "Doc-scope violation" in result.stderr


def test_manifest_under_a_dot_prefixed_root_is_matched(tmp_path: Path) -> None:
    doc_id = "proj::.pev/cycles/pev-2026-01-01-example"
    _write_state(tmp_path, doc_id)
    payload = _payload(tmp_path, "axiom_graph_patch_section", {"section_id": f"{doc_id}::decisions"})
    result = run_hook("pev-doc-scope.sh", payload, tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "doc_json",
    [
        {"id": "pev/cycles/pev-2026-01-01-example", "title": "Manifest", "sections": []},
        {"id": "pev/cycles/other", "title": "Other", "sections": []},
        {"title": "No id", "sections": []},
    ],
)
def test_write_doc_is_refused_with_a_pointer_to_the_section_tools(tmp_path: Path, doc_json: dict) -> None:
    """A scoped agent never writes a whole document: the manifest exists, and a rewrite would clobber it."""
    _write_state(tmp_path)
    payload = _payload(tmp_path, "axiom_graph_write_doc", {"doc_json": json.dumps(doc_json)})
    result = run_hook("pev-doc-scope.sh", payload, tmp_path)
    assert result.returncode == 2
    assert "axiom_graph_update_section" in result.stderr
    assert "Could not extract doc_id" not in result.stderr


def test_auditor_may_write_docs(tmp_path: Path) -> None:
    _write_state(tmp_path)
    payload = _payload(tmp_path, "axiom_graph_write_doc", {"doc_json": json.dumps({"title": "PRD", "sections": []})})
    payload["agent_type"] = "pev:pev-auditor"
    result = run_hook("pev-doc-scope.sh", payload, tmp_path)
    assert result.returncode == 0, result.stderr


def test_add_section_compares_the_doc_id_directly(tmp_path: Path) -> None:
    _write_state(tmp_path)
    allowed = run_hook(
        "pev-doc-scope.sh", _payload(tmp_path, "axiom_graph_add_section", {"doc_id": CYCLE_DOC_ID}), tmp_path
    )
    assert allowed.returncode == 0, allowed.stderr

    blocked = run_hook(
        "pev-doc-scope.sh",
        _payload(tmp_path, "axiom_graph_add_section", {"doc_id": "proj::docs/pev/cycles/other"}),
        tmp_path,
    )
    assert blocked.returncode == 2


def test_legacy_layout_does_not_apply_the_append_only_rule(tmp_path: Path) -> None:
    """A cycle that started single-file finishes single-file: patch_section on its decisions still works."""
    _write_state(tmp_path)
    for tool in ("axiom_graph_patch_section", "axiom_graph_update_section"):
        result = run_hook(
            "pev-doc-scope.sh", _payload(tmp_path, tool, {"section_id": f"{CYCLE_DOC_ID}::decisions"}), tmp_path
        )
        assert result.returncode == 0, result.stderr
        batch = {"edits": [{"section_id": f"{CYCLE_DOC_ID}::status"}, {"section_id": f"{CYCLE_DOC_ID}::decisions"}]}
        result = run_hook("pev-doc-scope.sh", _payload(tmp_path, tool, batch), tmp_path)
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Directory layout: the cycle is a directory of seven docs with per-doc owners.
# ---------------------------------------------------------------------------

CYCLE_DIR = "proj::docs/pev/cycles/pev-2026-01-01-example"
AGENTS = ["architect", "builder", "reviewer", "auditor", "doc-reviewer"]
FRICTION_GROUP = {
    "architect": "architect",
    "builder": "builder",
    "reviewer": "reviewer",
    "auditor": "auditor",
    "doc-reviewer": "doc-review",
}

# doc -> agents that may write it (decisions and friction through add_section only)
OWNERS = {
    "manifest": set(),
    "architect": {"architect"},
    "decisions": {"architect", "builder"},
    "builder": {"builder"},
    "review": {"reviewer"},
    "audit": {"auditor"},  # the Doc Reviewer writes audit::doc-review only (its own test)
    "friction": set(AGENTS),
}


def _write_dir_state(root: Path) -> None:
    (root / ".pev-state.json").write_text(
        json.dumps(
            {
                "cycle_id": "pev-2026-01-01-example",
                "cycle_doc_id": f"{CYCLE_DIR}/manifest",
                "layout": "directory",
                "worktree_path": str(root),
            }
        ),
        encoding="utf-8",
    )


def _dir_hook(root: Path, agent: str, tool: str, tool_input: dict):
    payload = _payload(root, tool, tool_input)
    payload["agent_type"] = f"pev:pev-{agent}"
    return run_hook("pev-doc-scope.sh", payload, root)


def _representative_write(agent: str, doc: str) -> tuple[str, dict]:
    if doc == "decisions":
        return "axiom_graph_add_section", {"doc_id": f"{CYCLE_DIR}/decisions", "section_id": "d-9", "heading": "D-9"}
    if doc == "friction":
        return "axiom_graph_add_section", {
            "doc_id": f"{CYCLE_DIR}/friction",
            "parent_id": FRICTION_GROUP[agent],
            "section_id": "a-tag",
            "heading": "a tag",
        }
    return "axiom_graph_update_section", {"section_id": f"{CYCLE_DIR}/{doc}::some-section"}


@workflow(
    purpose=(
        "In a directory-layout cycle each PEV agent may write only the cycle docs it owns: every agent type is tried "
        "against every one of the seven docs, and each deny message names the docs that agent may write"
    )
)
def test_directory_layout_enforces_per_doc_ownership(tmp_path: Path) -> None:
    _write_dir_state(tmp_path)
    for agent in AGENTS:
        for doc, owners in OWNERS.items():
            tool, tool_input = _representative_write(agent, doc)
            result = _dir_hook(tmp_path, agent, tool, tool_input)
            if agent in owners:
                assert result.returncode == 0, (agent, doc, result.stderr)
            else:
                assert result.returncode == 2, (agent, doc)
                assert "may write:" in result.stderr, (agent, doc, result.stderr)


@workflow(
    purpose=(
        "The directory layout's carve-outs: the Architect writes manifest::scope but no other manifest section, the "
        "Auditor writes audit and live docs outside the cycle but not builder, audit-skill agents are exempt, and the "
        "Reviewer owns review-shard-* sections and docs; an Auditor write_doc from a doc_file is checked by the "
        "file's id, a whitespace-padded id is compared stripped, and the write is refused when the file can't be "
        "read or isn't valid JSON"
    )
)
def test_directory_layout_carve_outs(tmp_path: Path) -> None:
    _write_dir_state(tmp_path)
    update = "axiom_graph_update_section"
    (tmp_path / "outside.docjson").write_text(json.dumps({"id": "features/x/prd", "title": "PRD"}), encoding="utf-8")
    into_cycle = tmp_path / "into-cycle.docjson"
    into_cycle.write_text(json.dumps({"id": "pev/cycles/pev-2026-01-01-example/audit", "title": "x"}), encoding="utf-8")
    padded_id = "  pev/cycles/pev-2026-01-01-example/audit \n"
    padded = tmp_path / "padded.docjson"
    padded.write_text(json.dumps({"id": padded_id, "title": "x"}), encoding="utf-8")
    (tmp_path / "malformed.docjson").write_text("{not json", encoding="utf-8")
    nested_progress = {"section_id": f"{CYCLE_DIR}/builder::inc-1.task-1.progress"}
    allowed = [
        ("architect", update, {"section_id": f"{CYCLE_DIR}/manifest::scope"}),
        ("auditor", update, {"section_id": f"{CYCLE_DIR}/audit::impact-report"}),
        ("auditor", update, {"section_id": "proj::docs/features/x/prd::capabilities"}),
        ("auditor", "axiom_graph_write_doc", {"doc_json": json.dumps({"id": "features/x/prd", "title": "PRD"})}),
        ("audit-dev-shard", update, {"section_id": "proj::docs/pev/audits/a::meta"}),
        ("reviewer", "axiom_graph_add_section", {"doc_id": f"{CYCLE_DIR}/review", "section_id": "review-shard-x"}),
        ("reviewer", update, {"section_id": f"{CYCLE_DIR}/review-shard-1::verdict"}),
        ("builder", update, nested_progress),
        ("auditor", "axiom_graph_write_doc", {"doc_file": "outside.docjson"}),
    ]
    for agent, tool, tool_input in allowed:
        result = _dir_hook(tmp_path, agent, tool, tool_input)
        assert result.returncode == 0, (agent, tool_input, result.stderr)

    denied = [
        ("architect", update, {"section_id": f"{CYCLE_DIR}/manifest::status"}),
        ("architect", "axiom_graph_add_section", {"doc_id": f"{CYCLE_DIR}/manifest", "section_id": "scope-2"}),
        # The orchestrator's merge-target ids and fix rounds are no agent's to write.
        ("auditor", "axiom_graph_patch_section", {"section_id": f"{CYCLE_DIR}/manifest::baseline"}),
        ("builder", "axiom_graph_add_section", {"doc_id": f"{CYCLE_DIR}/manifest", "parent_id": "fix-list"}),
        ("auditor", update, {"section_id": f"{CYCLE_DIR}/builder::build-plan"}),
        ("builder", update, {"section_id": f"{CYCLE_DIR}/review-shard-1::verdict"}),
        ("builder", update, {"section_id": "proj::docs/features/x/prd::capabilities"}),
        ("builder", update, {"section_id": "proj::docs/pev/cycles/pev-2026-01-02-other/builder::build-plan"}),
        ("reviewer", update, nested_progress),
        # A non-owner is told what it may write, not to retry with add_section.
        ("reviewer", update, {"section_id": f"{CYCLE_DIR}/decisions::d-1"}),
        ("auditor", "axiom_graph_write_doc", {"doc_file": str(into_cycle)}),
        ("auditor", "axiom_graph_write_doc", {"doc_file": "missing.docjson"}),
        ("auditor", "axiom_graph_write_doc", {"doc_file": "malformed.docjson"}),
        ("auditor", "axiom_graph_write_doc", {"doc_file": str(padded)}),
        ("auditor", "axiom_graph_write_doc", {"doc_json": json.dumps({"id": padded_id, "title": "x"})}),
    ]
    for agent, tool, tool_input in denied:
        result = _dir_hook(tmp_path, agent, tool, tool_input)
        assert result.returncode == 2, (agent, tool_input)
        assert "may write:" in result.stderr, result.stderr


@workflow(
    purpose=(
        "The Doc Reviewer of a directory-layout cycle writes only audit::doc-review and its subsections (its "
        "progress, findings and verdict) and its own friction group: the Auditor's audit sections, the rest of "
        "the cycle and live docs are denied, each deny naming what it may write"
    )
)
def test_doc_reviewer_writes_only_its_doc_review_section(tmp_path: Path) -> None:
    _write_dir_state(tmp_path)
    update = "axiom_graph_update_section"
    add = "axiom_graph_add_section"
    audit = f"{CYCLE_DIR}/audit"
    allowed = [
        (update, {"section_id": f"{audit}::doc-review"}),
        ("axiom_graph_patch_section", {"section_id": f"{audit}::doc-review.findings"}),
        (update, _edits(f"{audit}::doc-review", f"{audit}::doc-review.progress")),
        (add, {"doc_id": audit, "parent_id": "doc-review", "section_id": "x"}),
        (add, {"doc_id": audit, "sections": [{"section_id": "a", "parent_id": "doc-review.findings"}]}),
        (add, {"doc_id": f"{CYCLE_DIR}/friction", "parent_id": "doc-review", "section_id": "a-tag"}),
    ]
    for tool, tool_input in allowed:
        result = _dir_hook(tmp_path, "doc-reviewer", tool, tool_input)
        assert result.returncode == 0, (tool_input, result.stderr)

    denied = [
        (update, {"section_id": f"{audit}::impact-report"}),
        (update, {"section_id": f"{audit}::doc-reviewer"}),
        (update, _edits(f"{audit}::doc-review", f"{audit}::changes-summary")),
        (add, {"doc_id": audit, "section_id": "x"}),
        (add, {"doc_id": audit, "parent_id": "impact-report", "section_id": "x"}),
        (add, {"doc_id": audit, "sections": [{"section_id": "a", "parent_id": "doc-review"}, {"section_id": "b"}]}),
        (update, {"section_id": "proj::docs/features/x/prd::capabilities"}),
        (update, {"section_id": f"{CYCLE_DIR}/manifest::baseline"}),
    ]
    for tool, tool_input in denied:
        result = _dir_hook(tmp_path, "doc-reviewer", tool, tool_input)
        assert result.returncode == 2, tool_input
        assert "may write: audit::doc-review and its subsections" in result.stderr, result.stderr

    impact = _dir_hook(tmp_path, "auditor", update, {"section_id": f"{audit}::impact-report"})
    assert impact.returncode == 0, impact.stderr


@workflow(
    purpose=(
        "decisions and the friction groups are append-only in a directory-layout cycle: update_section and "
        "patch_section are refused with a message naming add_section, and add_section is allowed only under the "
        "caller's own friction group"
    )
)
def test_directory_layout_logs_are_append_only(tmp_path: Path) -> None:
    _write_dir_state(tmp_path)
    for tool in ("axiom_graph_update_section", "axiom_graph_patch_section"):
        for section_id in (f"{CYCLE_DIR}/decisions::d-1", f"{CYCLE_DIR}/friction::builder.a-tag"):
            result = _dir_hook(tmp_path, "builder", tool, {"section_id": section_id})
            assert result.returncode == 2, (tool, section_id)
            assert "axiom_graph_add_section" in result.stderr

    friction = f"{CYCLE_DIR}/friction"
    own = _dir_hook(
        tmp_path, "builder", "axiom_graph_add_section", {"doc_id": friction, "parent_id": "builder", "section_id": "x"}
    )
    assert own.returncode == 0, own.stderr
    for parent in ("reviewer", "", "builderish"):
        other = _dir_hook(
            tmp_path, "builder", "axiom_graph_add_section", {"doc_id": friction, "parent_id": parent, "section_id": "x"}
        )
        assert other.returncode == 2, parent
        assert 'parent_id="builder"' in other.stderr

    batch = {
        "doc_id": friction,
        "sections": [{"section_id": "a", "parent_id": "reviewer"}, {"section_id": "b", "parent_id": "builder"}],
    }
    assert _dir_hook(tmp_path, "reviewer", "axiom_graph_add_section", batch).returncode == 2


@workflow(
    purpose=(
        "No PEV subagent clones a document in either layout, and write_doc stays refused to every subagent but the "
        "Auditor writing outside the cycle directory"
    )
)
def test_clone_doc_and_write_doc_are_refused(tmp_path: Path) -> None:
    clone = {"source_doc_id": "proj::.pev/templates/cycle/manifest", "new_id": "pev/cycles/x/manifest"}
    for write_state in (_write_state, _write_dir_state):
        write_state(tmp_path)
        for agent in AGENTS:
            result = _dir_hook(tmp_path, agent, "axiom_graph_clone_doc", clone)
            assert result.returncode == 2, (write_state.__name__, agent)
            assert "only cloner" in result.stderr

    _write_dir_state(tmp_path)
    for agent in ("architect", "builder", "reviewer", "doc-reviewer"):
        result = _dir_hook(tmp_path, agent, "axiom_graph_write_doc", {"doc_json": json.dumps({"title": "x"})})
        assert result.returncode == 2, agent
        assert "axiom_graph_add_section" in result.stderr
    into_cycle = {"doc_json": json.dumps({"id": "pev/cycles/pev-2026-01-01-example/audit", "title": "x"})}
    assert _dir_hook(tmp_path, "auditor", "axiom_graph_write_doc", into_cycle).returncode == 2


def test_directory_layout_rejects_a_cycle_doc_id_that_is_not_a_manifest(tmp_path: Path) -> None:
    (tmp_path / ".pev-state.json").write_text(
        json.dumps({"cycle_doc_id": CYCLE_DIR, "layout": "directory"}), encoding="utf-8"
    )
    result = _dir_hook(tmp_path, "builder", "axiom_graph_update_section", {"section_id": f"{CYCLE_DIR}/builder::x"})
    assert result.returncode == 2
    assert "not a cycle manifest" in result.stderr


# ---------------------------------------------------------------------------
# Batch edits: update_section / patch_section with edits=[...], in both layouts.
# ---------------------------------------------------------------------------


def _edits(*section_ids: str) -> dict:
    return {"edits": [{"section_id": sid, "content": "x"} for sid in section_ids]}


def _own_and_other(root: Path, layout: str) -> tuple[str, str]:
    """Write the layout's state file; return a section the Builder owns and one it doesn't."""
    if layout == "legacy":
        _write_state(root)
        return f"{CYCLE_DOC_ID}::status", "proj::docs/pev/cycles/pev-2026-01-02-sibling::status"
    _write_dir_state(root)
    return f"{CYCLE_DIR}/builder::progress", f"{CYCLE_DIR}/review::verdict"


@pytest.mark.parametrize("layout", ["legacy", "directory"])
@pytest.mark.parametrize("tool", ["axiom_graph_update_section", "axiom_graph_patch_section"])
def test_batch_edits_are_checked_item_by_item(tmp_path: Path, layout: str, tool: str) -> None:
    """Every edits[] item gets the single-section check; one failing item denies the call and is named."""
    own, other = _own_and_other(tmp_path, layout)

    def hook(tool_input: dict):
        return _dir_hook(tmp_path, "builder", tool, tool_input)

    assert hook(_edits(own, f"{own}.child")).returncode == 0
    denied = hook(_edits(own, other))
    assert denied.returncode == 2
    assert f"'{other}'" in denied.stderr and "Doc-scope violation" in denied.stderr

    for bad in ({"edits": [{"section_id": own}, {"content": "x"}]}, {"edits": json.dumps(_edits(own)["edits"])}):
        assert hook(bad).returncode == 2, bad

    # A top-level section_id sent alongside edits is checked as well as the items.
    assert hook({"section_id": own, **_edits(own)}).returncode == 0
    top_level = hook({"section_id": other, **_edits(own)})
    assert top_level.returncode == 2
    assert f"top-level section_id '{other}'" in top_level.stderr
    assert hook({"section_id": own, **_edits(other)}).returncode == 2


@pytest.mark.parametrize("layout", ["legacy", "directory"])
def test_null_or_empty_edits_count_as_absent(tmp_path: Path, layout: str) -> None:
    """edits: null or [] leaves the call to the single-section check on its top-level section_id."""
    own, other = _own_and_other(tmp_path, layout)
    update = "axiom_graph_update_section"
    for empty in (None, []):
        assert _dir_hook(tmp_path, "builder", update, {"section_id": own, "edits": empty}).returncode == 0
        denied = _dir_hook(tmp_path, "builder", update, {"section_id": other, "edits": empty})
        assert denied.returncode == 2
        assert "batch edit" not in denied.stderr
        no_target = _dir_hook(tmp_path, "builder", update, {"edits": empty})
        assert no_target.returncode == 2
        assert "Could not extract doc_id" in no_target.stderr


@pytest.mark.parametrize("layout", ["legacy", "directory"])
def test_blank_top_level_section_id_beside_edits(tmp_path: Path, layout: str) -> None:
    """An empty or null top-level section_id is ignored beside edits; a whitespace-only one fails closed."""
    own, other = _own_and_other(tmp_path, layout)

    def hook(tool_input: dict):
        return _dir_hook(tmp_path, "builder", "axiom_graph_update_section", tool_input)

    for absent in ("", None):
        assert hook({"section_id": absent, **_edits(own)}).returncode == 0, absent
        assert hook({"section_id": absent, **_edits(own, other)}).returncode == 2, absent
    assert hook({"section_id": "   ", **_edits(own)}).returncode == 2


def test_directory_layout_batch_edits_keep_the_logs_append_only(tmp_path: Path) -> None:
    _write_dir_state(tmp_path)
    leaf = f"{CYCLE_DIR}/decisions::d-1"
    result = _dir_hook(tmp_path, "builder", "axiom_graph_update_section", _edits(f"{CYCLE_DIR}/builder::x", leaf))
    assert result.returncode == 2
    assert f"'{leaf}'" in result.stderr and "axiom_graph_add_section" in result.stderr


def test_batch_edits_keep_the_exemptions_and_the_scope_carve_out(tmp_path: Path) -> None:
    """Exempt agents batch freely, and the Architect's manifest::scope carve-out holds item by item."""
    update = "axiom_graph_update_section"
    live = "proj::docs/features/x/prd::capabilities"
    audit_doc = "proj::docs/pev/audits/a::meta"
    _write_state(tmp_path)
    assert _dir_hook(tmp_path, "auditor", update, _edits(live, f"{CYCLE_DOC_ID}::status")).returncode == 0
    assert _dir_hook(tmp_path, "audit-dev-shard", update, _edits(live, audit_doc)).returncode == 0

    _write_dir_state(tmp_path)
    assert _dir_hook(tmp_path, "audit-dev-shard", update, _edits(live, audit_doc)).returncode == 0
    assert _dir_hook(tmp_path, "auditor", update, _edits(live, f"{CYCLE_DIR}/audit::impact-report")).returncode == 0
    build_plan = f"{CYCLE_DIR}/builder::build-plan"
    auditor_out = _dir_hook(tmp_path, "auditor", update, _edits(live, build_plan))
    assert auditor_out.returncode == 2
    assert f"'{build_plan}'" in auditor_out.stderr

    scope, status = f"{CYCLE_DIR}/manifest::scope", f"{CYCLE_DIR}/manifest::status"
    assert _dir_hook(tmp_path, "architect", update, _edits(scope, f"{CYCLE_DIR}/architect::problem")).returncode == 0
    denied = _dir_hook(tmp_path, "architect", update, _edits(scope, status))
    assert denied.returncode == 2
    assert f"batch edit '{status}'" in denied.stderr and "manifest::scope only" in denied.stderr


@pytest.mark.parametrize(
    "shell_syntax",
    ["$(touch pwned)", "`touch pwned`", "x'; touch pwned; '", 'x"; touch pwned; "', "x\\'$(touch pwned)"],
)
def test_injection_shaped_batch_ids_are_data(tmp_path: Path, shell_syntax: str) -> None:
    """A section id carrying shell syntax is compared as a string: it never runs, and it is still scope-checked."""
    _write_dir_state(tmp_path)
    update = "axiom_graph_update_section"
    progress = f"{CYCLE_DIR}/builder::progress"
    in_scope = _dir_hook(tmp_path, "builder", update, _edits(progress, f"{CYCLE_DIR}/builder::{shell_syntax}"))
    out_of_scope = _dir_hook(tmp_path, "builder", update, _edits(progress, f"{shell_syntax}::x"))
    assert in_scope.returncode == 0, in_scope.stderr
    assert out_of_scope.returncode == 2
    assert not (tmp_path / "pwned").exists()


@workflow(
    purpose=(
        "The write shapes the role skills document for a directory-layout cycle pass the hook for their owner only: "
        "the Builder's one-call batch that adds inc-N, task-M under it and task-M's progress under that, a friction "
        "entry under the caller's own group, and a batch rewrite of the nested progress section"
    )
)
def test_documented_role_write_shapes_pass_for_their_owner(tmp_path: Path) -> None:
    _write_dir_state(tmp_path)
    add = "axiom_graph_add_section"
    nested_batch = {
        "doc_id": f"{CYCLE_DIR}/builder",
        "sections": [
            {"section_id": "inc-2", "heading": "Incarnation 2"},
            {"section_id": "task-3", "parent_id": "inc-2", "heading": "Task 3"},
            {"section_id": "progress", "parent_id": "inc-2.task-3", "heading": "Progress", "content": "Started."},
        ],
    }
    assert _dir_hook(tmp_path, "builder", add, nested_batch).returncode == 0
    assert _dir_hook(tmp_path, "reviewer", add, nested_batch).returncode == 2

    friction = {"doc_id": f"{CYCLE_DIR}/friction", "parent_id": "builder", "section_id": "slow-suite", "heading": "s"}
    assert _dir_hook(tmp_path, "builder", add, friction).returncode == 0

    rewrite = _edits(f"{CYCLE_DIR}/builder::inc-2.task-3.progress")
    assert _dir_hook(tmp_path, "builder", "axiom_graph_update_section", rewrite).returncode == 0
    assert _dir_hook(tmp_path, "reviewer", "axiom_graph_update_section", rewrite).returncode == 2


# ---------------------------------------------------------------------------
# builder_docs: the Builder may also write the pitch's deliverable docs.
# ---------------------------------------------------------------------------

DELIVERABLE = "proj::docs/features/x/prd"
UNLISTED = "proj::docs/features/y/prd"


def _write_layout_state(root: Path, layout: str, builder_docs) -> str:
    """Write a state file for ``layout`` ("directory" or "legacy"); return a section the Builder owns."""
    state: dict = {"cycle_id": "pev-2026-01-01-example", "worktree_path": str(root)}
    if layout == "directory":
        state.update(cycle_doc_id=f"{CYCLE_DIR}/manifest", layout="directory")
        own = f"{CYCLE_DIR}/builder::build-plan"
    else:
        state["cycle_doc_id"] = CYCLE_DOC_ID
        own = f"{CYCLE_DOC_ID}::status"
    if builder_docs is not None:
        state["builder_docs"] = builder_docs
    (root / ".pev-state.json").write_text(json.dumps(state), encoding="utf-8")
    return own


@workflow(
    purpose=(
        "In both layouts the Builder may write a doc listed in the state file's builder_docs, by single write, "
        "add_section or batch edit; an unlisted doc, a batch with one unlisted item and write_doc stay denied, "
        "and the other roles keep their old behaviour"
    )
)
@pytest.mark.parametrize("layout", ["directory", "legacy"])
def test_builder_docs_open_listed_deliverables_to_the_builder_only(tmp_path: Path, layout: str) -> None:
    own = _write_layout_state(tmp_path, layout, [DELIVERABLE, "proj::docs/other/listed"])
    update = "axiom_graph_update_section"

    allowed = [
        ("builder", update, {"section_id": f"{DELIVERABLE}::capabilities"}),
        ("builder", "axiom_graph_patch_section", {"section_id": f"{DELIVERABLE}::capabilities.table"}),
        ("builder", "axiom_graph_add_section", {"doc_id": DELIVERABLE, "section_id": "new", "heading": "New"}),
        ("builder", update, _edits(own, f"{DELIVERABLE}::capabilities")),
        ("auditor", update, {"section_id": f"{UNLISTED}::capabilities"}),
    ]
    denied = [
        ("builder", update, {"section_id": f"{UNLISTED}::capabilities"}),
        ("builder", "axiom_graph_add_section", {"doc_id": UNLISTED, "section_id": "new", "heading": "New"}),
        ("builder", update, _edits(f"{DELIVERABLE}::capabilities", f"{UNLISTED}::capabilities")),
        ("builder", "axiom_graph_write_doc", {"doc_json": json.dumps({"id": "features/x/prd", "sections": []})}),
        ("reviewer", update, {"section_id": f"{DELIVERABLE}::capabilities"}),
        ("architect", update, {"section_id": f"{DELIVERABLE}::capabilities"}),
    ]
    for agent, tool, tool_input in allowed:
        result = _dir_hook(tmp_path, agent, tool, tool_input)
        assert result.returncode == 0, (agent, tool_input, result.stderr)
    for agent, tool, tool_input in denied:
        result = _dir_hook(tmp_path, agent, tool, tool_input)
        assert result.returncode == 2, (agent, tool_input)


@pytest.mark.parametrize("builder_docs", [None, [], "proj::docs/features/x/prd", [7]])
def test_absent_or_malformed_builder_docs_open_nothing(tmp_path: Path, builder_docs) -> None:
    _write_layout_state(tmp_path, "directory", builder_docs)
    result = _dir_hook(tmp_path, "builder", "axiom_graph_update_section", {"section_id": f"{DELIVERABLE}::a"})
    assert result.returncode == 2
    assert "outside the current cycle directory" in result.stderr
    assert "builder_docs" not in result.stderr


def test_builder_deny_message_lists_its_deliverable_docs(tmp_path: Path) -> None:
    _write_layout_state(tmp_path, "directory", [DELIVERABLE, "proj::docs/other/listed"])
    result = _dir_hook(tmp_path, "builder", "axiom_graph_update_section", {"section_id": f"{UNLISTED}::a"})
    assert result.returncode == 2
    assert f"(builder_docs): {DELIVERABLE}, proj::docs/other/listed" in result.stderr


# ---------------------------------------------------------------------------
# Instance layout: a /pev-instance run's reviewers write only their own checkin section.
# ---------------------------------------------------------------------------

CHECKIN = "proj::docs/pev/instances/2026-01-01-example"
INSTANCE_OWN = {"reviewer": "review", "doc-reviewer": "doc-review"}


def _write_instance_state(root: Path, worktree: bool = False) -> None:
    state = {"layout": "instance", "cycle_doc_id": CHECKIN}
    if worktree:
        state["worktree_path"] = str(root)
    (root / ".pev-state.json").write_text(json.dumps(state), encoding="utf-8")


def _instance_attempts(own: str | None) -> list[tuple[str, dict, bool]]:
    """(tool, tool_input, allowed) for one agent whose own checkin section is ``own`` (None: no section)."""
    update = "axiom_graph_update_section"
    add = "axiom_graph_add_section"
    attempts = []
    for section in ("review", "review.findings", "doc-review", "doc-review.findings", "plan", "reviewer"):
        mine = own is not None and (section == own or section.startswith(f"{own}."))
        attempts.append((update, {"section_id": f"{CHECKIN}::{section}"}, mine))
    for parent in ("review", "doc-review", None):
        tool_input = {"doc_id": CHECKIN, "section_id": "x", "heading": "X"}
        if parent:
            tool_input["parent_id"] = parent
        attempts.append((add, tool_input, own is not None and parent == own))
    if own is not None:
        attempts += [
            (update, _edits(f"{CHECKIN}::{own}", f"{CHECKIN}::{own}.a"), True),
            (update, _edits(f"{CHECKIN}::{own}", f"{CHECKIN}::plan"), False),
            (add, {"doc_id": CHECKIN, "sections": [{"section_id": "a", "parent_id": own}]}, True),
            (add, {"doc_id": CHECKIN, "sections": [{"section_id": "a", "parent_id": own}, {"section_id": "b"}]}, False),
            (add, {"doc_id": "proj::docs/other", "parent_id": own, "section_id": "x"}, False),
            (update, {"section_id": f"proj::docs/other::{own}"}, False),
            ("axiom_graph_write_doc", {"doc_json": json.dumps({"id": "other", "sections": []})}, False),
        ]
    return attempts


@workflow(
    purpose=(
        "With an instance state file the Reviewer writes only checkin::review and its subsections, the Doc Reviewer "
        "only checkin::doc-review and its subsections, and every other PEV agent writes nothing; each deny names "
        "the section the agent may write, whether or not the state file names the instance's worktree"
    )
)
@pytest.mark.parametrize("worktree", [False, True])
@pytest.mark.parametrize("agent", ["reviewer", "doc-reviewer", "builder", "auditor", "architect", "audit-dev-shard"])
def test_instance_layout_scopes_each_reviewer_to_its_own_section(tmp_path: Path, agent: str, worktree: bool) -> None:
    _write_instance_state(tmp_path, worktree)
    own = INSTANCE_OWN.get(agent)

    for tool, tool_input, expect_allowed in _instance_attempts(own):
        result = _dir_hook(tmp_path, agent, tool, tool_input)
        if expect_allowed:
            assert result.returncode == 0, (tool, tool_input, result.stderr)
            continue
        assert result.returncode == 2, (tool, tool_input)
        if own is not None:
            assert f"may write only {CHECKIN}::{own} and its subsections" in result.stderr
        else:
            assert f"{CHECKIN}::review" in result.stderr and f"{CHECKIN}::doc-review" in result.stderr


@workflow(
    purpose=(
        "The Auditor's axiom_graph_delete_section is scoped like a section write: it may delete a live doc's "
        "section and its own audit section, never another agent's cycle doc section or a log entry"
    )
)
def test_delete_section_is_scoped_like_a_section_write(tmp_path: Path) -> None:
    _write_dir_state(tmp_path)
    delete = "axiom_graph_delete_section"
    allowed = ["proj::docs/features/x/prd::migration", f"{CYCLE_DIR}/audit::progress"]
    denied = [
        f"{CYCLE_DIR}/builder::build-plan",
        f"{CYCLE_DIR}/manifest::status",
        f"{CYCLE_DIR}/friction::auditor.some-entry",
        f"{CYCLE_DIR}/decisions::d-1",
    ]
    for section_id in allowed:
        result = _dir_hook(tmp_path, "auditor", delete, {"section_id": section_id})
        assert result.returncode == 0, (section_id, result.stderr)
    for section_id in denied:
        assert _dir_hook(tmp_path, "auditor", delete, {"section_id": section_id}).returncode == 2, section_id


def test_hooks_json_routes_delete_section_to_the_doc_scope_hook() -> None:
    import re

    config = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
    tool = "mcp__axiom-graph__axiom_graph_delete_section"
    commands = [
        hook["command"]
        for entry in config["hooks"]["PreToolUse"]
        if re.fullmatch(entry.get("matcher", ""), tool)
        for hook in entry["hooks"]
    ]
    assert any("pev-doc-scope.sh" in command for command in commands), commands
