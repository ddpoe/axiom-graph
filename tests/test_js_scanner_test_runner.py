"""Tests for JS scanner test-runner-aware envelope detection.

Covers the test-runner dispatch added on top of Phase B's HOF envelope
recognition: ``test('name', workflow(opts)(fn))`` (inline form) and
``test('name', existingWorkflowVar)`` (reference form), plus negative
cases (D1 duplicate name, D2 interpolated name).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_typescript")

from axiom_graph.scanners.js_scanner import scan_js_module  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "scanners" / "fixtures" / "js"


def _scan(file_name: str):
    findings: list = []
    autosteps: list = []
    nodes, edges = scan_js_module(
        FIXTURE_DIR / file_name,
        FIXTURE_DIR,
        "test",
        findings_out=findings,
        autosteps_out=autosteps,
    )
    return nodes, edges, findings


def _envelopes(nodes):
    return [n for n in nodes if n.node_type == "composite_process" and n.subtype in ("workflow", "task")]


# ---------------------------------------------------------------------------
# Inline form
# ---------------------------------------------------------------------------


def test_inline_test_workflow_emits_tagged_envelope():
    nodes, edges, findings = _scan("test_runner_inline.ts")
    envelopes = _envelopes(nodes)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.subtype == "workflow"
    assert "test" in env.tags
    assert "envelope" in env.tags
    assert env.dflow_meta["test_runner"] == "test"
    assert env.dflow_meta["test_name"] == "Bug 3: node paints running before completed"
    assert env.dflow_meta["purpose"] == "regression for terminal-event defer"
    # Envelope ID derived from slug.
    assert "test::bug-3-node-paints-running-before-completed" in env.id
    # Step nodes from the wrapped body still extracted.
    steps = [n for n in nodes if n.node_type == "atomic_process" and getattr(n, "subtype", None) == "step"]
    assert len(steps) == 2
    assert findings == []


def test_inline_it_task_emits_task_subtype():
    nodes, _edges, findings = _scan("test_runner_it_task.ts")
    envelopes = _envelopes(nodes)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.subtype == "task"
    assert "test" in env.tags
    assert env.dflow_meta["test_runner"] == "it"
    assert env.dflow_meta["test_name"] == "bridge service handles A then B"
    assert findings == []


def test_template_literal_no_interpolation_accepted():
    nodes, _edges, findings = _scan("test_runner_template_no_interp.ts")
    envelopes = _envelopes(nodes)
    assert len(envelopes) == 1
    assert "test" in envelopes[0].tags
    assert envelopes[0].dflow_meta["test_name"] == "Plain template literal"
    assert findings == []


def test_multi_test_file_emits_distinct_envelopes():
    nodes, _edges, findings = _scan("test_runner_multi.ts")
    envelopes = _envelopes(nodes)
    assert len(envelopes) == 3
    test_names = sorted(e.dflow_meta["test_name"] for e in envelopes)
    assert test_names == ["First case", "Second case", "Third case"]
    for env in envelopes:
        assert "test" in env.tags
    assert findings == []


def test_describe_block_inner_test_recognized():
    nodes, _edges, findings = _scan("test_runner_describe.ts")
    envelopes = _envelopes(nodes)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert "test" in env.tags
    assert env.dflow_meta["test_name"] == "Inside describe"
    assert findings == []


# ---------------------------------------------------------------------------
# Reference form
# ---------------------------------------------------------------------------


def test_reference_form_back_patches_existing_envelope():
    nodes, _edges, findings = _scan("test_runner_reference.ts")
    envelopes = _envelopes(nodes)
    # Only one envelope -- the const-declared one.  Reference-form does NOT
    # create a second envelope; it tags the existing one.
    assert len(envelopes) == 1
    env = envelopes[0]
    assert "test" in env.tags
    assert env.dflow_meta["test_runner"] == "test"
    assert env.dflow_meta["test_name"] == "Foo flow regression"
    # Original purpose preserved.
    assert env.dflow_meta["purpose"] == "shared workflow body"
    assert findings == []


# ---------------------------------------------------------------------------
# Coexistence with const-form envelopes
# ---------------------------------------------------------------------------


def test_coexistence_const_envelope_untagged_inline_envelope_tagged():
    nodes, _edges, findings = _scan("test_runner_coexistence.ts")
    envelopes = _envelopes(nodes)
    assert len(envelopes) == 2
    by_purpose = {e.dflow_meta.get("purpose"): e for e in envelopes}
    helper = by_purpose["helper workflow used elsewhere"]
    inline = by_purpose["inline-form test envelope"]
    # The helper const is NOT tagged -- never referenced by a test() call.
    assert "test" not in (helper.tags or [])
    assert "test_runner" not in (helper.dflow_meta or {})
    # The inline-form envelope IS tagged.
    assert "test" in inline.tags
    assert inline.dflow_meta["test_runner"] == "test"
    assert findings == []


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


def test_d2_interpolated_test_name_emits_finding_no_envelope():
    nodes, _edges, findings = _scan("test_runner_d2_interpolated.ts")
    envelopes = _envelopes(nodes)
    assert envelopes == []
    d2 = [f for f in findings if f.rule_id == "D2"]
    assert len(d2) == 1


def test_d1_duplicate_test_name_emits_finding_first_wins():
    nodes, _edges, findings = _scan("test_runner_d1_duplicate.ts")
    envelopes = _envelopes(nodes)
    assert len(envelopes) == 1
    # First-occurrence-wins: the kept envelope carries the first purpose.
    assert envelopes[0].dflow_meta["purpose"] == "first wins"
    d1 = [f for f in findings if f.rule_id == "D1"]
    assert len(d1) == 1


def test_plain_function_arg_produces_no_envelope():
    nodes, _edges, findings = _scan("test_runner_plain_fn.ts")
    assert _envelopes(nodes) == []
    # Plain unannotated test -- no findings either.
    assert [f for f in findings if f.rule_id in ("D1", "D2")] == []


def test_test_dot_skip_modifier_ignored():
    nodes, _edges, findings = _scan("test_runner_skip_modifier.ts")
    # Member-expression callee (test.skip) is out of v1 allowlist.  No
    # envelope, no D1/D2 finding.
    assert _envelopes(nodes) == []
    assert [f for f in findings if f.rule_id in ("D1", "D2")] == []
