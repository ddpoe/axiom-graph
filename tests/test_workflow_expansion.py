"""Phase 3 workflow expansion renderer tests.

Mapped to user story US-6: flat, one-level, two-level, loop-inside, cycle,
non-annotated target.  See
``axiom_graph::docs.pev.cycles.pev-2026-04-21-phase3-axiom-annotations``.
"""

from __future__ import annotations

from pathlib import Path

from axiom_annotations import workflow

from axiom_graph.workflows.api import workflow_expanded_steps
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
