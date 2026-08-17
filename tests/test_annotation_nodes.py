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


@workflow(purpose="AutoStep delegating to a same-file function emits delegates_to")
def test_autostep_emits_delegates_to_local_function(tmp_path):
    """AutoStep, then a bare call to a function defined later in the SAME file.

    The delegate is defined *after* the workflow, so resolution must see
    forward references — the local-function map is a whole-module pre-pass.
    """
    f = _write(
        tmp_path / "cli.py",
        """
from axiom_annotations import workflow, task, AutoStep

@workflow(purpose="runs a step")
def run_step():
    口 = AutoStep(step_num=6, name="Complete run")
    complete_run(run_id=1)

@task(purpose="finish the run")
def complete_run(run_id):
    return None
""".lstrip(),
    )
    nodes, edges = module_scanner.scan_module(f, tmp_path, "proj")
    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert delegates, "expected a delegates_to edge to the same-file function"
    assert any(e.from_id == "proj::cli::run_step::step-6" and e.to_id == "proj::cli::complete_run" for e in delegates)


@workflow(purpose="Step markers declared in nested blocks emit nodes like top-level ones")
def test_step_markers_in_nested_blocks_emit(tmp_path):
    """A marker in a loop, try, handler, else, or finally body emits its node.

    Nesting depth does not bear on emission — only the enclosing envelope does.
    """
    f = _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow, Step, AutoStep

@workflow(purpose="markers at every nesting shape")
def F(items):
    口 = Step(step_num=1, name="Prepare", purpose="read configuration")
    if items:
        口 = Step(step_num=2, name="Validate input", purpose="conditional phase")
    try:
        口 = Step(step_num=3, name="Open resource", purpose="try body")
    except ValueError:
        口 = Step(step_num=4, name="Recover", purpose="handler body")
    else:
        口 = Step(step_num=5, name="Confirm", purpose="else body")
    finally:
        口 = Step(step_num=6, name="Release", purpose="finally body")
    口 = Step(step_num=7, name="Process each item", purpose="loop over the batch")
    for it in items:
        口 = AutoStep(step_num=7.1, name="Validate one item")
        口 = AutoStep(step_num=7.2, name="Store one item")
    口 = Step(step_num=8, name="Drain queue", purpose="loop until empty")
    while items:
        口 = AutoStep(step_num=8.1, name="Pop one entry")
    口 = Step(step_num=9, name="Report", purpose="summarise the run")
""".lstrip(),
    )
    nodes, _ = module_scanner.scan_module(f, tmp_path, "proj")

    emitted = {n.id for n in nodes if n.subtype in ("step", "autostep")}
    expected = {f"proj::m::F::step-{num}" for num in ("1", "2", "3", "4", "5", "6", "7", "7.1", "7.2", "8", "8.1", "9")}
    assert expected <= emitted, f"missing: {sorted(expected - emitted)}"


@workflow(purpose="AutoStep inside a loop body emits delegates_to like a top-level one")
def test_autostep_in_loop_body_emits_delegates_to(tmp_path):
    """An AutoStep nested in a for body pairs with the next call for its edge."""
    f = _write(
        tmp_path / "cli.py",
        """
from axiom_annotations import workflow, task, Step, AutoStep

@workflow(purpose="processes each item")
def run(items):
    口 = Step(step_num=1, name="Process items", purpose="loop over the batch")
    for item in items:
        口 = AutoStep(step_num=1.1, name="Handle one item")
        handle(item)

@task(purpose="handle a single item")
def handle(item):
    return None
""".lstrip(),
    )
    nodes, edges = module_scanner.scan_module(f, tmp_path, "proj")

    assert any(n.id == "proj::cli::run::step-1.1" for n in nodes)
    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert any(e.from_id == "proj::cli::run::step-1.1" and e.to_id == "proj::cli::handle" for e in delegates)


@workflow(purpose="AutoStep delegating through self resolves to the sibling method")
def test_autostep_via_self_emits_delegates_to_sibling_method(tmp_path):
    """AutoStep, then ``self.method()``, links to that method's node.

    The receiver is not an import, so resolution comes from the enclosing
    class rather than the name map.
    """
    f = _write(
        tmp_path / "svc.py",
        """
from axiom_annotations import workflow, task, AutoStep

class DataLoader:
    @workflow(purpose="load one day")
    def run(self, day):
        口 = AutoStep(step_num=1, name="Load raw rows")
        raw = self.load_data(day)
        return raw

    @task(purpose="read one day of rows")
    def load_data(self, day):
        return []
""".lstrip(),
    )
    nodes, edges = module_scanner.scan_module(f, tmp_path, "proj")

    assert any(n.id == "proj::svc::DataLoader.load_data" for n in nodes)
    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert any(
        e.from_id == "proj::svc::DataLoader.run::step-1" and e.to_id == "proj::svc::DataLoader.load_data"
        for e in delegates
    ), f"got {[(e.from_id, e.to_id) for e in delegates]}"


@workflow(purpose="AutoStep delegating through cls resolves to the sibling method")
def test_autostep_via_cls_emits_delegates_to_sibling_method(tmp_path):
    """``cls.method()`` after an AutoStep resolves the same way ``self.`` does."""
    f = _write(
        tmp_path / "svc.py",
        """
from axiom_annotations import workflow, task, AutoStep

class Registry:
    @classmethod
    @workflow(purpose="rebuild the registry")
    def rebuild(cls):
        口 = AutoStep(step_num=1, name="Drop entries")
        cls.drop_all()

    @classmethod
    @task(purpose="drop every entry")
    def drop_all(cls):
        return None
""".lstrip(),
    )
    _, edges = module_scanner.scan_module(f, tmp_path, "proj")

    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert any(
        e.from_id == "proj::svc::Registry.rebuild::step-1" and e.to_id == "proj::svc::Registry.drop_all"
        for e in delegates
    ), f"got {[(e.from_id, e.to_id) for e in delegates]}"


@workflow(purpose="Two classes sharing a method name each resolve to their own method")
def test_self_delegation_is_scoped_to_the_enclosing_class(tmp_path):
    """``self.load()`` in two classes links to each class's own ``load``.

    Resolution reads the enclosing class from the walk position, so a name
    shared across classes in one module is never ambiguous.
    """
    f = _write(
        tmp_path / "svc.py",
        """
from axiom_annotations import workflow, task, AutoStep

class Alpha:
    @workflow(purpose="alpha pipeline")
    def run(self):
        口 = AutoStep(step_num=1, name="Load")
        self.load()

    @task(purpose="alpha load")
    def load(self):
        return "a"

class Beta:
    @workflow(purpose="beta pipeline")
    def run(self):
        口 = AutoStep(step_num=1, name="Load")
        self.load()

    @task(purpose="beta load")
    def load(self):
        return "b"
""".lstrip(),
    )
    _, edges = module_scanner.scan_module(f, tmp_path, "proj")

    pairs = {(e.from_id, e.to_id) for e in edges if e.edge_type == "delegates_to"}
    assert ("proj::svc::Alpha.run::step-1", "proj::svc::Alpha.load") in pairs
    assert ("proj::svc::Beta.run::step-1", "proj::svc::Beta.load") in pairs
    assert ("proj::svc::Alpha.run::step-1", "proj::svc::Beta.load") not in pairs
    assert ("proj::svc::Beta.run::step-1", "proj::svc::Alpha.load") not in pairs


@workflow(purpose="self delegation inside a closure resolves to the class, not the enclosing method")
def test_self_delegation_in_a_closure_resolves_to_the_class(tmp_path):
    """A workflow nested inside a method still reaches ``ClassName.method``.

    The enclosing *class* qualifies the target; the enclosing *function* does
    not, so a closure two levels deep resolves the same as a plain method.
    """
    f = _write(
        tmp_path / "svc.py",
        """
from axiom_annotations import workflow, task, AutoStep

class Loader:
    def build(self):
        @workflow(purpose="inner pipeline")
        def inner():
            口 = AutoStep(step_num=1, name="Read rows")
            self.read_rows()

        return inner

    @task(purpose="read the rows")
    def read_rows(self):
        return []
""".lstrip(),
    )
    _, edges = module_scanner.scan_module(f, tmp_path, "proj")

    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert any(
        e.from_id == "proj::svc::Loader.build.inner::step-1" and e.to_id == "proj::svc::Loader.read_rows"
        for e in delegates
    ), f"got {[(e.from_id, e.to_id) for e in delegates]}"


@workflow(purpose="A call on an attribute of self is not treated as a sibling method")
def test_self_delegation_ignores_a_chained_attribute_receiver(tmp_path):
    """``self.collaborator.method()`` names a method of the collaborator.

    Only a direct ``self.method()`` receiver identifies a sibling; walking a
    chain down to ``self`` and pairing it with the outermost attribute would
    name a method the class does not have.
    """
    f = _write(
        tmp_path / "svc.py",
        """
from axiom_annotations import workflow, task, AutoStep

class Env:
    @workflow(purpose="build one image")
    def build_image(self):
        口 = AutoStep(step_num=1, name="Build the image")
        self.docker_runner.build(1, 2)

    @task(purpose="a real sibling method")
    def other(self):
        return None
""".lstrip(),
    )
    nodes, edges = module_scanner.scan_module(f, tmp_path, "proj")

    minted = {n.id for n in nodes}
    delegates = [e for e in edges if e.edge_type == "delegates_to"]
    assert not delegates, f"expected no edge, got {[(e.from_id, e.to_id) for e in delegates]}"
    assert all(e.to_id in minted for e in edges if e.edge_type == "delegates_to")


@workflow(purpose="A self receiver outside any class emits no delegates_to")
def test_self_delegation_outside_a_class_emits_no_delegates(tmp_path):
    """``self.method()`` in a module-level function has no class to qualify it.

    There is no enclosing class, so no target can be named and no edge is
    invented.
    """
    f = _write(
        tmp_path / "svc.py",
        """
from axiom_annotations import workflow, AutoStep

@workflow(purpose="not a method despite the parameter name")
def run(self):
    口 = AutoStep(step_num=1, name="Load rows")
    self.load_data()
""".lstrip(),
    )
    nodes, edges = module_scanner.scan_module(f, tmp_path, "proj")

    assert any(n.id == "proj::svc::run::step-1" for n in nodes)
    assert not [e for e in edges if e.edge_type == "delegates_to"]


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
