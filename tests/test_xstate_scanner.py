"""Tests for the xstate v5 state-machine scanner.

Covers the user stories from the PEV pitch:
    US-1: Machine envelope visible to cortex tools
    US-2: State tree renders the lifecycle
    US-3: Transitions as first-class graph edges with event metadata
    US-4: Cross-module bridges show up in the graph
    US-5: Strict-literal contract — no silent extraction failures

Tier 2 unit tests + Tier 3 build-integration tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom_annotations import AutoStep, Step, workflow

from axiom_graph.scanners.xstate_scanner import HAS_TREE_SITTER, scan_xstate_module

pytestmark = pytest.mark.skipif(not HAS_TREE_SITTER, reason="tree-sitter not installed (pip install axiom-graph[js])")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_fixture(tmp_path: Path, content: str, name: str = "machine.ts") -> Path:
    """Write *content* to ``{tmp_path}/{name}`` and return the Path."""
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


def _scan(tmp_path: Path, content: str, name: str = "machine.ts"):
    """Run the scanner against an inline fixture string.

    Returns ``(nodes, edges, findings)`` for assertion convenience.
    """
    f = _write_fixture(tmp_path, content, name)
    findings: list = []
    nodes, edges = scan_xstate_module(f, tmp_path, "proj", findings_out=findings)
    return nodes, edges, findings


def _envelope(nodes):
    """Return the single state_machine envelope node, asserting one exists."""
    envs = [n for n in nodes if n.subtype == "state_machine"]
    assert len(envs) == 1, f"expected 1 state_machine envelope, got {len(envs)}"
    return envs[0]


def _states(nodes):
    """Return all nodes whose subtype is 'state'."""
    return [n for n in nodes if n.subtype == "state"]


def _delegates_to_edges(edges):
    return [e for e in edges if e.edge_type == "delegates_to"]


def _composes_edges(edges):
    return [e for e in edges if e.edge_type == "composes"]


# ---------------------------------------------------------------------------
# US-1: Machine envelope visible
# ---------------------------------------------------------------------------


@workflow(purpose="US-1: createMachine emits one envelope per machine")
def test_simple_create_machine_envelope(tmp_path):
    Step(step_num=1, name="scan flat machine", purpose="single createMachine with three flat states")
    src = """
import { createMachine } from 'xstate';

export const m = createMachine({
  id: 'lights',
  initial: 'green',
  meta: { purpose: 'Traffic light controller' },
  states: {
    green: { meta: { purpose: 'Go' } },
    yellow: { meta: { purpose: 'Slow down' } },
    red: { meta: { purpose: 'Stop' } },
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    env = _envelope(nodes)
    assert env.id.endswith("lights@machine")
    assert env.subtype == "state_machine"
    assert "Traffic light controller" in (env.level_1 or "")
    states = _states(nodes)
    assert len(states) == 3
    paths = sorted(n.dflow_meta["xstate_path"] for n in states)
    assert paths == ["green", "red", "yellow"]
    composes = _composes_edges(edges)
    # 1 module->envelope + 3 envelope->state
    composes_to_states = [e for e in composes if e.from_id == env.id]
    assert len(composes_to_states) == 3


@workflow(purpose="US-1: setup({actors}).createMachine({...}) is detected as state_machine")
def test_setup_create_machine(tmp_path):
    src = """
import { setup, fromPromise } from 'xstate';
import { fetchData } from './api';

export const m = setup({
  actors: {
    fetchData: fromPromise(fetchData),
  },
}).createMachine({
  id: 'fetcher',
  initial: 'idle',
  states: { idle: {} },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    env = _envelope(nodes)
    assert env.id.endswith("fetcher@machine")


@workflow(purpose="US-1: const-binding name fallback when no `id` field")
def test_const_binding_name_fallback(tmp_path):
    src = """
import { createMachine } from 'xstate';

const pipelineRunActor = createMachine({
  initial: 'idle',
  states: { idle: {} },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    env = _envelope(nodes)
    assert env.id.endswith("pipelineRunActor@machine")


# ---------------------------------------------------------------------------
# US-2: State tree
# ---------------------------------------------------------------------------


@workflow(purpose="US-2: compound state renders parent composite + nested atomic children")
def test_compound_state_tree(tmp_path):
    src = """
import { createMachine } from 'xstate';

export const m = createMachine({
  id: 'm',
  initial: 'outer',
  states: {
    outer: {
      initial: 'a',
      states: {
        a: {},
        b: {},
        c: {},
      },
    },
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    states = _states(nodes)
    paths = sorted(n.dflow_meta["xstate_path"] for n in states)
    assert paths == ["outer", "outer.a", "outer.b", "outer.c"]
    # Outer must be composite_process with subtype state.
    outer = next(n for n in states if n.dflow_meta["xstate_path"] == "outer")
    assert outer.node_type == "composite_process"
    leaf_a = next(n for n in states if n.dflow_meta["xstate_path"] == "outer.a")
    assert leaf_a.node_type == "atomic_process"
    # composes edge: outer -> outer.a
    composes = _composes_edges(edges)
    assert any(e.from_id == outer.id and e.to_id == leaf_a.id for e in composes)


@workflow(purpose="US-2: meta.purpose populates level_1 / level_2")
def test_state_meta_purpose(tmp_path):
    src = """
import { createMachine } from 'xstate';

export const m = createMachine({
  id: 'm',
  states: {
    s1: { meta: { purpose: 'Initial state' } },
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    states = _states(nodes)
    s1 = next(n for n in states if n.dflow_meta["xstate_path"] == "s1")
    assert "Initial state" in (s1.level_1 or "")
    assert s1.level_2 == "Initial state"


@workflow(purpose="US-2: type='final' is tagged terminal")
def test_terminal_state(tmp_path):
    src = """
import { createMachine } from 'xstate';

export const m = createMachine({
  id: 'm',
  states: {
    done: { type: 'final' },
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    s = next(n for n in _states(nodes) if n.dflow_meta["xstate_path"] == "done")
    assert "final" in (s.tags or [])
    assert s.dflow_meta["terminal"] is True


# ---------------------------------------------------------------------------
# US-3: Transitions
# ---------------------------------------------------------------------------


@workflow(purpose="US-3: 'on' handlers emit delegates_to with meta event/via")
def test_on_transitions(tmp_path):
    src = """
import { createMachine } from 'xstate';

export const m = createMachine({
  id: 'm',
  initial: 'idle',
  states: {
    idle: {
      on: {
        RUN_CLICKED: 'preflight',
        INVALID: 'idle',
      },
    },
    preflight: {},
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    deleg = _delegates_to_edges(edges)
    metas = [(e.meta or {}) for e in deleg]
    events = sorted((m.get("event") or "") for m in metas)
    assert events == ["INVALID", "RUN_CLICKED"]
    for m in metas:
        assert m.get("via") == "on"


@workflow(purpose="US-3: 'always' and 'after' transitions emit correct meta")
def test_always_and_after_transitions(tmp_path):
    src = """
import { createMachine } from 'xstate';

export const m = createMachine({
  id: 'm',
  initial: 'a',
  states: {
    a: {
      always: { target: 'b' },
    },
    b: {
      after: { 1000: 'c' },
    },
    c: {},
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    deleg = _delegates_to_edges(edges)
    vias = sorted((e.meta or {}).get("via") for e in deleg)
    assert "always" in vias
    assert "after" in vias
    after_edge = next(e for e in deleg if (e.meta or {}).get("via") == "after")
    assert after_edge.meta["delay"] == "1000"


@workflow(purpose="US-3: long-form transition with internal:true captures internal in meta")
def test_long_form_internal_transition(tmp_path):
    src = """
import { createMachine } from 'xstate';

export const m = createMachine({
  id: 'm',
  states: {
    a: {
      on: {
        EVT: { target: 'b', internal: true },
      },
    },
    b: {},
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    deleg = _delegates_to_edges(edges)
    e = next(x for x in deleg if (x.meta or {}).get("event") == "EVT")
    assert e.meta.get("internal") is True


@workflow(purpose="US-3: existing make_edge call sites without meta= round-trip unchanged")
def test_make_edge_backward_compat():
    Step(step_num=1, name="call without meta", purpose="ensure existing callers still work")
    from axiom_graph.models import make_edge

    e = make_edge("composes", "a", "b")
    assert e.meta is None
    assert e.id == "a::composes::b"
    e2 = make_edge("composes", "a", "b", meta={"x": 1})
    assert e2.meta == {"x": 1}


# ---------------------------------------------------------------------------
# US-4: Cross-module / spawn
# ---------------------------------------------------------------------------


@workflow(purpose="US-4: setup actors map → imported identifier → cross-module delegates_to")
def test_setup_actors_invoke_resolution(tmp_path):
    # Sibling module providing the actor implementation.
    api = tmp_path / "api.ts"
    api.write_text(
        "export function submitPipeline() { return Promise.resolve(); }\n",
        encoding="utf-8",
    )
    src = """
import { setup, fromPromise } from 'xstate';
import { submitPipeline } from './api';

export const m = setup({
  actors: {
    submit: fromPromise(submitPipeline),
  },
}).createMachine({
  id: 'm',
  initial: 'submitting',
  states: {
    submitting: {
      invoke: { src: 'submit' },
    },
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    deleg = _delegates_to_edges(edges)
    # Find the invoke edge — meta.via == "invoke".
    inv = [e for e in deleg if (e.meta or {}).get("via") == "invoke"]
    assert len(inv) == 1
    assert inv[0].to_id.endswith("::submitPipeline")


@workflow(purpose="US-4: spawn(importedMachine) emits composes between machine envelopes")
def test_spawn_composes_between_machines(tmp_path):
    other = tmp_path / "other.ts"
    other.write_text(
        """
import { createMachine } from 'xstate';
export const childMachine = createMachine({ id: 'child', states: {} });
""",
        encoding="utf-8",
    )
    src = """
import { createMachine, spawn } from 'xstate';
import { childMachine } from './other';

export const parent = createMachine({
  id: 'parent',
  states: {
    running: {
      entry: () => spawn(childMachine),
    },
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    parent_env = next(n for n in nodes if n.id.endswith("parent@machine"))
    composes = _composes_edges(edges)
    # parent envelope should compose into "{other_module_id}::childMachine@machine"
    targets = [e.to_id for e in composes if e.from_id == parent_env.id]
    assert any(t.endswith("childMachine@machine") for t in targets)


@workflow(purpose="US-4: invoke.src not in actors map -> finding + <unresolved> edge")
def test_unknown_invoke_src_emits_finding(tmp_path):
    src = """
import { setup } from 'xstate';

export const m = setup({
  actors: {},
}).createMachine({
  id: 'm',
  states: {
    a: {
      invoke: { src: 'missing' },
    },
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    assert any("missing" in f.message and f.severity == "IMPORTANT" for f in findings)
    deleg = _delegates_to_edges(edges)
    assert any("<unresolved>" in e.to_id for e in deleg)


# ---------------------------------------------------------------------------
# US-5: Strict-literal contract
# ---------------------------------------------------------------------------


@workflow(purpose="US-5: createMachine(buildConfig()) emits finding, no envelope")
def test_create_machine_with_call_arg_emits_finding(tmp_path):
    src = """
import { createMachine } from 'xstate';
function buildConfig() { return { id: 'x', states: {} }; }
export const m = createMachine(buildConfig());
"""
    nodes, edges, findings = _scan(tmp_path, src)
    envs = [n for n in nodes if n.subtype == "state_machine"]
    assert envs == []
    assert any(f.severity == "IMPORTANT" for f in findings)


@workflow(purpose="US-5: states.{name} = variable emits finding for that key; siblings extracted")
def test_variable_state_value_emits_per_key_finding(tmp_path):
    src = """
import { createMachine } from 'xstate';
const someStateConfig = { meta: { purpose: 'x' } };
export const m = createMachine({
  id: 'm',
  states: {
    idle: someStateConfig,
    running: {},
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    states = _states(nodes)
    paths = sorted(n.dflow_meta["xstate_path"] for n in states)
    assert paths == ["running"]  # idle skipped
    assert any("idle" in f.message and f.severity == "IMPORTANT" for f in findings)


@workflow(purpose="US-5: state.on = bare identifier emits finding; no transition edges")
def test_state_on_variable_emits_finding(tmp_path):
    src = """
import { createMachine } from 'xstate';
const handlers = { CLICK: 'b' };
export const m = createMachine({
  id: 'm',
  initial: 'a',
  states: {
    a: { on: handlers },
    b: {},
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    # Finding emitted with rule_id X1 / IMPORTANT.
    assert any(f.rule_id == "X1" and f.severity == "IMPORTANT" and "on" in f.message for f in findings)
    # No transition edge produced from 'a' via 'on'.
    deleg = _delegates_to_edges(edges)
    a_node = next(n for n in _states(nodes) if n.dflow_meta["xstate_path"] == "a")
    on_edges_from_a = [e for e in deleg if e.from_id == a_node.id and (e.meta or {}).get("via") == "on"]
    assert on_edges_from_a == []


@workflow(purpose="US-5: state.always = bare identifier emits finding; no via='always' edge")
def test_state_always_variable_emits_finding(tmp_path):
    src = """
import { createMachine } from 'xstate';
const cond = { target: 'b' };
export const m = createMachine({
  id: 'm',
  initial: 'a',
  states: {
    a: { always: cond },
    b: {},
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    assert any(f.rule_id == "X1" and f.severity == "IMPORTANT" and "always" in f.message for f in findings)
    deleg = _delegates_to_edges(edges)
    a_node = next(n for n in _states(nodes) if n.dflow_meta["xstate_path"] == "a")
    always_edges = [e for e in deleg if e.from_id == a_node.id and (e.meta or {}).get("via") == "always"]
    assert always_edges == []


@workflow(purpose="US-5: state.after = bare identifier emits finding; no via='after' edge")
def test_state_after_variable_emits_finding(tmp_path):
    src = """
import { createMachine } from 'xstate';
const delays = { 1000: 'b' };
export const m = createMachine({
  id: 'm',
  initial: 'a',
  states: {
    a: { after: delays },
    b: {},
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    assert any(f.rule_id == "X1" and f.severity == "IMPORTANT" and "after" in f.message for f in findings)
    deleg = _delegates_to_edges(edges)
    a_node = next(n for n in _states(nodes) if n.dflow_meta["xstate_path"] == "a")
    after_edges = [e for e in deleg if e.from_id == a_node.id and (e.meta or {}).get("via") == "after"]
    assert after_edges == []


@workflow(purpose="US-5: invoke.src = bare identifier emits finding; no invoke edge")
def test_invoke_src_non_string_emits_finding(tmp_path):
    src = """
import { createMachine } from 'xstate';
const someVariable = 'submit';
export const m = createMachine({
  id: 'm',
  initial: 'a',
  states: {
    a: {
      invoke: { src: someVariable },
    },
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    assert any(f.rule_id == "X1" and f.severity == "IMPORTANT" and "src" in f.message for f in findings)
    deleg = _delegates_to_edges(edges)
    invoke_edges = [e for e in deleg if (e.meta or {}).get("via") == "invoke"]
    assert invoke_edges == []


@workflow(purpose="US-5: after.{delay} inner-value bare identifier emits finding; no via='after' edge")
def test_state_after_inner_value_variable_emits_finding(tmp_path):
    src = """
import { createMachine } from 'xstate';
const someVar = 'b';
export const m = createMachine({
  id: 'm',
  initial: 'a',
  states: {
    a: { after: { 1000: someVar } },
    b: {},
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    # X1 finding emitted for the non-literal delay value.
    assert any(f.rule_id == "X1" and f.severity == "IMPORTANT" and "after" in f.message for f in findings), (
        f"expected X1 IMPORTANT finding mentioning 'after'; got {findings}"
    )
    # No via='after' edge for this delay key.
    deleg = _delegates_to_edges(edges)
    a_node = next(n for n in _states(nodes) if n.dflow_meta["xstate_path"] == "a")
    after_edges = [e for e in deleg if e.from_id == a_node.id and (e.meta or {}).get("via") == "after"]
    assert after_edges == []


@workflow(purpose="US-5: always:[identifier, ...] emits per-element finding; literals still produce edges")
def test_state_always_array_element_variable_emits_finding(tmp_path):
    src = """
import { createMachine } from 'xstate';
const someVar = 'idle';
export const m = createMachine({
  id: 'm',
  initial: 'a',
  states: {
    a: { always: ['idle', someVar, { target: 'next' }] },
    idle: {},
    next: {},
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    # X1 finding emitted for the bare-identifier array element.
    assert any(f.rule_id == "X1" and f.severity == "IMPORTANT" and "always" in f.message for f in findings), (
        f"expected X1 IMPORTANT finding mentioning 'always'; got {findings}"
    )
    # The valid 'idle' string and {target:'next'} object elements still
    # produce their edges; only the identifier element is dropped.
    deleg = _delegates_to_edges(edges)
    a_node = next(n for n in _states(nodes) if n.dflow_meta["xstate_path"] == "a")
    always_edges = [e for e in deleg if e.from_id == a_node.id and (e.meta or {}).get("via") == "always"]
    targets = {e.to_id.rsplit(".", 1)[-1] for e in always_edges}
    assert "idle" in targets
    assert "next" in targets
    # Exactly two valid edges (the identifier element produced none).
    assert len(always_edges) == 2, f"expected 2 always edges (idle + next); got {len(always_edges)}: {targets}"


@workflow(
    purpose="US-5: invoke:[identifier, ...] array form emits per-element finding; valid object element still produces edge"
)
def test_invoke_array_element_variable_emits_finding(tmp_path):
    src = """
import { setup, fromPromise } from 'xstate';
import { submitPipeline } from './api';

export const m = setup({
  actors: {
    submit: fromPromise(submitPipeline),
  },
}).createMachine({
  id: 'm',
  initial: 'a',
  states: {
    a: {
      invoke: [{ src: 'submit' }, someVar, 'b'],
    },
  },
});
"""
    api = tmp_path / "api.ts"
    api.write_text(
        "export function submitPipeline() { return Promise.resolve(); }\n",
        encoding="utf-8",
    )
    nodes, edges, findings = _scan(tmp_path, src)
    # X1 finding emitted for the bare-identifier array element.
    assert any(f.rule_id == "X1" and f.severity == "IMPORTANT" and "invoke" in f.message for f in findings), (
        f"expected X1 IMPORTANT finding mentioning 'invoke'; got {findings}"
    )
    # The valid object element {src:'submit'} still produces its invoke edge;
    # only the identifier element is dropped.  (Strings have no inner pairs
    # so they produce no edges either, matching the ``always`` contract.)
    deleg = _delegates_to_edges(edges)
    invoke_edges = [e for e in deleg if (e.meta or {}).get("via") == "invoke"]
    assert len(invoke_edges) == 1, (
        f"expected 1 invoke edge from the valid object element; "
        f"got {len(invoke_edges)}: {[e.to_id for e in invoke_edges]}"
    )
    assert invoke_edges[0].to_id.endswith("::submitPipeline")


@workflow(purpose="US-5: invoke.onDone = bare identifier emits finding; no via='invoke.onDone' edge")
def test_invoke_on_done_non_literal_emits_finding(tmp_path):
    src = """
import { setup, fromPromise } from 'xstate';
import { submitPipeline } from './api';

export const m = setup({
  actors: {
    submit: fromPromise(submitPipeline),
  },
}).createMachine({
  id: 'm',
  initial: 'a',
  states: {
    a: {
      invoke: { src: 'submit', onDone: bareIdentifier },
    },
  },
});
"""
    api = tmp_path / "api.ts"
    api.write_text(
        "export function submitPipeline() { return Promise.resolve(); }\n",
        encoding="utf-8",
    )
    nodes, edges, findings = _scan(tmp_path, src)
    # X1 finding emitted mentioning onDone.
    assert any(f.rule_id == "X1" and f.severity == "IMPORTANT" and "onDone" in f.message for f in findings), (
        f"expected X1 IMPORTANT finding mentioning 'onDone'; got {findings}"
    )
    # No via='invoke.onDone' edge produced.
    deleg = _delegates_to_edges(edges)
    on_done_edges = [e for e in deleg if (e.meta or {}).get("via") == "invoke.onDone"]
    assert on_done_edges == [], f"expected no invoke.onDone edge; got {[e.to_id for e in on_done_edges]}"


@workflow(purpose="US-5: after.{computedKey: ...} (JS computed property key) emits finding; no via='after' edge")
def test_after_computed_key_emits_finding(tmp_path):
    src = """
import { createMachine } from 'xstate';
const computedDelay = 1000;
export const m = createMachine({
  id: 'm',
  initial: 'a',
  states: {
    a: { after: { [computedDelay]: 'next' } },
    next: {},
  },
});
"""
    nodes, edges, findings = _scan(tmp_path, src)
    # X1 finding emitted mentioning after.
    assert any(f.rule_id == "X1" and f.severity == "IMPORTANT" and "after" in f.message for f in findings), (
        f"expected X1 IMPORTANT finding mentioning 'after'; got {findings}"
    )
    # No via='after' edge produced for the computed-key pair.
    deleg = _delegates_to_edges(edges)
    a_node = next(n for n in _states(nodes) if n.dflow_meta["xstate_path"] == "a")
    after_edges = [e for e in deleg if e.from_id == a_node.id and (e.meta or {}).get("via") == "after"]
    assert after_edges == [], f"expected no after edge; got {[e.to_id for e in after_edges]}"


@workflow(purpose="US-5: file mixing workflow(opts)(fn) HOF and createMachine runs both scanners")
def test_coexistence_with_js_scanner(tmp_path):
    Step(step_num=1, name="set up file", purpose="single file containing both an HOF wrapper and a createMachine")
    Step(step_num=2, name="run both scanners", purpose="js_scanner + xstate_scanner produce independent outputs")
    src = """
import { workflow } from 'axiom-annotations';
import { createMachine } from 'xstate';

const myFn = workflow({purpose: 'p'})(function myFn() {});

export const m = createMachine({
  id: 'm',
  states: { idle: {} },
});
"""
    f = _write_fixture(tmp_path, src)
    findings: list = []
    xs_nodes, xs_edges = scan_xstate_module(f, tmp_path, "proj", findings_out=findings)
    # xstate scanner only emits machine + states, no module / function nodes.
    assert all(n.subtype in ("state_machine", "state") for n in xs_nodes)
    # And js_scanner runs cleanly on the same source (no crash).
    from axiom_graph.scanners.js_scanner import scan_js_module

    js_nodes, js_edges = scan_js_module(f, tmp_path, "proj")
    # No collision: state_machine node IDs end with @machine; HOF envelopes end with @workflow.
    js_ids = {n.id for n in js_nodes}
    xs_ids = {n.id for n in xs_nodes}
    assert not (js_ids & xs_ids)


# ---------------------------------------------------------------------------
# Tier 3 — End-to-end build integration
# ---------------------------------------------------------------------------


@workflow(purpose="US-2 / US-3 E2E: build emits StateMachineDetail; formatter renders EVENT -> target")
def test_e2e_build_and_workflow_detail(tmp_path):
    Step(step_num=1, name="prepare project", purpose="write minimal axiom-graph project with one xstate file")
    Step(step_num=2, name="run build", purpose="full builder.build() pass over project")
    Step(step_num=3, name="query workflow_detail", purpose="StateMachineDetail returned with transitions")
    Step(step_num=4, name="render via MCP tool", purpose="formatter emits EVENT -> target with no step_num prefix")
    # Set up a minimal project layout the builder can scan.
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\njs_paths = ["src/**/*.ts"]\n',
        encoding="utf-8",
    )
    src_dir = proj / "src"
    src_dir.mkdir()
    (src_dir / "machine.ts").write_text(
        """
import { createMachine } from 'xstate';

export const m = createMachine({
  id: 'lights',
  initial: 'green',
  meta: { purpose: 'Traffic controller' },
  states: {
    green: { on: { TICK: 'yellow' } },
    yellow: { on: { TICK: 'red' } },
    red: { on: { TICK: 'green' }, type: 'final' },
  },
});
""",
        encoding="utf-8",
    )

    from axiom_graph.index import builder as ag_builder

    AutoStep(step_num=2, name="build")
    summary = ag_builder.build(proj, project_id="proj", discovery_only=False)
    assert summary["nodes_written"] >= 4  # module + envelope + 3 states (at least)

    from axiom_graph.workflows.api import (
        StateMachineDetail,
        workflow_detail,
    )

    detail = workflow_detail(proj, "lights")
    assert isinstance(detail, StateMachineDetail)
    assert detail.role == "state_machine"
    paths = sorted(s.path for s in detail.states)
    assert paths == ["green", "red", "yellow"]
    # Transitions present
    green = next(s for s in detail.states if s.path == "green")
    assert any(t.event == "TICK" and t.via == "on" for t in green.transitions)
    red = next(s for s in detail.states if s.path == "red")
    assert red.is_terminal

    # MCP formatter renders without step_num prefix.
    from axiom_graph.workflows.mcp_tools import axiom_graph_workflow_detail

    out = axiom_graph_workflow_detail(str(proj), "lights")
    assert "TICK" in out
    assert "→" in out
    # No "1." or "2." numbered step lines preceding the state path.
    for line in out.splitlines():
        stripped = line.strip()
        if stripped and stripped[0].isdigit() and stripped[1:2] == ".":
            pytest.fail(f"State machine output should not use step_num prefix: {line!r}")
