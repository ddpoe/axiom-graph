"""Aggregate honesty for mark_clean.

Marking a node clean when its LINKED_STALE is inherited from ``composes``
descendants writes a verification row but has no direct effect — the next
recompute re-derives the parent's link_status from its children.  These
tests prove the result now says so explicitly (``inherited`` / ``mixed``
classification on :class:`MarkCleanResult`) while ordinary own-signal
nodes keep the plain success shape.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from axiom_annotations import workflow
from click.testing import CliRunner

from axiom_graph.cli import cmd_mark_clean
from axiom_graph.index import builder
from axiom_graph.index.staleness import _get_linked_stale_ids
from axiom_graph.lifecycle.api import compute_check_summary, mark_clean_nodes
from axiom_graph.lifecycle.mcp_tools import axiom_graph_mark_clean


def _write_doc(docs_dir: Path, filename: str, payload: dict) -> Path:
    doc_path = docs_dir / filename
    doc_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return doc_path


def _setup_code(mini_project: Path, name: str = "mod") -> Path:
    src_dir = mini_project / "src"
    src_dir.mkdir(exist_ok=True)
    code_path = src_dir / f"{name}.py"
    code_path.write_text("def foo():\n    return 0\n", encoding="utf-8")
    return code_path


def _drift(code_path: Path, body: str = "def foo():\n    return 42\n") -> None:
    time.sleep(0.05)
    code_path.write_text(body, encoding="utf-8")


@workflow(
    purpose=(
        "mark_clean on a doc envelope whose LINKED_STALE is inherited from a "
        "stale section returns an explicit inherited classification naming the "
        "stale descendant, and the envelope's persisted link_status is still "
        "LINKED_STALE after the next recompute — no more silent false success"
    ),
)
def test_mark_clean_envelope_inherited_no_direct_effect(mini_project: Path, db_path: Path):
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    code_path = _setup_code(mini_project)
    _write_doc(
        docs_dir,
        "spec.json",
        {
            "title": "Spec",
            "sections": [
                {
                    "id": "overview",
                    "heading": "Overview",
                    "content": "Documents foo.",
                    "links": [{"node_id": "proj::src.mod::foo"}],
                },
            ],
        },
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    _drift(code_path)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    envelope_id = "proj::docs.spec"
    section_id = "proj::docs.spec::overview"

    # Sanity: the section carries the own signal; the envelope inherits.
    stale_map = _get_linked_stale_ids(db_path)
    assert section_id in stale_map
    assert envelope_id not in stale_map

    time.sleep(0.05)
    result = mark_clean_nodes(
        db_path,
        mini_project,
        [envelope_id],
        "Checked the doc top-to-bottom.",
        verified_by="agent",
    )

    assert result.marked == [envelope_id]
    assert result.inherited == {envelope_id: [section_id]}
    assert result.mixed == {}

    # The recompute re-derives the envelope's link_status from the still-stale
    # section: marking the envelope changed nothing.
    cs = compute_check_summary(db_path, mini_project)
    own, link, _via = cs.statuses[envelope_id]
    assert link == "LINKED_STALE"


@workflow(
    purpose=(
        "The inherited classification keys on composes edges, not node_type: a "
        "nested section parent (atomic_process with child sections) whose "
        "LINKED_STALE comes from a stale child gets the same honest "
        "inherited-only report as a composite doc envelope"
    ),
)
def test_mark_clean_nested_section_parent_inherited(mini_project: Path, db_path: Path):
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    code_path = _setup_code(mini_project)
    _write_doc(
        docs_dir,
        "arch.json",
        {
            "title": "Arch",
            "sections": [
                {
                    "id": "parent",
                    "heading": "Parent",
                    "content": "Parent prose with no links.",
                    "sections": [
                        {
                            "id": "child",
                            "heading": "Child",
                            "content": "Documents foo.",
                            "links": [{"node_id": "proj::src.mod::foo"}],
                        },
                    ],
                },
            ],
        },
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    _drift(code_path)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    parent_id = "proj::docs.arch::parent"
    child_id = "proj::docs.arch::parent.child"

    stale_map = _get_linked_stale_ids(db_path)
    assert child_id in stale_map
    assert parent_id not in stale_map

    time.sleep(0.05)
    result = mark_clean_nodes(
        db_path,
        mini_project,
        [parent_id],
        "Read the parent section.",
        verified_by="agent",
    )

    assert result.inherited == {parent_id: [child_id]}
    assert result.mixed == {}

    cs = compute_check_summary(db_path, mini_project)
    _own, link, _via = cs.statuses[parent_id]
    assert link == "LINKED_STALE"


@workflow(
    purpose=(
        "A mixed node — own documents edge to drifted code AND a stale child "
        "section — gets its own signal genuinely cleared by mark_clean while "
        "the report distinguishes the surviving inherited portion instead of "
        "claiming either full success or no effect"
    ),
)
def test_mark_clean_mixed_own_cleared_inherited_remains(mini_project: Path, db_path: Path):
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    src_dir = mini_project / "src"
    src_dir.mkdir(exist_ok=True)
    code_x = src_dir / "alpha.py"
    code_x.write_text("def foo():\n    return 0\n", encoding="utf-8")
    code_y = src_dir / "beta.py"
    code_y.write_text("def bar():\n    return 0\n", encoding="utf-8")
    _write_doc(
        docs_dir,
        "guide.json",
        {
            "title": "Guide",
            "sections": [
                {
                    "id": "parent",
                    "heading": "Parent",
                    "content": "Documents alpha.foo directly.",
                    "links": [{"node_id": "proj::src.alpha::foo"}],
                    "sections": [
                        {
                            "id": "child",
                            "heading": "Child",
                            "content": "Documents beta.bar.",
                            "links": [{"node_id": "proj::src.beta::bar"}],
                        },
                    ],
                },
            ],
        },
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_x.write_text("def foo():\n    return 1\n", encoding="utf-8")
    code_y.write_text("def bar():\n    return 1\n", encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    parent_id = "proj::docs.guide::parent"
    child_id = "proj::docs.guide::parent.child"

    stale_map = _get_linked_stale_ids(db_path)
    assert parent_id in stale_map
    assert child_id in stale_map

    time.sleep(0.05)
    result = mark_clean_nodes(
        db_path,
        mini_project,
        [parent_id],
        "Parent prose still matches alpha.foo.",
        verified_by="agent",
    )

    assert result.mixed == {parent_id: [child_id]}
    assert result.inherited == {}

    # Own signal genuinely cleared: the parent leaves the live stale map...
    stale_map_after = _get_linked_stale_ids(db_path)
    assert parent_id not in stale_map_after
    assert child_id in stale_map_after

    # ...but the inherited portion survives the recompute.
    cs = compute_check_summary(db_path, mini_project)
    _own, link, _via = cs.statuses[parent_id]
    assert link == "LINKED_STALE"


def test_mark_clean_ordinary_node_keeps_plain_shape(mini_project: Path, db_path: Path):
    """Own-signal-only nodes report plain success — no classification noise."""
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    code_path = _setup_code(mini_project)
    _write_doc(
        docs_dir,
        "spec.json",
        {
            "title": "Spec",
            "sections": [
                {
                    "id": "overview",
                    "heading": "Overview",
                    "content": "Documents foo.",
                    "links": [{"node_id": "proj::src.mod::foo"}],
                },
            ],
        },
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    _drift(code_path)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    section_id = "proj::docs.spec::overview"

    time.sleep(0.05)
    result = mark_clean_nodes(
        db_path,
        mini_project,
        [section_id],
        "Prose still accurate.",
        verified_by="agent",
    )
    assert result.marked == [section_id]
    assert result.inherited == {}
    assert result.mixed == {}

    # MCP single-node rendering for an ordinary node is byte-identical to
    # the pre-classification output.
    time.sleep(0.05)
    out = axiom_graph_mark_clean(
        project_root=str(mini_project),
        node_id=section_id,
        reason="Prose still accurate.",
    )
    assert out == f"Marked '{section_id}' as AGENT_VERIFIED.\nReason: Prose still accurate."


# ---------------------------------------------------------------------------
# CLI rendering of the inherited / mixed classification
# ---------------------------------------------------------------------------


def _setup_inherited_envelope(mini_project: Path) -> tuple[str, str]:
    """Build a doc envelope whose LINKED_STALE is inherited from one section.

    Returns:
        Tuple of (envelope node ID, stale section node ID).
    """
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    code_path = _setup_code(mini_project)
    _write_doc(
        docs_dir,
        "spec.json",
        {
            "title": "Spec",
            "sections": [
                {
                    "id": "overview",
                    "heading": "Overview",
                    "content": "Documents foo.",
                    "links": [{"node_id": "proj::src.mod::foo"}],
                },
            ],
        },
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    _drift(code_path)
    builder.build(mini_project, project_id="proj", discovery_only=False)
    return "proj::docs.spec", "proj::docs.spec::overview"


def _setup_mixed_parent(mini_project: Path) -> tuple[str, str]:
    """Build a section with its own stale documents edge AND a stale child.

    Returns:
        Tuple of (mixed parent node ID, stale child node ID).
    """
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    src_dir = mini_project / "src"
    src_dir.mkdir(exist_ok=True)
    code_x = src_dir / "alpha.py"
    code_x.write_text("def foo():\n    return 0\n", encoding="utf-8")
    code_y = src_dir / "beta.py"
    code_y.write_text("def bar():\n    return 0\n", encoding="utf-8")
    _write_doc(
        docs_dir,
        "guide.json",
        {
            "title": "Guide",
            "sections": [
                {
                    "id": "parent",
                    "heading": "Parent",
                    "content": "Documents alpha.foo directly.",
                    "links": [{"node_id": "proj::src.alpha::foo"}],
                    "sections": [
                        {
                            "id": "child",
                            "heading": "Child",
                            "content": "Documents beta.bar.",
                            "links": [{"node_id": "proj::src.beta::bar"}],
                        },
                    ],
                },
            ],
        },
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    code_x.write_text("def foo():\n    return 1\n", encoding="utf-8")
    code_y.write_text("def bar():\n    return 1\n", encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)
    return "proj::docs.guide::parent", "proj::docs.guide::parent.child"


def test_cli_mark_clean_inherited_renders_warning_and_descendants(mini_project: Path):
    """The CLI warns that the LINKED_STALE is inherited and lists descendants."""
    envelope_id, section_id = _setup_inherited_envelope(mini_project)

    time.sleep(0.05)
    result = CliRunner().invoke(
        cmd_mark_clean,
        [envelope_id, str(mini_project), "--reason", "Doc read end to end."],
    )

    assert result.exit_code == 0
    assert f"Marked '{envelope_id}' as MANUAL_VERIFIED." in result.output
    assert "Warning: LINKED_STALE on this node is inherited — marking it clean has no direct effect." in result.output
    assert "Stale descendants to mark clean:" in result.output
    assert f"  - {section_id}" in result.output
    assert "Reason: Doc read end to end." in result.output


def test_cli_mark_clean_mixed_renders_own_cleared_inherited_remains(mini_project: Path):
    """The CLI reports own signal cleared while naming the surviving inherited stale child."""
    parent_id, child_id = _setup_mixed_parent(mini_project)

    time.sleep(0.05)
    result = CliRunner().invoke(
        cmd_mark_clean,
        [parent_id, str(mini_project), "--reason", "Parent prose verified."],
    )

    assert result.exit_code == 0
    assert f"Marked '{parent_id}' as MANUAL_VERIFIED." in result.output
    assert "Own stale signal cleared; LINKED_STALE inherited from stale descendants remains:" in result.output
    assert f"  - {child_id}" in result.output
    # The mixed shape must not be conflated with the inherited-only warning.
    assert "Warning: LINKED_STALE on this node is inherited" not in result.output
    assert "Reason: Parent prose verified." in result.output
