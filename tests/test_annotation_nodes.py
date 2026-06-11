"""Phase 3 annotation-node scanner tests.

Covers edge emission (composes, annotates, delegates_to), envelope-hash
isolation, step-no-staleness invariant, no-dedup, step_num_raw vs parts
preservation, minor-step-outside-loop WARNING, and AutoStep-without-task
negative case.

Mapped to user stories US-1, US-2, US-4 in
``axiom_graph::docs.pev.cycles.pev-2026-04-21-phase3-axiom-annotations``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from axiom_annotations import workflow

from axiom_graph.scanners import module_scanner


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# US-1: envelope + composes + annotates
# ---------------------------------------------------------------------------


@workflow(purpose="Envelope emits composes and annotates edges from @workflow")
def test_envelope_edges_emitted(tmp_path):
    """A @workflow on def F emits envelope + composes + annotates."""
    f = _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow

@workflow(purpose="Build the thing")
def build():
    pass
""".lstrip(),
    )
    nodes, edges = module_scanner.scan_module(f, tmp_path, "proj")

    env_id = "proj::m::build@workflow"
    func_id = "proj::m::build"
    env = [n for n in nodes if n.id == env_id]
    assert env, "envelope node missing"
    assert env[0].node_type == "composite_process"
    assert env[0].subtype == "workflow"

    composes = [e for e in edges if e.edge_type == "composes" and e.to_id == env_id]
    assert len(composes) == 1 and composes[0].from_id == "proj::m"

    annotates = [e for e in edges if e.edge_type == "annotates"]
    assert any(e.from_id == env_id and e.to_id == func_id for e in annotates)


@workflow(purpose="Envelope hash isolation — kwargs drive envelope.own_status only")
def test_envelope_hash_isolation(tmp_path):
    """Editing only decorator kwargs flips envelope hash but not function hash."""
    text_before = """
from axiom_annotations import workflow

@workflow(purpose="Original purpose")
def build():
    return 1
""".lstrip()
    text_after = """
from axiom_annotations import workflow

@workflow(purpose="Different purpose")
def build():
    return 1
""".lstrip()
    f = _write(tmp_path / "m.py", text_before)
    nodes_a, _ = module_scanner.scan_module(f, tmp_path, "proj")
    _write(tmp_path / "m.py", text_after)
    nodes_b, _ = module_scanner.scan_module(f, tmp_path, "proj")

    env_a = next(n for n in nodes_a if n.id == "proj::m::build@workflow")
    env_b = next(n for n in nodes_b if n.id == "proj::m::build@workflow")
    func_a = next(n for n in nodes_a if n.id == "proj::m::build")
    func_b = next(n for n in nodes_b if n.id == "proj::m::build")

    assert env_a.code_hash != env_b.code_hash, "envelope hash should flip on kwarg edit"
    assert func_a.code_hash == func_b.code_hash, "function hash should NOT flip on kwarg edit"
    assert func_a.desc_hash == func_b.desc_hash
    # Envelope desc_hash is always NULL.
    assert env_a.desc_hash is None
    assert env_b.desc_hash is None


# ---------------------------------------------------------------------------
# US-2: step nodes invariants
# ---------------------------------------------------------------------------


@workflow(purpose="Step nodes emitted with NO staleness fields")
def test_step_nodes_have_no_staleness(tmp_path):
    """Each Step() call emits a step node with empty code_hash and NULL desc."""
    f = _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow, Step

@workflow(purpose="Has two steps")
def F():
    口 = Step(step_num=1, name="one", purpose="first step")
    口 = Step(step_num=2, name="two", purpose="second step")
""".lstrip(),
    )
    nodes, edges = module_scanner.scan_module(f, tmp_path, "proj")
    env_id = "proj::m::F@workflow"
    s1_id = "proj::m::F::step-1"
    s2_id = "proj::m::F::step-2"
    s1 = next((n for n in nodes if n.id == s1_id), None)
    s2 = next((n for n in nodes if n.id == s2_id), None)
    assert s1 is not None and s2 is not None
    assert s1.node_type == "atomic_process" and s1.subtype == "step"
    # Sentinel: empty code_hash, None desc_hash.
    assert s1.code_hash == "" and s1.desc_hash is None
    assert s2.code_hash == "" and s2.desc_hash is None

    # Each step has exactly one inbound composes from the envelope.
    composes_to_s1 = [e for e in edges if e.edge_type == "composes" and e.to_id == s1_id]
    composes_to_s2 = [e for e in edges if e.edge_type == "composes" and e.to_id == s2_id]
    assert len(composes_to_s1) == 1 and composes_to_s1[0].from_id == env_id
    assert len(composes_to_s2) == 1 and composes_to_s2[0].from_id == env_id
    # No annotates or documents edges touching step nodes.
    for e in edges:
        if e.edge_type in ("annotates", "documents"):
            assert e.from_id not in (s1_id, s2_id)
            assert e.to_id not in (s1_id, s2_id)


@workflow(purpose="Steps with identical kwargs across workflows do NOT dedup")
def test_no_step_dedup_across_workflows(tmp_path):
    """Two workflows each containing a Step with identical kwargs → two nodes."""
    f = _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow, Step

@workflow(purpose="one")
def F1():
    口 = Step(step_num=1, name="load", purpose="read file")

@workflow(purpose="two")
def F2():
    口 = Step(step_num=1, name="load", purpose="read file")
""".lstrip(),
    )
    nodes, _ = module_scanner.scan_module(f, tmp_path, "proj")
    assert any(n.id == "proj::m::F1::step-1" for n in nodes)
    assert any(n.id == "proj::m::F2::step-1" for n in nodes)


@workflow(purpose="step_num_raw vs step_num_parts preserve significance")
def test_step_num_dual_storage(tmp_path):
    """'1.10' and '1.1' are distinct; sort uses parts so 1.1 precedes 1.10."""
    f = _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow, Step

@workflow(purpose="minors")
def F():
    for i in range(2):
        口 = Step(step_num="1.10", name="ten", purpose="p")
        口 = Step(step_num="1.1", name="one", purpose="p")
""".lstrip(),
    )
    nodes, _ = module_scanner.scan_module(f, tmp_path, "proj")
    s10 = next(n for n in nodes if n.id == "proj::m::F::step-1.10")
    s1 = next(n for n in nodes if n.id == "proj::m::F::step-1.1")
    assert s10.dflow_meta["step_num_raw"] == "1.10"
    assert s10.dflow_meta["step_num_parts"] == [1, 10]
    assert s1.dflow_meta["step_num_raw"] == "1.1"
    assert s1.dflow_meta["step_num_parts"] == [1, 1]
    assert tuple(s1.dflow_meta["step_num_parts"]) < tuple(s10.dflow_meta["step_num_parts"])


@workflow(purpose="Minor step outside loop logs a WARNING and still emits node")
def test_minor_step_outside_loop_warning(tmp_path, caplog):
    """Minor N.M step not inside a for/while produces a WARNING but still emits."""
    f = _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow, Step

@workflow(purpose="has minor outside loop")
def F():
    口 = Step(step_num="2.1", name="bad", purpose="outside loop")
""".lstrip(),
    )
    with caplog.at_level(logging.WARNING, logger="axiom_graph.scanners.module_scanner"):
        nodes, _ = module_scanner.scan_module(f, tmp_path, "proj")
    assert any(n.id == "proj::m::F::step-2.1" for n in nodes)
    assert any("minor step" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# US-4: AutoStep + delegates_to
# ---------------------------------------------------------------------------


@workflow(purpose="AutoStep followed by @task call emits delegates_to")
def test_autostep_emits_delegates_to(tmp_path):
    """AutoStep, then direct call to an imported @task, emits a delegates_to."""
    _write(
        tmp_path / "tasks.py",
        """
from axiom_annotations import task

@task(purpose="does thing")
def do_it():
    return 1
""".lstrip(),
    )
    f = _write(
        tmp_path / "pipe.py",
        """
from axiom_annotations import workflow, AutoStep
from tasks import do_it

@workflow(purpose="calls do_it")
def run():
    口 = AutoStep(step_num=1, name="x")
    do_it()
""".lstrip(),
    )
    # Scan both files (tasks first, so name_map resolves).
    nodes_t, edges_t = module_scanner.scan_module(tmp_path / "tasks.py", tmp_path, "proj")
    nodes_p, edges_p = module_scanner.scan_module(f, tmp_path, "proj")
    delegates = [e for e in edges_p if e.edge_type == "delegates_to"]
    assert delegates, "expected a delegates_to edge from autostep"
    assert any(e.from_id == "proj::pipe::run::step-1" and e.to_id == "proj::tasks::do_it" for e in delegates)


@workflow(purpose="AutoStep without a following task call emits no delegates_to")
def test_autostep_no_call_emits_no_delegates(tmp_path):
    """AutoStep followed only by print(...) → step node emitted, no delegates_to."""
    f = _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow, AutoStep

@workflow(purpose="autostep without task")
def run():
    口 = AutoStep(step_num=1, name="x")
    print("hello")
""".lstrip(),
    )
    nodes, edges = module_scanner.scan_module(f, tmp_path, "proj")
    step_nodes = [n for n in nodes if n.subtype == "autostep"]
    assert step_nodes and step_nodes[0].id == "proj::m::run::step-1"
    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert not delegates
