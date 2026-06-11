"""Unit tests for :func:`axiom_graph.scanners.node_hashing.parse_node_title`.

The parser is a small read-only helper that consults ``node.id`` and
``node.title`` to surface three pieces of information that several call
sites previously open-coded with brittle ``.title.split(...)`` patterns:

* ``qualified``  -- e.g. ``"TestFoo.test_bar"`` or ``"my_func"`` (read
  from the trailing ``::`` segment of ``node.id``, with any
  ``@workflow`` envelope suffix stripped).
* ``last``       -- the trailing dot segment of ``qualified``.
* ``envelope_kind`` -- ``"workflow"`` / ``"task"`` if the *title* ends
  with the corresponding ``" @workflow"`` / ``" @task"`` annotation,
  otherwise ``None``.

Both surfaces are read independently: the parser does *not* cross-validate
them.  This is intentional -- during early indexing, a node may briefly
hold an id from one revision and a title from another.
"""

from __future__ import annotations

import pytest

from axiom_graph.models import AxiomNode
from axiom_graph.scanners.node_hashing import NodeTitle, parse_node_title


def _node(node_id: str, title: str, *, subtype: str | None = None) -> AxiomNode:
    """Build a minimal AxiomNode for parser exercise -- no DB needed."""
    return AxiomNode(
        id=node_id,
        node_type="atomic_process",
        subtype=subtype,
        title=title,
        location=None,
        source="ast",
        code_hash=None,
        desc_hash=None,
        level_0=node_id,
        level_1=node_id,
    )


# ---------------------------------------------------------------------------
# Happy paths -- parametrised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("node_id", "title", "subtype", "expected_qualified", "expected_last", "expected_envelope_kind"),
    [
        # Bare top-level function.
        (
            "proj::module::foo",
            "foo",
            "function",
            "foo",
            "foo",
            None,
        ),
        # Class.method.
        (
            "proj::module::TestFoo.test_bar",
            "TestFoo.test_bar",
            "test",
            "TestFoo.test_bar",
            "test_bar",
            None,
        ),
        # Envelope on a bare function -- id has @workflow suffix, title has " @task".
        (
            "proj::module::foo@workflow",
            "foo @task",
            "task",
            "foo",
            "foo",
            "task",
        ),
        # Envelope on a Class.method -- id has @workflow suffix, title has " @workflow".
        (
            "proj::module::TestFoo.test_bar@workflow",
            "TestFoo.test_bar @workflow",
            "workflow",
            "TestFoo.test_bar",
            "test_bar",
            "workflow",
        ),
    ],
    ids=[
        "bare-function",
        "class-method",
        "envelope-task-on-bare",
        "envelope-workflow-on-method",
    ],
)
def test_parse_node_title_happy_paths(
    node_id: str,
    title: str,
    subtype: str | None,
    expected_qualified: str,
    expected_last: str,
    expected_envelope_kind: str | None,
) -> None:
    parsed = parse_node_title(_node(node_id, title, subtype=subtype))
    assert isinstance(parsed, NodeTitle)
    assert parsed.qualified == expected_qualified
    assert parsed.last == expected_last
    assert parsed.envelope_kind == expected_envelope_kind


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_parse_node_title_degenerate_id_no_double_colon() -> None:
    """``id`` without any ``::`` separator -- must not raise."""
    parsed = parse_node_title(_node("foo", ""))
    assert parsed.qualified == "foo"
    assert parsed.last == "foo"
    assert parsed.envelope_kind is None


def test_parse_node_title_empty_title_with_valid_id() -> None:
    """Empty title -> ``envelope_kind=None``; qualified/last from id."""
    parsed = parse_node_title(_node("proj::module::my_func", ""))
    assert parsed.qualified == "my_func"
    assert parsed.last == "my_func"
    assert parsed.envelope_kind is None


def test_parse_node_title_id_without_workflow_suffix_but_title_has_envelope() -> None:
    """Title says ``@workflow``, id has no envelope suffix.

    Possible during early indexing of a renamed function.  Trust each
    surface independently -- ``envelope_kind`` from title, qualified
    from id.  No cross-validation, no error.
    """
    parsed = parse_node_title(_node("proj::module::renamed_func", "renamed_func @workflow"))
    assert parsed.qualified == "renamed_func"
    assert parsed.last == "renamed_func"
    assert parsed.envelope_kind == "workflow"


def test_parse_node_title_id_only_envelope_no_module() -> None:
    """Edge: id is bare (``foo@workflow``) with envelope suffix and no ``::``."""
    parsed = parse_node_title(_node("foo@workflow", "foo @task"))
    assert parsed.qualified == "foo"
    assert parsed.last == "foo"
    assert parsed.envelope_kind == "task"


def test_parse_node_title_returns_frozen_dataclass() -> None:
    """``NodeTitle`` should be immutable -- assignment raises."""
    parsed = parse_node_title(_node("proj::module::foo", "foo"))
    with pytest.raises((AttributeError, Exception)):
        parsed.qualified = "other"  # type: ignore[misc]
