"""Tests for the staleness content-hash gate (cycle
pev-2026-06-11-staleness-content-hash-gate).

The mtime fast-pass in ``compute_staleness`` step 2 no longer trusts the
filesystem clock alone: inside the "unchanged" branch it confirms the file's
bytes against the file-level anchor node's whole-file ``code_hash`` before
blanket-verifying.  These tests prove:

- US-1: bytes change + mtime rolled back -> CONTENT_UPDATED (not VERIFIED)
- US-2: byte-identical at any mtime (older or newer) -> VERIFIED via fingerprint
- gate safety: anchor missing / empty code_hash -> fall through to the ladder
- US-4: scan_module / scan_js_module emit subtype="module"; Python module
  code_hash == hash16(source)
"""

from __future__ import annotations

from pathlib import Path

from axiom_annotations import workflow

from axiom_graph.index import builder, db
from axiom_graph.index.staleness import (
    _file_content_matches_anchor,
    compute_staleness,
)
from axiom_graph.models import AxiomNode, hash16
from axiom_graph.scanners.module_scanner import scan_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _module_anchor(node_id: str, location: str, code_hash: str, mtime: float) -> AxiomNode:
    """A file-level Python module anchor node (subtype="module")."""
    return AxiomNode(
        id=node_id,
        node_type="composite_process",
        subtype="module",
        title=node_id.split("::")[-1],
        location=location,
        source="ast",
        code_hash=code_hash,
        level_0=node_id,
        level_1=node_id,
        file_mtime=mtime,
    )


def _func_node(node_id: str, location: str, code_hash: str, mtime: float) -> AxiomNode:
    """An atomic_process function node living in the same file."""
    return AxiomNode(
        id=node_id,
        node_type="atomic_process",
        title=node_id.split("::")[-1],
        location=location,
        source="ast",
        code_hash=code_hash,
        level_0=node_id,
        level_1=node_id,
        file_mtime=mtime,
    )


# ---------------------------------------------------------------------------
# US-1 — content drift caught despite a rolled-back mtime
# ---------------------------------------------------------------------------


@workflow(
    purpose="Bytes change but mtime is rolled back to/below the stored value -> "
    "the content gate reports CONTENT_UPDATED instead of blanket VERIFIED"
)
def test_content_drift_with_rolledback_mtime_is_not_verified(mini_project: Path, db_path: Path):
    project_root = mini_project
    src = project_root / "src"
    src.mkdir()
    py = src / "mod.py"
    location = "src/mod.py"

    # C0 — original content, index it.
    py.write_text("def foo():\n    return 1\n", encoding="utf-8")
    builder.build(project_root, project_id="proj", discovery_only=False)
    nodes = db.all_nodes(db_path)
    stored_mtime = py.stat().st_mtime

    # C1 — rewrite bytes, then roll the mtime BACK to <= the stored value.
    py.write_text("def foo():\n    return 999\n", encoding="utf-8")
    import os

    os.utime(py, (stored_mtime - 5, stored_mtime - 5))
    assert py.stat().st_mtime <= stored_mtime  # mtime fast-pass would say "unchanged"

    result = compute_staleness(db_path, project_root, nodes)
    foo_id = "proj::src.mod::foo"
    own, _link, _via = result[foo_id]
    assert own == "CONTENT_UPDATED", f"expected CONTENT_UPDATED, got {own}"


# ---------------------------------------------------------------------------
# US-2 — byte-identical at any mtime stays VERIFIED via fingerprint
# ---------------------------------------------------------------------------


@workflow(
    purpose="Byte-identical file at an OLDER and a NEWER mtime than stored -> "
    "VERIFIED both ways; at the unchanged-mtime end the fingerprint fast-path "
    "verifies WITHOUT invoking the per-node ladder"
)
def test_byte_identical_any_mtime_is_verified(mini_project: Path, db_path: Path, monkeypatch):
    project_root = mini_project
    src = project_root / "src"
    src.mkdir()
    py = src / "mod.py"

    py.write_text("def foo():\n    return 1\n", encoding="utf-8")
    builder.build(project_root, project_id="proj", discovery_only=False)
    nodes = db.all_nodes(db_path)
    stored_mtime = py.stat().st_mtime

    # Spy: count ladder invocations so we can assert the fast-path skips it.
    import axiom_graph.scanners.node_hashing as node_hashing

    calls = {"n": 0}
    real = node_hashing.current_node_hashes_for_file

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(node_hashing, "current_node_hashes_for_file", _spy)

    import os

    foo_id = "proj::src.mod::foo"

    # Older / equal mtime -> mtime fast-pass fires; gate confirms identical
    # bytes -> VERIFIED via fingerprint, ladder NOT invoked.
    os.utime(py, (stored_mtime - 10, stored_mtime - 10))
    result = compute_staleness(db_path, project_root, nodes)
    assert result[foo_id][0] == "VERIFIED"
    assert calls["n"] == 0, "fingerprint hit must not run the per-node ladder"

    # Newer mtime -> fast-pass legitimately does not fire (mtime advanced);
    # the file still hashes identical so the ladder yields VERIFIED.
    os.utime(py, (stored_mtime + 10, stored_mtime + 10))
    result = compute_staleness(db_path, project_root, nodes)
    assert result[foo_id][0] == "VERIFIED"


# ---------------------------------------------------------------------------
# Gate safety — missing / empty anchor falls through (never blanket-VERIFY)
# ---------------------------------------------------------------------------


@workflow(
    purpose="When no file-level anchor exists or its code_hash is empty, the gate "
    "returns False so the caller falls through to the per-node ladder"
)
def test_gate_falls_through_when_anchor_absent_or_empty(tmp_path: Path):
    py = tmp_path / "mod.py"
    py.write_text("def foo():\n    return 1\n", encoding="utf-8")
    file_hash = hash16(py.read_text(encoding="utf-8", errors="replace"))

    # No anchor among loc_nodes (only an atomic function) -> fall through.
    only_func = [_func_node("proj::src.mod::foo", "src/mod.py", "abc", 0.0)]
    assert _file_content_matches_anchor(py, only_func) is False

    # Anchor present but empty code_hash -> fall through.
    empty_anchor = _module_anchor("proj::src.mod", "src/mod.py", "", 0.0)
    assert _file_content_matches_anchor(py, [empty_anchor]) is False

    # Anchor present with a MATCHING hash -> True (fast win preserved).
    good_anchor = _module_anchor("proj::src.mod", "src/mod.py", file_hash, 0.0)
    assert _file_content_matches_anchor(py, [good_anchor]) is True

    # Anchor present with a NON-matching hash -> fall through.
    stale_anchor = _module_anchor("proj::src.mod", "src/mod.py", "deadbeefdeadbeef", 0.0)
    assert _file_content_matches_anchor(py, [stale_anchor]) is False


# ---------------------------------------------------------------------------
# US-4 — Python anchors addressable (subtype="module") + whole-file hash
# ---------------------------------------------------------------------------


@workflow(
    purpose="scan_module on a valid .py and a syntax-error .py both emit a module "
    "node with subtype='module'; the main node's code_hash == hash16(source)"
)
def test_scan_module_emits_module_subtype_and_wholefile_hash(tmp_path: Path):
    project_root = tmp_path

    # Valid module.
    good = project_root / "good.py"
    source = "def foo():\n    return 1\n"
    good.write_text(source, encoding="utf-8")
    nodes, _ = scan_module(good, project_root, "proj")
    module_node = next(n for n in nodes if n.id == "proj::good")
    assert module_node.subtype == "module"
    assert module_node.code_hash == hash16(source)

    # Syntax-error module -> stub node, still subtype="module".
    bad = project_root / "bad.py"
    bad_source = "def foo(:\n  pass\n"
    bad.write_text(bad_source, encoding="utf-8")
    bad_nodes, _ = scan_module(bad, project_root, "proj")
    assert len(bad_nodes) == 1
    stub = bad_nodes[0]
    assert stub.subtype == "module"
    assert stub.code_hash == hash16(bad_source)
