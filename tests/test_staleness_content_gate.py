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

import os
import time
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.index import builder, db
from axiom_graph.index.staleness import (
    _file_content_matches_anchor,
    compute_staleness,
)
from axiom_graph.lifecycle.api import build_index, compute_check_summary, purge_nodes
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


# ---------------------------------------------------------------------------
# The fast pass confirms, never promotes
# ---------------------------------------------------------------------------

_MODULE = "proj::pkg.m"
_KEEP = "proj::pkg.m::keep"
_EDITED = "proj::pkg.m::edited"
_GONE = "proj::pkg.m::gone"


def _write_module(project_root: Path, *, edited_returns: int = 1, with_gone: bool = True) -> Path:
    pkg = project_root / "pkg"
    pkg.mkdir(exist_ok=True)
    py = pkg / "m.py"
    source = f"def keep():\n    return 1\n\n\ndef edited():\n    return {edited_returns}\n"
    if with_gone:
        source += "\n\ndef gone():\n    return 1\n"
    py.write_text(source, encoding="utf-8")
    return py


def _own_status(db_path: Path, project_root: Path, node_id: str) -> str:
    return compute_check_summary(db_path, project_root).statuses[node_id][0]


def _spy_on_per_node_ladder(monkeypatch) -> dict[str, int]:
    """Count calls to the per-node hashing ladder that step 2 falls back to."""
    import axiom_graph.scanners.node_hashing as node_hashing

    calls = {"n": 0}
    real = node_hashing.current_node_hashes_for_file

    def _spy(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(node_hashing, "current_node_hashes_for_file", _spy)
    return calls


@workflow(
    purpose=(
        "A developer cleaning up a module whose functions were deleted and edited "
        "tries to purge the module node, which purge refuses while its file is on disk, "
        "and rebuilds: the deleted function stays NOT_FOUND and the edited one "
        "CONTENT_UPDATED on every later build, with or without touching the file, so "
        "the deleted row can then be purged normally"
    ),
)
def test_rebuilds_after_a_refused_module_purge_never_launder_its_functions(mini_project: Path, db_path: Path):
    口 = Step(
        step_num=1,
        name="Index a module with three functions",
        purpose="keep, edited and gone all start VERIFIED against their baselines",
    )
    _write_module(mini_project)
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    口 = Step(
        step_num=2,
        name="Delete gone, edit edited, rebuild",
        purpose="The real drift is recorded and the module reads NOT_FOUND from its removed child",
    )
    time.sleep(0.05)
    _write_module(mini_project, edited_returns=2, with_gone=False)
    build_index(db_path, mini_project, project_id="proj")
    assert _own_status(db_path, mini_project, _GONE) == "NOT_FOUND"
    assert _own_status(db_path, mini_project, _EDITED) == "CONTENT_UPDATED"

    口 = Step(
        step_num=3,
        name="Try to purge the module node",
        purpose="Its file is on disk, so its NOT_FOUND is inherited: purge refuses it and the module stays",
    )
    refused = purge_nodes(db_path, mini_project, [_MODULE], "ghost module", actor="agent")[0]
    assert not refused.purged
    assert db.get_node(db_path, _MODULE) is not None

    口 = Step(
        step_num=4,
        name="Rebuild twice without touching the file",
        purpose="The file's hash matches the module's by construction — the fast pass must still not promote the drifted rows",
    )
    for _ in range(2):
        build_index(db_path, mini_project, project_id="proj")
        assert db.get_node(db_path, _MODULE) is not None
        assert _own_status(db_path, mini_project, _GONE) == "NOT_FOUND"
        assert _own_status(db_path, mini_project, _EDITED) == "CONTENT_UPDATED"
        assert _own_status(db_path, mini_project, _KEEP) == "VERIFIED"

    口 = Step(
        step_num=5,
        name="Purge the deleted function",
        purpose="It is still NOT_FOUND, so purge accepts it",
    )
    assert purge_nodes(db_path, mini_project, [_GONE], "function deleted", actor="agent")[0].purged
    assert db.get_node(db_path, _GONE) is None


@workflow(
    purpose=(
        "An unchanged file whose nodes are all already VERIFIED still takes the "
        "mtime fast pass: staleness confirms it without re-hashing any node"
    ),
)
def test_unchanged_fully_verified_file_takes_the_fast_pass(mini_project: Path, db_path: Path, monkeypatch):
    _write_module(mini_project)
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    build_index(db_path, mini_project, project_id="proj")

    calls = _spy_on_per_node_ladder(monkeypatch)
    result = compute_staleness(db_path, mini_project, db.all_nodes(db_path))

    assert result[_KEEP][0] == "VERIFIED"
    assert result[_GONE][0] == "VERIFIED"
    assert calls["n"] == 0, "an unchanged, fully VERIFIED file must not run the per-node ladder"


@workflow(
    purpose=(
        "Purging the module node of a deleted file whose function rows still carry a "
        "stored file mtime (written by an older version) makes the next build rescan "
        "the file once it is restored with that same mtime, so the module node is "
        "re-created instead of staying missing"
    ),
)
def test_purged_anchor_is_recreated_when_function_rows_carry_a_stored_mtime(mini_project: Path, db_path: Path):
    py = _write_module(mini_project)
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    # Index state left by an older version: every row at the location
    # carries the file's mtime, not just the module node.
    with db._connect(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET file_mtime = (SELECT file_mtime FROM nodes WHERE id = ?) WHERE location = ?",
            (_MODULE, "pkg/m.py"),
        )
    stored = py.stat()
    source = py.read_bytes()
    assert db.get_file_mtime(db_path, "pkg/m.py") == stored.st_mtime

    # The file is deleted, its module node purged, then the file comes back
    # unchanged with its old mtime (as a restore from backup would leave it).
    py.unlink()
    compute_check_summary(db_path, mini_project)
    assert purge_nodes(db_path, mini_project, [_MODULE], "module deleted", actor="agent")[0].purged
    py.write_bytes(source)
    os.utime(py, ns=(stored.st_atime_ns, stored.st_mtime_ns))

    build_index(db_path, mini_project, project_id="proj")

    assert db.get_node(db_path, _MODULE) is not None
