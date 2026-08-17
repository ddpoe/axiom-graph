"""Structural invariants an index must satisfy after a build.

Each invariant is a helper that returns the violations it finds, so it can
be pointed at any built index, plus a test that asserts a freshly built
index has none.  Add new invariants as helper + test pairs beside these.
"""

from __future__ import annotations

from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.index import builder, db
from axiom_graph.index.staleness import find_broken_links
from axiom_graph.index.status import BROKEN_LINK
from axiom_graph.lifecycle.api import build_index, compute_check_summary
from axiom_graph.models import make_edge


# ---------------------------------------------------------------------------
# Invariant helpers
# ---------------------------------------------------------------------------


def autostep_delegate_cardinality_violations(db_path: Path) -> dict[str, list[str]]:
    """Return AutoStep sources holding more than one outbound delegate link.

    An AutoStep delegates to at most one target — the function its next call
    resolves to.  The scoping to AutoStep sources is load-bearing: a state
    machine's states legitimately delegate to one target per transition.

    Args:
        db_path: Path to the index to inspect.

    Returns:
        Mapping of source node ID to its delegate targets, containing only
        sources with two or more.  Empty when the invariant holds.
    """
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT e.from_id, e.to_id FROM edges e "
            "JOIN nodes n ON n.id = e.from_id "
            "WHERE e.edge_type = 'delegates_to' AND n.subtype = 'autostep'"
        ).fetchall()
    by_source: dict[str, list[str]] = {}
    for row in rows:
        by_source.setdefault(row["from_id"], []).append(row["to_id"])
    return {source: targets for source, targets in by_source.items() if len(targets) > 1}


def _is_external_stub_id(node_id: str, shadowing_modules: set[str]) -> bool:
    """Return whether *node_id* is a scanner-minted external package stub.

    An external stub id is exactly ``{project_id}::external::{package}`` —
    three segments, the middle one literal.  Matching that shape rather
    than the substring ``::external::`` keeps the exclusion from swallowing
    deeper ids that merely contain a segment called ``external``, such as a
    step node under a function of that name.

    The three-segment shape alone is still ambiguous: a project holding a
    top-level ``external.py`` mints ``{project_id}::external::{function}``
    for each of its functions, which is character-for-character an external
    stub id.  Nothing in the id distinguishes them, so the module node is
    consulted instead — *shadowing_modules* holds the ids of module nodes
    named ``external``, and a project that has one gets no exclusion at
    all, which errs toward reporting rather than hiding.

    Args:
        node_id: The edge target being classified.
        shadowing_modules: Ids of module nodes whose dotpath is ``external``
            (i.e. ``{project_id}::external``), as returned by the index.

    Returns:
        True when the id should be treated as an external package stub.
    """
    parts = node_id.split("::")
    if len(parts) != 3 or parts[1] != "external":
        return False
    return f"{parts[0]}::external" not in shadowing_modules


def dangling_edge_target_violations(db_path: Path) -> list[tuple[str, str, str]]:
    """Return every edge whose target names no node in the index.

    An edge is a claim about two nodes.  A target that names nothing is a
    claim the index cannot honour: navigation dead-ends, traversal stops,
    and consumers print an identifier that resolves to no source.  The
    check is deliberately written over edge targets in general rather than
    one edge type, so an edge type added later inherits it.

    External package stubs are excluded.  Scanners mint those ids for
    third-party imports and the node behind one is a deliberate placeholder,
    so its absence from a partial index is not a broken claim.

    Args:
        db_path: Path to the index to inspect.

    Returns:
        Sorted list of ``(edge_type, from_id, to_id)`` triples, one per
        dangling edge.  Empty when the invariant holds.
    """
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT e.edge_type, e.from_id, e.to_id FROM edges e LEFT JOIN nodes n ON n.id = e.to_id WHERE n.id IS NULL"
        ).fetchall()
        shadowing_modules = {
            row["id"] for row in conn.execute("SELECT id FROM nodes WHERE subtype = 'module' AND id LIKE '%::external'")
        }
    return sorted(
        (row["edge_type"], row["from_id"], row["to_id"])
        for row in rows
        if not _is_external_stub_id(row["to_id"], shadowing_modules)
    )


# ---------------------------------------------------------------------------
# Fixture family — one multi-file package modelling real module topology
# ---------------------------------------------------------------------------
#
# Every resolution shape the index has to survive lives in one package so the
# cases below share a single topology instead of each inventing its own:
#
#   pkgapp/__init__.py        package that defines nothing
#   pkgapp/runner.py          tasks reached by attribute call
#   pkgapp/impl/core.py       task reached only through two star hops
#   pkgapp/impl/__init__.py   star aggregator
#   pkgapp/shim.py            star back-compat shim
#   pkgapp/orchestrator.py    `from pkg import submodule` + relative form
#   pkgapp/deferred.py        import inside the function body, attribute call
#   pkgapp/plain.py           no annotations, deferred import its only reference
#   pkgapp/service.py         workflow in a class body, delegating to a
#                             sibling method and to a method on a collaborator
#
# ``_write_unresolvable_module`` is deliberately separate: the family above is
# fully resolvable, and the broken-link case needs a target nothing defines.

_FAMILY_FILES: dict[str, str] = {
    "pkgapp/__init__.py": '"""Package that defines nothing of its own."""\n',
    "pkgapp/runner.py": (
        '"""Tasks a workflow reaches by attribute call."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Build the image")\n'
        "def build_image():\n"
        '    return "image"\n\n\n'
        '@task(purpose="Tear the stack down")\n'
        "def teardown():\n"
        '    return "gone"\n'
    ),
    "pkgapp/impl/core.py": (
        '"""Definitions reached only through the star-export layers."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Persist one record")\n'
        "def persist_record():\n"
        "    return 1\n"
    ),
    "pkgapp/impl/__init__.py": (
        '"""Aggregator that re-exports its submodule."""\n\nfrom pkgapp.impl.core import *  # noqa: F403\n'
    ),
    "pkgapp/shim.py": ('"""Back-compat shim that defines nothing."""\n\nfrom pkgapp.impl import *  # noqa: F403\n'),
    "pkgapp/orchestrator.py": (
        '"""Workflow whose steps leave the file."""\n\n'
        "from axiom_annotations import AutoStep, workflow\n\n"
        "from pkgapp import runner\n"
        "from . import shim\n\n\n"
        '@workflow(purpose="Run the cross-module pipeline")\n'
        "def pipeline():\n"
        '    口 = AutoStep(step_num=1, name="Build the image")\n'
        "    runner.build_image()\n"
        '    口 = AutoStep(step_num=2, name="Persist the record")\n'
        "    shim.persist_record()\n"
    ),
    "pkgapp/deferred.py": (
        '"""Workflow whose receiver is imported inside the function body."""\n\n'
        "from axiom_annotations import AutoStep, workflow\n\n\n"
        '@workflow(purpose="Tear the stack down")\n'
        "def deploy():\n"
        "    from pkgapp import runner\n\n"
        '    口 = AutoStep(step_num=1, name="Tear the stack down")\n'
        "    runner.teardown()\n"
    ),
    "pkgapp/plain.py": (
        '"""Module with no annotations whose only reference is deferred."""\n\n\n'
        "def helper():\n"
        "    from pkgapp import runner\n\n"
        "    return runner.build_image()\n"
    ),
    "pkgapp/service.py": (
        '"""Workflow declared in a class body."""\n\n'
        "from axiom_annotations import AutoStep, task, workflow\n\n\n"
        '@task(purpose="Emit the report")\n'
        "def emit_report():\n"
        '    return "report"\n\n\n'
        "class Service:\n"
        '    """One request handler."""\n\n'
        '    @workflow(purpose="Serve one request")\n'
        "    def handle(self):\n"
        '        口 = AutoStep(step_num=1, name="Emit the report")\n'
        "        emit_report()\n"
        '        口 = AutoStep(step_num=2, name="Record the outcome")\n'
        "        self.record_outcome()\n"
        '        口 = AutoStep(step_num=3, name="Flush the sink")\n'
        "        self.sink.flush()\n\n"
        '    @task(purpose="Record one outcome")\n'
        "    def record_outcome(self):\n"
        "        return None\n"
    ),
}

_UNRESOLVABLE_MODULE = (
    '"""Workflow whose delegate target is defined nowhere."""\n\n'
    "from axiom_annotations import AutoStep, workflow\n\n"
    "from pkgapp import runner\n\n\n"
    '@workflow(purpose="Call a name the package does not define")\n'
    "def orphan():\n"
    '    口 = AutoStep(step_num=1, name="Call a helper that does not exist")\n'
    "    runner.vanished_helper()\n"
)


def _write_fixture_family(project_root: Path) -> None:
    """Write the multi-file fixture package into *project_root*."""
    for rel_path, source in _FAMILY_FILES.items():
        target = project_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")


def _write_unresolvable_module(project_root: Path) -> None:
    """Add a module whose delegate target no file in the family defines."""
    (project_root / "pkgapp" / "unresolvable.py").write_text(_UNRESOLVABLE_MODULE, encoding="utf-8")


# ---------------------------------------------------------------------------
# Index navigation helpers — every expectation is read back from the build
# ---------------------------------------------------------------------------


def _node_id(db_path: Path, location: str, title: str) -> str:
    """Return the id of the node the build minted for *title* in *location*."""
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM nodes WHERE location = ? AND title = ?",
            (location, title),
        ).fetchall()
    assert len(rows) == 1, f"expected one node for {title!r} in {location}, got {[r['id'] for r in rows]}"
    return rows[0]["id"]


def _module_node_id(db_path: Path, location: str) -> str:
    """Return the id of the module node the build minted for *location*."""
    with db._connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM nodes WHERE location = ? AND subtype = 'module'",
            (location,),
        ).fetchone()
    assert row is not None, f"build did not mint a module node for {location}"
    return row["id"]


def _persisted_link_status(db_path: Path, node_id: str) -> str | None:
    """Return the ``link_status`` column stored on *node_id*, or ``None``.

    Reads the persisted column rather than any in-memory computation, so a
    caller can tell whether a status a build wrote actually survived the
    staleness recompute that follows it.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        node_id: Node whose stored link status is wanted.

    Returns:
        The stored ``link_status`` string, or ``None`` when no such node row
        exists.
    """
    with db._connect(db_path) as conn:
        row = conn.execute("SELECT link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return None if row is None else row["link_status"]


def _delegate_target(db_path: Path, step_source_id: str) -> str | None:
    """Return the single delegate target held by *step_source_id*, if any."""
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT to_id FROM edges WHERE edge_type = 'delegates_to' AND from_id = ?",
            (step_source_id,),
        ).fetchall()
    assert len(rows) <= 1, f"{step_source_id} holds several delegate links: {[r['to_id'] for r in rows]}"
    return rows[0]["to_id"] if rows else None


def _step_id(func_node_id: str, step_num: str) -> str:
    """Return the id of a step node inside *func_node_id*."""
    return f"{func_node_id}::step-{step_num}"


def _has_edge(db_path: Path, edge_type: str, from_id: str, to_id: str) -> bool:
    """Return whether the index holds *edge_type* between the two nodes."""
    with db._connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM edges WHERE edge_type = ? AND from_id = ? AND to_id = ?",
            (edge_type, from_id, to_id),
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Tier 2 — invariants over a built index
# ---------------------------------------------------------------------------


def _workflow_module(step_name: str, next_call: str) -> str:
    """Return a module with one workflow whose AutoStep is followed by *next_call*."""
    return (
        '"""Module."""\n\n'
        "from axiom_annotations import AutoStep, task, workflow\n\n\n"
        '@task(purpose="Do the first thing")\n'
        "def alpha():\n"
        "    return 1\n\n\n"
        '@task(purpose="Do the second thing")\n'
        "def beta():\n"
        "    return 2\n\n\n"
        '@workflow(purpose="Run it")\n'
        f"def {step_name}():\n"
        '    口 = AutoStep(step_num=1, name="Delegate")\n'
        f"    {next_call}\n"
    )


@workflow(
    purpose="No AutoStep in a built index holds more than one outbound delegate link, including after its call is retargeted",
)
def test_no_autostep_holds_more_than_one_delegate_link(mini_project, db_path):
    (mini_project / "stable.py").write_text(_workflow_module("run_stable", "alpha()"), encoding="utf-8")
    (mini_project / "moving.py").write_text(_workflow_module("run_moving", "alpha()"), encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    (mini_project / "moving.py").write_text(_workflow_module("run_moving", "beta()"), encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    with db._connect(db_path) as conn:
        delegate_count = conn.execute("SELECT COUNT(*) AS c FROM edges WHERE edge_type = 'delegates_to'").fetchone()[
            "c"
        ]
    assert delegate_count >= 2, "fixture should have produced delegate links to check"

    violations = autostep_delegate_cardinality_violations(db_path)
    assert violations == {}, f"AutoStep sources with several delegate links: {violations}"


@workflow(
    purpose="No edge in a built index points at an identifier the same build did not mint as a node",
)
def test_no_edge_targets_a_node_the_build_did_not_mint(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    with db._connect(db_path) as conn:
        edge_count = conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]
    assert edge_count > 0, "fixture family should have produced edges to check"

    violations = dangling_edge_target_violations(db_path)
    assert violations == [], f"edges whose target names no node: {violations}"


# ---------------------------------------------------------------------------
# Tier 2 / Tier 3 — delegate target resolution across module boundaries
# ---------------------------------------------------------------------------


@workflow(
    purpose="An AutoStep calling a method on a module imported via `from package import submodule` delegates to the node defining that method",
)
def test_attribute_call_on_an_imported_submodule_resolves_to_the_defining_node(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    pipeline_id = _node_id(db_path, "pkgapp/orchestrator.py", "pipeline")
    build_image_id = _node_id(db_path, "pkgapp/runner.py", "build_image")

    assert _delegate_target(db_path, _step_id(pipeline_id, "1")) == build_image_id


@workflow(
    purpose="An AutoStep whose target is reached through star-export shim layers delegates to the defining node, not to a shim that defines nothing",
)
def test_target_behind_star_export_shims_resolves_to_the_defining_node(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    pipeline_id = _node_id(db_path, "pkgapp/orchestrator.py", "pipeline")
    persist_record_id = _node_id(db_path, "pkgapp/impl/core.py", "persist_record")

    assert _delegate_target(db_path, _step_id(pipeline_id, "2")) == persist_record_id


@workflow(
    purpose="An AutoStep in a method calling a sibling method through self delegates to that method's node",
)
def test_self_call_in_a_method_resolves_to_the_sibling_method(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    handle_id = _node_id(db_path, "pkgapp/service.py", "Service.handle")
    record_outcome_id = _node_id(db_path, "pkgapp/service.py", "Service.record_outcome")

    assert _delegate_target(db_path, _step_id(handle_id, "2")) == record_outcome_id


@workflow(
    purpose="A module whose only reference to another module is an import inside a function body depends on it, with no annotations anywhere",
)
def test_import_inside_a_function_body_is_a_module_dependency(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    plain_module_id = _module_node_id(db_path, "pkgapp/plain.py")
    runner_module_id = _module_node_id(db_path, "pkgapp/runner.py")
    orchestrator_module_id = _module_node_id(db_path, "pkgapp/orchestrator.py")

    assert _has_edge(db_path, "depends_on", plain_module_id, runner_module_id)
    # A module-level import of the same module is unaffected.
    assert _has_edge(db_path, "depends_on", orchestrator_module_id, runner_module_id)


@workflow(
    purpose="A workflow whose receiver is imported inside its own function body and then called by attribute delegates to the node defining the called function",
)
def test_receiver_imported_inside_the_function_body_resolves_by_attribute_call(mini_project, db_path):
    口 = Step(
        step_num=1,
        name="Write the fixture package",
        purpose="Lay down a package whose workflow imports its receiver inside the function body and calls it by attribute",
    )
    _write_fixture_family(mini_project)

    口 = Step(
        step_num=2,
        name="Build the index",
        purpose="Scan the package so nodes and edges are minted for every module",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    口 = Step(
        step_num=3,
        name="Read back the nodes the build minted",
        purpose="Take the workflow and the called function from the index rather than naming them",
    )
    deploy_id = _node_id(db_path, "pkgapp/deferred.py", "deploy")
    teardown_id = _node_id(db_path, "pkgapp/runner.py", "teardown")

    口 = Step(
        step_num=4,
        name="Assert the step delegates to the defining node",
        purpose="Confirm the deferred import and the attribute call resolve together",
    )
    assert _delegate_target(db_path, _step_id(deploy_id, "1")) == teardown_id

    口 = Step(
        step_num=5,
        name="Assert the deferred import is also a dependency",
        purpose="Confirm the same import feeds the module dependency graph",
    )
    deferred_module_id = _module_node_id(db_path, "pkgapp/deferred.py")
    runner_module_id = _module_node_id(db_path, "pkgapp/runner.py")
    assert _has_edge(db_path, "depends_on", deferred_module_id, runner_module_id)


@workflow(
    purpose="A delegate target no file defines is reported as a broken link, while a target that resolves produces no finding",
)
def test_unresolvable_delegate_target_is_reported_as_a_broken_link(mini_project, db_path):
    _write_fixture_family(mini_project)
    _write_unresolvable_module(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    orphan_id = _node_id(db_path, "pkgapp/unresolvable.py", "orphan")
    pipeline_id = _node_id(db_path, "pkgapp/orchestrator.py", "pipeline")

    broken = find_broken_links(db_path)

    assert f"{orphan_id}@workflow" in broken, f"unresolvable delegate target not reported: {broken}"
    assert f"{pipeline_id}@workflow" not in broken, "a delegate target that resolves must produce no finding"


@workflow(
    purpose="A broken delegate link is persisted on the envelope by the build and still reported by the check that follows it",
)
def test_broken_delegate_link_survives_the_build_and_the_following_check(mini_project, db_path):
    _write_fixture_family(mini_project)
    _write_unresolvable_module(mini_project)

    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    orphan_envelope = f"{_node_id(db_path, 'pkgapp/unresolvable.py', 'orphan')}@workflow"
    pipeline_envelope = f"{_node_id(db_path, 'pkgapp/orchestrator.py', 'pipeline')}@workflow"

    assert _persisted_link_status(db_path, orphan_envelope) == BROKEN_LINK, (
        "the build did not leave BROKEN_LINK persisted on the envelope of the unresolvable delegate target"
    )

    summary = compute_check_summary(db_path, mini_project)

    assert summary is not None
    assert summary.link_counts[BROKEN_LINK] >= 1, (
        f"check did not report the unresolvable delegate target: {summary.link_counts}"
    )
    assert _persisted_link_status(db_path, orphan_envelope) == BROKEN_LINK, (
        "the staleness recompute behind check overwrote the envelope's BROKEN_LINK"
    )
    assert _persisted_link_status(db_path, pipeline_envelope) != BROKEN_LINK, (
        "an envelope whose delegate targets all resolve must not be flagged"
    )


def test_a_step_no_envelope_composes_keeps_the_broken_link_finding_on_itself(mini_project, db_path):
    """A step with no composing envelope holds its own finding, and it persists.

    Envelope attribution needs a ``composes`` edge from a workflow or task.
    A step left in the index by an earlier edit has none, so the finding
    falls back to the step id.  It must still land somewhere that keeps it:
    the step id is the only identifier that describes the leftover, and a
    status written there survives the recompute because the overlay outranks
    the blanket VERIFIED that steps are assigned.
    """
    _write_fixture_family(mini_project)
    _write_unresolvable_module(mini_project)
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    orphan_id = _node_id(db_path, "pkgapp/unresolvable.py", "orphan")
    step_id = _step_id(orphan_id, "1")
    with db._connect(db_path) as conn:
        conn.execute("DELETE FROM edges WHERE edge_type = 'composes' AND to_id = ?", (step_id,))

    assert find_broken_links(db_path).get(step_id) is not None, (
        "with no composing envelope the finding must fall back to the step itself"
    )

    compute_check_summary(db_path, mini_project)

    assert _persisted_link_status(db_path, step_id) == BROKEN_LINK
    assert _persisted_link_status(db_path, f"{orphan_id}@workflow") != BROKEN_LINK, (
        "an envelope that no longer composes the step must not keep its finding"
    )


@workflow(
    purpose="Retargeting an AutoStep's cross-module call leaves exactly one delegate link, pointing at the new target",
)
def test_retargeting_a_cross_module_autostep_leaves_one_delegate_link(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    deferred = mini_project / "pkgapp" / "deferred.py"
    deferred.write_text(
        deferred.read_text(encoding="utf-8").replace("runner.teardown()", "runner.build_image()"),
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    deploy_id = _node_id(db_path, "pkgapp/deferred.py", "deploy")
    build_image_id = _node_id(db_path, "pkgapp/runner.py", "build_image")

    assert _delegate_target(db_path, _step_id(deploy_id, "1")) == build_image_id
    assert dangling_edge_target_violations(db_path) == []


# ---------------------------------------------------------------------------
# Tier 1 — re-export closure walk internals
# ---------------------------------------------------------------------------


def test_reexport_walk_terminates_on_a_cycle_and_respects_the_depth_bound():
    """Mutual re-exports terminate, and a chain longer than the bound is not followed."""
    cycle = {"a": ["b"], "b": ["a"]}
    assert builder.resolve_symbol_through_reexports("a", "thing", lambda _: False, lambda m: cycle.get(m, [])) is None

    chain = {f"m{i}": [f"m{i + 1}"] for i in range(6)}
    defined = {"m5::thing"}
    assert (
        builder.resolve_symbol_through_reexports(
            "m0", "thing", lambda n: n in defined, lambda m: chain.get(m, []), max_depth=2
        )
        is None
    )
    assert (
        builder.resolve_symbol_through_reexports(
            "m0", "thing", lambda n: n in defined, lambda m: chain.get(m, []), max_depth=5
        )
        == "m5::thing"
    )


def test_reexport_walk_prefers_the_nearest_source_then_the_smallest_id():
    """A name exported by several sources resolves the same way on every build."""
    relation = {"root": ["zeta", "alpha"], "alpha": ["deep"]}
    defined = {"zeta::thing", "alpha::thing", "deep::thing"}

    assert (
        builder.resolve_symbol_through_reexports("root", "thing", lambda n: n in defined, lambda m: relation.get(m, []))
        == "alpha::thing"
    )

    nearest_only = {"deep::thing"}
    assert (
        builder.resolve_symbol_through_reexports(
            "root", "thing", lambda n: n in nearest_only, lambda m: relation.get(m, [])
        )
        == "deep::thing"
    )


def test_resolution_warns_only_when_unresolved_targets_meet_an_empty_reexport_relation(mini_project, db_path):
    """An index holding no re-export markers cannot resolve, and must say how to fix it."""
    _write_fixture_family(mini_project)
    _write_unresolvable_module(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    orphan_id = _node_id(db_path, "pkgapp/unresolvable.py", "orphan")
    unresolved_edge = make_edge("delegates_to", _step_id(orphan_id, "1"), "proj::pkgapp.runner::vanished_helper")

    with_markers: list[str] = []
    builder._resolve_delegate_targets(db_path, [unresolved_edge], with_markers)
    assert with_markers == [], f"a populated re-export relation must not warn: {with_markers}"

    # An index built before re-export markers existed carries none, which is
    # what stripping the edge meta reproduces.
    with db._connect(db_path) as conn:
        conn.execute("UPDATE edges SET meta = NULL WHERE edge_type = 'depends_on'")

    without_markers: list[str] = []
    builder._resolve_delegate_targets(db_path, [unresolved_edge], without_markers)

    assert len(without_markers) == 1, f"expected one warning, got {without_markers}"
    warning = without_markers[0]
    assert "file_mtime" in warning and "rescan" in warning, f"warning must name the remedy: {warning}"

    # No unresolved target, same empty relation — nothing to warn about.
    resolvable_target = _node_id(db_path, "pkgapp/runner.py", "build_image")
    quiet: list[str] = []
    builder._resolve_delegate_targets(
        db_path, [make_edge("delegates_to", _step_id(orphan_id, "1"), resolvable_target)], quiet
    )
    assert quiet == [], f"a resolved target must not warn: {quiet}"


def test_external_stub_exclusion_is_structural_and_yields_to_a_module_named_external():
    """The exclusion covers stub ids only, and steps aside for a real ``external.py``."""
    assert _is_external_stub_id("proj::external::requests", set())
    # Deeper ids merely containing an ``external`` segment stay in the net.
    assert not _is_external_stub_id("proj::pkg.mod::external::step-1", set())
    assert not _is_external_stub_id("proj::pkg.external::helper", set())
    # A project with a top-level external.py mints ids of the stub's exact
    # shape, so the module node decides.
    assert not _is_external_stub_id("proj::external::helper", {"proj::external"})
