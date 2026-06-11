"""Fixture-based tests for JS/TS scanner envelope + step extraction (Phase B).

Covers US-1 envelope/step parity, US-2 cross-module delegates_to, US-3 strict
literal findings, US-4 validation rule firing, US-1+US-4 loop ancestry, and
JS/TS parity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_typescript")

from axiom_graph.scanners.js_scanner import scan_js_module  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "scanners" / "fixtures" / "js"


def _scan(file_name: str, project_root: Path | None = None):
    project_root = project_root or FIXTURE_DIR
    findings: list = []
    autosteps: list = []
    nodes, edges = scan_js_module(
        FIXTURE_DIR / file_name,
        project_root,
        "test",
        findings_out=findings,
        autosteps_out=autosteps,
    )
    return nodes, edges, findings, autosteps


# ---------------------------------------------------------------------------
# US-1: envelope extraction
# ---------------------------------------------------------------------------


def test_workflow_envelope_node_emitted():
    nodes, edges, _, _ = _scan("positive_envelope.ts")
    envelopes = [n for n in nodes if n.node_type == "composite_process" and n.subtype == "workflow"]
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.id.endswith("@workflow")
    assert env.dflow_meta == {"purpose": "p", "critical": "c"}
    assert "envelope" in (env.tags or [])

    edge_types = {(e.edge_type, e.from_id, e.to_id) for e in edges}
    func_id = "test::positive_envelope::run"
    assert ("composes", "test::positive_envelope", env.id) in edge_types
    assert ("annotates", env.id, func_id) in edge_types


def test_step_and_autostep_nodes_emitted():
    nodes, edges, _, _ = _scan("positive_steps.ts")
    steps = [n for n in nodes if n.node_type == "atomic_process" and n.subtype in ("step", "autostep")]
    assert len(steps) == 2
    by_subtype = {s.subtype: s for s in steps}
    assert by_subtype["step"].code_hash == ""
    assert by_subtype["step"].desc_hash is None
    assert by_subtype["step"].dflow_meta["step_num_raw"] == "1"
    assert by_subtype["autostep"].dflow_meta["step_num_raw"] == "2"

    envelope_id = "test::positive_steps::run@workflow"
    edge_keys = {(e.edge_type, e.from_id, e.to_id) for e in edges}
    for s in steps:
        assert ("composes", envelope_id, s.id) in edge_keys


def test_camelcase_step_num_normalized():
    nodes, _, _, _ = _scan("positive_steps.ts")
    step1 = next(n for n in nodes if n.id.endswith("::step-1"))
    assert step1.dflow_meta["step_num_raw"] == "1"
    assert step1.dflow_meta["step_num_parts"] == [1]


# ---------------------------------------------------------------------------
# US-2: delegates_to resolution
# ---------------------------------------------------------------------------


def test_named_import_delegates_to():
    nodes, edges, _, _ = _scan("pipeline_named.ts")
    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert len(delegates) == 1
    assert delegates[0].to_id == "test::loader::loadData"


def test_namespace_member_delegates_to():
    nodes, edges, _, _ = _scan("pipeline_namespace.ts")
    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert len(delegates) == 1
    assert delegates[0].to_id == "test::loader::loadData"


# ---------------------------------------------------------------------------
# US-3: strict-literal findings
# ---------------------------------------------------------------------------


def test_non_literal_step_emits_finding_and_skips():
    nodes, _, findings, _ = _scan("negative_literals.ts")
    important = [f for f in findings if f.severity == "IMPORTANT"]
    assert len(important) >= 2  # Step(opts) + AutoStep()
    # Subsequent valid step still extracted
    step_nodes = [n for n in nodes if getattr(n, "subtype", None) == "step"]
    assert len(step_nodes) == 1
    assert step_nodes[0].dflow_meta["step_num_raw"] == "2"


# ---------------------------------------------------------------------------
# US-1 + US-4: loop ancestry
# ---------------------------------------------------------------------------


def test_minor_step_in_loops_no_b3_finding():
    _, _, findings, _ = _scan("loops.ts")
    b3 = [f for f in findings if f.rule_id == "B3"]
    assert b3 == []


# ---------------------------------------------------------------------------
# JS/TS parity
# ---------------------------------------------------------------------------


def test_js_and_ts_produce_same_envelope():
    ts_nodes, ts_edges, _, _ = _scan("positive_steps.ts")
    js_nodes, js_edges, _, _ = _scan("positive_steps.js")
    ts_env = next(n for n in ts_nodes if "@workflow" in n.id)
    js_env = next(n for n in js_nodes if "@workflow" in n.id)
    assert ts_env.dflow_meta == js_env.dflow_meta
    ts_steps = sorted(
        n.dflow_meta["step_num_raw"] for n in ts_nodes if getattr(n, "subtype", None) in ("step", "autostep")
    )
    js_steps = sorted(
        n.dflow_meta["step_num_raw"] for n in js_nodes if getattr(n, "subtype", None) in ("step", "autostep")
    )
    assert ts_steps == js_steps


# ---------------------------------------------------------------------------
# US-4: validation rules (B1 duplicate, B2 major-gap)
# ---------------------------------------------------------------------------


def test_b1_duplicate_step_num_emits_one_finding_first_occurrence_wins():
    """Two `Step({stepNum:1, ...})` calls in one envelope -> one B1 finding,
    and only one node with id ending in `::step-1` (first-occurrence wins).
    """
    nodes, _, findings, _ = _scan("b1_duplicate.ts")
    b1 = [f for f in findings if f.rule_id == "B1"]
    assert len(b1) == 1
    # Only one step-1 node was created.
    step_nodes = [n for n in nodes if getattr(n, "subtype", None) == "step" and n.id.endswith("::step-1")]
    assert len(step_nodes) == 1
    # The first-occurrence-wins rule keeps the FIRST literal: name='first'.
    assert step_nodes[0].dflow_meta["name"] == "first"


def test_b2_major_gap_emits_finding():
    """Major step numbers `[1, 3]` (skipping 2) -> one B2 finding."""
    _, _, findings, _ = _scan("b2_gap.ts")
    b2 = [f for f in findings if f.rule_id == "B2"]
    assert len(b2) == 1


# ---------------------------------------------------------------------------
# US-3: envelope-level non-literal opts (JS-LIT-ENV)
# ---------------------------------------------------------------------------


def test_workflow_non_literal_opts_emits_lit_env_finding_no_envelope_node():
    """`workflow(opts)(fn)` with `opts` as an identifier -> IMPORTANT finding;
    no envelope node; the function-level node for `run` is still created.
    """
    nodes, _, findings, _ = _scan("negative_envelope.ts")

    important = [f for f in findings if f.severity == "IMPORTANT" and f.rule_id == "JS-LIT-ENV"]
    assert len(important) == 1
    finding = important[0]
    assert finding.module.endswith("negative_envelope.ts")
    assert finding.line >= 1

    # No envelope node was emitted (envelope kwargs not extracted).
    envelopes = [n for n in nodes if n.node_type == "composite_process" and getattr(n, "subtype", None) == "workflow"]
    assert envelopes == []

    # Function-level node for `run` is still created.
    run_nodes = [n for n in nodes if n.id == "test::negative_envelope::run"]
    assert len(run_nodes) == 1


# ---------------------------------------------------------------------------
# US-2: aliased and default import resolution
# ---------------------------------------------------------------------------


def test_aliased_import_resolves_to_original_name():
    """`import { loadData as ld } from './loader'` + `ld()` after AutoStep
    resolves the delegates_to edge against the ORIGINAL name (`loadData`),
    not the alias (`ld`).
    """
    _, edges, _, _ = _scan("aliased_import.ts")
    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert len(delegates) == 1
    assert delegates[0].to_id == "test::loader::loadData"


def test_default_import_yields_no_edge():
    """Default-import binding shape (D-4): `import handler from './m'` is
    stored as `(target_id, None)` -- same as namespace bindings.  A bare
    `handler()` call therefore produces NO delegates_to edge in v1.

    This test pins the chosen convention so a future change is a deliberate
    decision, not an accident.
    """
    _, edges, _, _ = _scan("default_import.ts")
    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert delegates == []


# ---------------------------------------------------------------------------
# US-3: full 7-variant non-literal Step matrix
# ---------------------------------------------------------------------------


def test_negative_step_variants_emit_findings_and_subsequent_step_extracts():
    """Each enumerated non-literal Step variant emits one IMPORTANT finding
    with rule_id JS-LIT-STEP, no step node is created for the offending
    call, and the trailing valid `Step({stepNum: 8, ...})` still extracts.

    Variants exercised in `negative_step_variants.ts`:
      1. Step(opts)              -- identifier
      2. Step({...spread})       -- spread
      3. Step(buildOpts())       -- function call result
      4. Step(true ? a : b)      -- ternary
      5. Step(this.opts)         -- member expression (via Holder.fire)
      6. Step()                  -- missing arg
      7. Step(opts, base)        -- multi-arg

    Note: the Holder.fire method body in the fixture also contains a
    `Step(this.opts)` call.  That class-method body is NOT walked by the
    envelope step extractor (which only descends through the wrapped
    workflow function), so it does not emit a JS-LIT-STEP finding here.
    The 6 variants invoked inside `run` cover the in-envelope contract,
    while the standalone class method demonstrates the no-crash path.
    """
    nodes, _, findings, _ = _scan("negative_step_variants.ts")

    lit_step = [f for f in findings if f.severity == "IMPORTANT" and f.rule_id == "JS-LIT-STEP"]
    # Six in-envelope variants (1, 2, 3, 4, 6, 7) each emit one finding.
    assert len(lit_step) == 6
    for f in lit_step:
        assert f.module.endswith("negative_step_variants.ts")
        assert f.line >= 1
        assert f.message  # non-empty violation/fix-hint message

    # No step-N node was created for the rejected variants.  Only the
    # trailing valid `Step({stepNum: 8, ...})` should appear as a step node.
    step_nodes = [n for n in nodes if getattr(n, "subtype", None) == "step"]
    assert len(step_nodes) == 1
    assert step_nodes[0].dflow_meta["step_num_raw"] == "8"
    assert step_nodes[0].dflow_meta["name"] == "ok"
