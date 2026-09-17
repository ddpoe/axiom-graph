"""Phase 3 workflow expansion renderer tests.

Mapped to user story US-6: flat, one-level, two-level, loop-inside, cycle,
non-annotated target.  See
``axiom_graph::docs.pev.cycles.pev-2026-04-21-phase3-axiom-annotations``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from axiom_annotations import workflow

from axiom_graph.models import AxiomNode, make_edge
from axiom_graph.workflows.api import (
    WorkflowGraph,
    build_workflow_graph,
    step_delegate_target,
    workflow_detail,
    workflow_expanded_steps,
)
from axiom_graph.index import builder


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _build(project_root: Path) -> None:
    """Run axiom-graph build with embeddings skipped."""
    import os

    os.environ["AXIOM_GRAPH_SKIP_EMBEDDINGS"] = "1"
    builder.build(project_root, project_id="proj")


@workflow(purpose="Expansion renders flat workflow without renumbering")
def test_expansion_flat(tmp_path):
    _write(
        tmp_path / "pipe.py",
        """
from axiom_annotations import workflow, Step

@workflow(purpose="flat")
def F():
    口 = Step(step_num=1, name="a", purpose="p")
    口 = Step(step_num=2, name="b", purpose="p")
    口 = Step(step_num=3, name="c", purpose="p")
""".lstrip(),
    )
    _build(tmp_path)
    out = workflow_expanded_steps(tmp_path, "proj::pipe::F@workflow")
    assert [e.rendered_step_num for e in out] == ["1", "2", "3"]


@workflow(purpose="Expansion renumbers one-level nested AutoStep chain")
def test_expansion_one_level(tmp_path):
    _write(
        tmp_path / "tasks.py",
        """
from axiom_annotations import task, Step

@task(purpose="inner")
def inner():
    口 = Step(step_num=1, name="i1", purpose="p")
    口 = Step(step_num=2, name="i2", purpose="p")
""".lstrip(),
    )
    _write(
        tmp_path / "pipe.py",
        """
from axiom_annotations import workflow, AutoStep
from tasks import inner

@workflow(purpose="outer")
def outer():
    口 = AutoStep(step_num=3, name="call-inner")
    inner()
""".lstrip(),
    )
    _build(tmp_path)
    out = workflow_expanded_steps(tmp_path, "proj::pipe::outer@workflow")
    rendered = [e.rendered_step_num for e in out]
    # Outer AutoStep "3" comes first, then 3.1 and 3.2.
    assert rendered[0] == "3"
    assert "3.1" in rendered and "3.2" in rendered


@workflow(purpose="Expansion renders two-level nested chain 3 → 2 → 1 as 3.2.1")
def test_expansion_two_level(tmp_path):
    _write(
        tmp_path / "innermost.py",
        """
from axiom_annotations import task, Step

@task(purpose="innermost")
def innermost():
    口 = Step(step_num=1, name="leaf", purpose="p")
""".lstrip(),
    )
    _write(
        tmp_path / "middle.py",
        """
from axiom_annotations import task, AutoStep
from innermost import innermost

@task(purpose="middle")
def middle():
    口 = AutoStep(step_num=2, name="call-leaf")
    innermost()
""".lstrip(),
    )
    _write(
        tmp_path / "outer.py",
        """
from axiom_annotations import workflow, AutoStep
from middle import middle

@workflow(purpose="outer")
def outer():
    口 = AutoStep(step_num=3, name="call-middle")
    middle()
""".lstrip(),
    )
    _build(tmp_path)
    out = workflow_expanded_steps(tmp_path, "proj::outer::outer@workflow")
    rendered = [e.rendered_step_num for e in out]
    assert "3.2.1" in rendered, rendered


@workflow(purpose="Cycle detected at re-entry emits a note and stops expansion")
def test_expansion_cycle(tmp_path):
    _write(
        tmp_path / "a.py",
        """
from axiom_annotations import task, AutoStep
from b import B

@task(purpose="A")
def A():
    口 = AutoStep(step_num=1, name="to-b")
    B()
""".lstrip(),
    )
    _write(
        tmp_path / "b.py",
        """
from axiom_annotations import task, AutoStep
from a import A

@task(purpose="B")
def B():
    口 = AutoStep(step_num=1, name="back-to-a")
    A()
""".lstrip(),
    )
    _build(tmp_path)
    out = workflow_expanded_steps(tmp_path, "proj::a::A@workflow")
    notes = [e.note for e in out if e.note]
    assert any("cycle detected" in (n or "") for n in notes)


@workflow(purpose="Non-annotated delegation target emits a note instead of raising")
def test_expansion_non_annotated_target(tmp_path):
    _write(
        tmp_path / "helpers.py",
        """
def helper():
    return 1
""".lstrip(),
    )
    _write(
        tmp_path / "pipe.py",
        """
from axiom_annotations import workflow, AutoStep
from helpers import helper

@workflow(purpose="delegates to non-annotated helper")
def run():
    口 = AutoStep(step_num=1, name="call-helper")
    helper()
""".lstrip(),
    )
    _build(tmp_path)
    out = workflow_expanded_steps(tmp_path, "proj::pipe::run@workflow")
    assert any(e.note == "target not annotated" for e in out)


@workflow(purpose="Loop-inside minor step 2.1 under outer 3 renders as 3.2.1")
def test_expansion_loop_inside(tmp_path):
    _write(
        tmp_path / "inner.py",
        """
from axiom_annotations import task, Step

@task(purpose="inner has minor step inside a for loop")
def inner():
    for i in range(2):
        口 = Step(step_num="2.1", name="loop-step", purpose="p")
""".lstrip(),
    )
    _write(
        tmp_path / "outer.py",
        """
from axiom_annotations import workflow, AutoStep
from inner import inner

@workflow(purpose="outer")
def outer():
    口 = AutoStep(step_num=3, name="call-inner")
    inner()
""".lstrip(),
    )
    _build(tmp_path)
    out = workflow_expanded_steps(tmp_path, "proj::outer::outer@workflow")
    rendered = [e.rendered_step_num for e in out]
    assert "3.2.1" in rendered, rendered


# ---------------------------------------------------------------------------
# Delegate resolution
# ---------------------------------------------------------------------------


def _bare_node(node_id: str, *, subtype: str) -> AxiomNode:
    """Build the minimum node an in-memory workflow graph needs."""
    short = node_id.rsplit("::", 1)[-1]
    return AxiomNode(
        id=node_id,
        node_type="atomic_process",
        title=short,
        location="pipe.py",
        source="ast",
        code_hash="0" * 16,
        level_0=short,
        level_1="",
        subtype=subtype,
    )


def test_delegate_target_is_the_smallest_target_id_when_several_are_recorded():
    """A step naming several callees resolves to one, chosen independently of storage order."""
    step = _bare_node("proj::pipe::run.step-1", subtype="autostep")
    forward = WorkflowGraph(
        nodes_by_id={step.id: step},
        delegates_out={step.id: ["proj::zed::zeta", "proj::abc::alpha"]},
    )
    reversed_order = WorkflowGraph(
        nodes_by_id={step.id: step},
        delegates_out={step.id: ["proj::abc::alpha", "proj::zed::zeta"]},
    )

    assert step_delegate_target(step.id, forward).id == "proj::abc::alpha"
    assert step_delegate_target(step.id, reversed_order).id == "proj::abc::alpha"


def test_delegate_map_holds_step_sources_only():
    """State transitions reuse the delegates_to edge type but are not step delegation."""
    step = _bare_node("proj::pipe::run.step-1", subtype="autostep")
    state = _bare_node("proj::ui::machine.idle", subtype="state")
    edges = [
        make_edge("delegates_to", state.id, "proj::ui::machine.running"),
        make_edge("delegates_to", step.id, "proj::tasks::inner"),
    ]

    graph = build_workflow_graph({step.id: step, state.id: state}, edges)

    assert graph.delegates_out == {step.id: ["proj::tasks::inner"]}
    assert step_delegate_target(state.id, graph) is None


@pytest.mark.parametrize(
    "target_decorator, expected_inputs, expected_outputs",
    [
        (
            '@task(purpose="Assemble the protein table", inputs="a cache path",'
            ' outputs="a tidy frame", critical="the cache must already exist")',
            "a cache path",
            "a tidy frame",
        ),
        (
            '@workflow(purpose="Assemble the protein table", critical="the cache must already exist")',
            "",
            "",
        ),
    ],
    ids=["task-target", "workflow-target"],
)
@workflow(purpose="A delegating step reports its target's identity, file, line and declared intent")
def test_delegating_step_reports_its_target_and_the_intent_it_declares(
    tmp_path, target_decorator, expected_inputs, expected_outputs
):
    _write(
        tmp_path / "tasks.py",
        f"""
from axiom_annotations import task, workflow, Step

{target_decorator}
def build_proteins():
    口 = Step(step_num=1, name="locate cache", purpose="find the upstream cache")
""".lstrip(),
    )
    _write(
        tmp_path / "pipe.py",
        """
from axiom_annotations import workflow, Step, AutoStep
from tasks import build_proteins

@workflow(purpose="Build plot data")
def build_plot_data():
    口 = Step(step_num=1, name="load inputs", purpose="read the source frame",
             inputs="a csv path", outputs="a raw frame")
    口 = AutoStep(step_num=2, name="bootstrap")
    build_proteins()
""".lstrip(),
    )
    _build(tmp_path)

    detail = workflow_detail(tmp_path, "proj::pipe::build_plot_data@workflow")
    rows = {row.step_num: row for row in detail.steps}

    delegating = rows["2"]
    assert delegating.delegates_to_node_id == "proj::tasks::build_proteins"
    assert delegating.target.id == "proj::tasks::build_proteins"
    assert delegating.target.location == "tasks.py"
    assert delegating.target.line == 3
    assert delegating.purpose == "Assemble the protein table"
    assert delegating.inputs == expected_inputs
    assert delegating.outputs == expected_outputs
    assert delegating.critical == "the cache must already exist"
    assert delegating.location == "pipe.py"

    authored = rows["1"]
    assert authored.target is None
    assert authored.purpose == "read the source frame"
    assert authored.inputs == "a csv path"
    assert authored.outputs == "a raw frame"


@workflow(
    purpose="Steps whose target declares nothing, is absent from the index, or that call nothing,"
    " stay empty rather than raising"
)
def test_step_intent_stays_empty_when_there_is_nothing_to_inherit(tmp_path):
    _write(
        tmp_path / "helpers.py",
        """
def plain_helper():
    return 1
""".lstrip(),
    )
    _write(
        tmp_path / "pipe.py",
        """
from axiom_annotations import workflow, Step, AutoStep
from helpers import plain_helper

@workflow(purpose="Build plot data")
def build_plot_data():
    口 = Step(step_num=1, name="load inputs")
    口 = AutoStep(step_num=2, name="call an undecorated helper")
    plain_helper()
    口 = AutoStep(step_num=3, name="call nothing at all")
""".lstrip(),
    )
    _build(tmp_path)

    rows = {row.step_num: row for row in workflow_detail(tmp_path, "proj::pipe::build_plot_data@workflow").steps}

    undecorated = rows["2"]
    assert undecorated.target is not None
    assert undecorated.target.id == "proj::helpers::plain_helper"
    assert (undecorated.purpose, undecorated.inputs, undecorated.outputs, undecorated.critical) == ("", "", "", "")

    calls_nothing = rows["3"]
    assert calls_nothing.target is None
    assert (calls_nothing.purpose, calls_nothing.inputs, calls_nothing.outputs, calls_nothing.critical) == (
        "",
        "",
        "",
        "",
    )

    plain_step = rows["1"]
    assert plain_step.target is None
    assert plain_step.delegates_to_node_id is None

    # Third shape: the step names a callee the index does not hold.  The
    # target is still reported by id and name so the surface can say what
    # was called, but nothing is inherited and nothing raises.
    dangling_step = _bare_node("proj::pipe::build_plot_data.step-4", subtype="autostep")
    dangling = step_delegate_target(
        dangling_step.id,
        WorkflowGraph(
            nodes_by_id={dangling_step.id: dangling_step},
            delegates_out={dangling_step.id: ["proj::gone::vanished_helper"]},
        ),
    )
    assert dangling is not None
    assert (dangling.id, dangling.name) == ("proj::gone::vanished_helper", "vanished_helper")
    assert (dangling.location, dangling.line) == ("", 0)
    assert (dangling.purpose, dangling.inputs, dangling.outputs, dangling.critical) == ("", "", "", "")
