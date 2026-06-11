"""Regression: mark_clean must not freeze a node's scan-derived summary.

Bug: ``mark_clean`` advanced the node's ``file_mtime`` to the current on-disk
mtime.  The builder's mtime fast-pass (``axiom_graph/index/builder.py``) then
treated the file as already-scanned and skipped it on every subsequent
``discovery_only`` build, so the scanner never regenerated ``level_1`` /
``level_2`` (or any other scan-derived field).  The node read VERIFIED with a
correct hash baseline, but its human-readable signature (``level_1``) stayed
stale forever, and the file-level ``MAX(file_mtime)`` masked genuinely-stale
sibling nodes in the same file.

Root cause: ``file_mtime`` did double duty as both the staleness baseline and
the scan-skip cache.  ``mark_clean`` is the only incremental path that advanced
it.  Fix (Option B): ``mark_clean`` no longer writes ``file_mtime``, so the next
build re-scans the changed file and regenerates all scan-derived fields.

Both tests drive the real ``build_index`` path (whose fast-pass uses
``MAX(file_mtime)`` per location, making the skip decision deterministic) and
observe the user-facing symptom: a frozen ``level_1`` signature.
"""

from __future__ import annotations

import os
from pathlib import Path

from axiom_graph.db.staleness import get_file_mtime
from axiom_graph.index import db
from axiom_graph.lifecycle.api import build_index


def _func_nodes(db_path: Path) -> dict[str, object]:
    """Return {title: node} for atomic_process nodes in mod.py."""
    return {n.title: n for n in db.all_nodes(db_path) if n.location == "mod.py" and n.node_type == "atomic_process"}


def test_mark_clean_does_not_freeze_own_level_1(mini_project: Path, db_path: Path) -> None:
    """After mark_clean + build, the node's own level_1 reflects the new signature."""
    root = mini_project
    py_file = root / "mod.py"
    py_file.write_text("def my_func(a):\n    return a\n", encoding="utf-8")

    # Build 1: index the node with its original signature.
    build_index(db_path, root, project_id="proj")
    func_id = _func_nodes(db_path)["my_func"].id
    original = db.get_node(db_path, func_id)
    assert "new_param" not in original.level_1
    # file_mtime lives on the module node (function rows carry None); the
    # fast-pass reads it via get_file_mtime per location.
    build1_mtime = get_file_mtime(db_path, "mod.py")
    assert build1_mtime is not None

    # Edit the signature, forcing the on-disk mtime strictly forward so the
    # fast-pass decision is deterministic regardless of FS timestamp resolution.
    py_file.write_text("def my_func(a, new_param):\n    return a\n", encoding="utf-8")
    later = build1_mtime + 10.0
    os.utime(py_file, (later, later))

    # mark_clean BEFORE re-building -- the trigger for the bug.
    from axiom_graph.mcp_server import axiom_graph_mark_clean

    axiom_graph_mark_clean(str(root), node_id=func_id, reason="signature looks fine")

    # Build 2 (incremental / discovery_only -- the default and MCP path).
    build_index(db_path, root, project_id="proj")

    updated = db.get_node(db_path, func_id)
    assert "new_param" in updated.level_1, f"level_1 was frozen by mark_clean's mtime write; got {updated.level_1!r}"


def test_mark_clean_does_not_freeze_sibling_level_1(mini_project: Path, db_path: Path) -> None:
    """mark_clean on one node must not freeze a changed sibling in the same file."""
    root = mini_project
    py_file = root / "mod.py"
    py_file.write_text(
        "def func_a(a):\n    return a\n\n\ndef func_b(b):\n    return b\n",
        encoding="utf-8",
    )

    # Build 1: index both functions.
    build_index(db_path, root, project_id="proj")
    nodes = _func_nodes(db_path)
    a_id = nodes["func_a"].id
    b_id = nodes["func_b"].id
    build1_mtime = get_file_mtime(db_path, "mod.py")
    assert build1_mtime is not None

    # Change func_b's signature; func_a is untouched.
    py_file.write_text(
        "def func_a(a):\n    return a\n\n\ndef func_b(b, new_param):\n    return b\n",
        encoding="utf-8",
    )
    later = build1_mtime + 10.0
    os.utime(py_file, (later, later))

    # mark_clean only func_a (the unchanged node).  Under the bug this advances
    # the file-level MAX(file_mtime), masking func_b's real change on next build.
    from axiom_graph.mcp_server import axiom_graph_mark_clean

    axiom_graph_mark_clean(str(root), node_id=a_id, reason="func_a unchanged")

    # Build 2.
    build_index(db_path, root, project_id="proj")

    updated_b = db.get_node(db_path, b_id)
    assert "new_param" in updated_b.level_1, (
        f"sibling func_b's level_1 was frozen by mark_clean on func_a; got {updated_b.level_1!r}"
    )
