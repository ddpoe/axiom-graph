"""The annotation findings store: build writes it, check reads it.

Behavioural tests enter through ``lifecycle.api`` (``build_index`` and
``read_annotation_findings``).  The store-equivalence test compares check's
read with a fresh full scan over the build's file set, built from the same
primitives the build uses.
"""

from __future__ import annotations

import os
import shutil
import time
from collections import Counter
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.config import AxiomGraphConfig
from axiom_graph.index import annotation_findings, builder, db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, read_annotation_findings
from axiom_graph.scanners import module_scanner
from axiom_graph.scanners.js_scanner import HAS_TREE_SITTER

JS_FIXTURE_DIR = Path(__file__).parent / "scanners" / "fixtures" / "js"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _touch_later(path: Path) -> None:
    """Append a blank line and move the mtime forward, so the build sees an edit."""
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))


def _b4(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["rule_id"] == "B4"]


def _write_b4_project(root: Path) -> Path:
    """Caller with an AutoStep to a decorated target and one to an undecorated helper, each in its own module."""
    _write(root / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(
        root / "target.py",
        'from axiom_annotations import task\n\n\n@task(purpose="does the work")\ndef do_work():\n    return 1\n',
    )
    _write(root / "helper.py", "def plain_helper():\n    return 2\n")
    return _write(
        root / "caller.py",
        "from axiom_annotations import AutoStep, workflow\n\n"
        "from helper import plain_helper\n"
        "from target import do_work\n\n\n"
        '@workflow(purpose="calls a decorated target and an undecorated helper")\n'
        "def run():\n"
        '    _ = AutoStep(step_num=1, name="work")\n'
        "    do_work()\n"
        '    _ = AutoStep(step_num=2, name="help")\n'
        "    plain_helper()\n",
    )


def _assert_only_helper_flagged(findings: list[dict]) -> None:
    b4 = _b4(findings)
    assert len(b4) == 1, b4
    assert "plain_helper" in b4[0]["message"] and "unresolved" not in b4[0]["message"], b4
    assert not any("do_work" in f["message"] for f in b4), b4


@workflow(
    purpose=(
        "B4 flags only the AutoStep whose target lacks a decorator, on the build that scans everything and on a "
        "build that rescans only the caller while the target's file is skipped"
    )
)
def test_b4_flags_only_undecorated_target_across_two_builds(tmp_path: Path) -> None:
    口 = Step(
        step_num=1,
        name="Build a project with a decorated and an undecorated AutoStep target",
        purpose="Every file is scanned; B4 is resolved against the whole index",
    )
    caller = _write_b4_project(tmp_path)
    dbp = _db_path(str(tmp_path))
    first = build_index(dbp, tmp_path)
    _assert_only_helper_flagged(first.annotation_findings)

    口 = Step(
        step_num=2,
        name="Edit only the caller and build again",
        purpose="The target and helper files are mtime-skipped; their decorators are known only from the index",
    )
    _touch_later(caller)
    second = build_index(dbp, tmp_path)
    assert second.files_skipped_mtime >= 2, second
    _assert_only_helper_flagged(second.annotation_findings)
    assert second.annotation_findings_new == 0

    口 = Step(
        step_num=3,
        name="Read the findings the way check does",
        purpose="Check's read agrees with the build and parses nothing",
    )
    read = read_annotation_findings(dbp, tmp_path)
    assert read.files_rescanned == 0
    _assert_only_helper_flagged(read.findings)


@workflow(
    purpose=(
        "A decorated AutoStep target reached through a package that star re-exports its defining module draws "
        "neither an undecorated nor an unresolved finding"
    )
)
def test_b4_follows_package_reexport_to_decorated_target(tmp_path: Path) -> None:
    _write(tmp_path / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(tmp_path / "pkg" / "__init__.py", "from pkg.impl import *  # noqa: F403\n")
    _write(
        tmp_path / "pkg" / "impl.py",
        'from axiom_annotations import task\n\n\n@task(purpose="stores a row")\ndef upsert_row():\n    return 1\n',
    )
    _write(
        tmp_path / "caller.py",
        "from axiom_annotations import AutoStep, workflow\n\n"
        "import pkg\n"
        "from pkg import upsert_row\n\n\n"
        '@workflow(purpose="calls through the package")\n'
        "def run():\n"
        '    _ = AutoStep(step_num=1, name="by attribute")\n'
        "    pkg.upsert_row()\n"
        '    _ = AutoStep(step_num=2, name="by name")\n'
        "    upsert_row()\n",
    )
    dbp = _db_path(str(tmp_path))
    summary = build_index(dbp, tmp_path)
    assert _b4(summary.annotation_findings) == []
    assert _b4(read_annotation_findings(dbp, tmp_path).findings) == []


def _fresh_full_scan(root: Path) -> list[dict]:
    """Scan every file the build walks, in memory, and resolve B4 against the index."""
    config = AxiomGraphConfig.load(root)
    skip_dirs = builder._BASE_SKIP_DIRS | frozenset(config.scan.exclude_dirs)
    py_files = list(builder._iter_python_files(root, skip_dirs))
    js_files = list(builder._iter_js_files(root, config.scan.js_paths, skip_dirs)) if HAS_TREE_SITTER else []
    results, nodes, edges = annotation_findings.rescan_in_memory(root, "proj", py_files, js_files)
    dbp = _db_path(str(root))
    live_ids = annotation_findings.live_node_lookup(dbp, root, nodes)
    with db._connect(dbp) as conn, live_ids.reading_on(conn):
        star, named = annotation_findings.overlaid_reexport_relation(conn, nodes, edges)
        outcome = annotation_findings.compute_findings(
            db.StoredAnnotations(),
            walked=results,
            rescanned=results,
            live_ids=live_ids,
            star=star,
            named=named,
            is_rule_enabled=config.validation.is_enabled,
        )
    return outcome.findings


def _as_multiset(findings: list[dict]) -> Counter:
    return Counter((*annotation_findings.finding_identity(f), f["line"]) for f in findings)


@workflow(
    purpose=(
        "With nothing edited since the build, check's findings read from the store equal a fresh full scan of the "
        "build's file set, and the read parses no source file"
    )
)
def test_idle_read_equals_fresh_full_scan_and_parses_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toml = '[axiom_graph]\nproject_id = "proj"\n'
    if HAS_TREE_SITTER:
        toml += '\n[axiom_graph.scan]\njs_paths = ["web/*.ts"]\n'
        (tmp_path / "web").mkdir()
        shutil.copy(JS_FIXTURE_DIR / "b1_duplicate.ts", tmp_path / "web" / "b1_duplicate.ts")
    _write_b4_project(tmp_path)
    _write(tmp_path / "axiom-graph.toml", toml)
    _write(
        tmp_path / "dup.py",
        "from axiom_annotations import Step, workflow\n\n\n"
        '@workflow(purpose="duplicate step numbers")\n'
        "def run_demo():\n"
        "    _ = Step(step_num=1, name='one', purpose='first')\n"
        "    _ = Step(step_num=1, name='two', purpose='second')\n",
    )
    dbp = _db_path(str(tmp_path))
    build_index(dbp, tmp_path)
    expected = _fresh_full_scan(tmp_path)
    rules = {f["rule_id"] for f in expected}
    assert {"B1", "B4"} <= rules, expected
    if HAS_TREE_SITTER:
        assert any(f["module"] == "web/b1_duplicate.ts" for f in expected), expected

    parsed: list[Path] = []

    def _no_parse(path, *args, **kwargs):
        parsed.append(path)
        raise AssertionError(f"an idle read parsed {path}")

    monkeypatch.setattr(module_scanner, "scan_module", _no_parse)
    if HAS_TREE_SITTER:
        from axiom_graph.scanners import js_scanner, xstate_scanner  # noqa: PLC0415 -- optional tree-sitter

        monkeypatch.setattr(js_scanner, "scan_js_module", _no_parse)
        monkeypatch.setattr(xstate_scanner, "scan_xstate_module", _no_parse)

    read = read_annotation_findings(dbp, tmp_path)
    assert parsed == []
    assert read.files_rescanned == 0
    assert _as_multiset(read.findings) == _as_multiset(expected)
    assert read.new == 0 and read.resolved == 0


@workflow(
    purpose=(
        "Check reads findings over the build's file set: files under an agent worktree or a configured "
        "exclude_dirs entry are not reported"
    )
)
def test_check_skips_worktree_and_excluded_dirs(tmp_path: Path) -> None:
    violation = (
        "from axiom_annotations import Step, workflow\n\n\n"
        '@workflow(purpose="duplicate step numbers")\n'
        "def run_demo():\n"
        "    _ = Step(step_num=1, name='one', purpose='first')\n"
        "    _ = Step(step_num=1, name='two', purpose='second')\n"
    )
    _write(
        tmp_path / "axiom-graph.toml",
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\nexclude_dirs = ["vendored"]\n',
    )
    _write(tmp_path / "clean.py", "def ok():\n    return 1\n")
    _write(tmp_path / ".claude" / "worktrees" / "x" / "mod.py", violation)
    _write(tmp_path / "vendored" / "mod.py", violation)
    dbp = _db_path(str(tmp_path))
    summary = build_index(dbp, tmp_path)
    assert summary.annotation_findings == []

    read = read_annotation_findings(dbp, tmp_path)
    assert read.findings == []
    assert read.files_rescanned == 0


def test_identity_ignores_line_moves_and_counts_duplicates() -> None:
    """A finding that only moved, line reference in its message included, is not new; duplicates count apart."""
    stored = {
        "module": "m.py",
        "rule_id": "B1",
        "function": "f",
        "line": 9,
        "message": "duplicate step_num 1 in 'f' (first seen at line 8)",
    }
    moved = {**stored, "line": 10, "message": "duplicate step_num 1 in 'f' (first seen at line 9)"}
    findings, resolved = annotation_findings.diff_findings([stored], [moved, moved])
    assert [f["new"] for f in findings] == [False, True]
    assert resolved == 0
    _, resolved = annotation_findings.diff_findings([stored, stored], [moved])
    assert resolved == 1


_DUP_STEPS = (
    "from axiom_annotations import Step, workflow\n\n\n"
    '@workflow(purpose="duplicate step numbers")\n'
    "def run_demo():\n"
    "    _ = Step(step_num=1, name='one', purpose='first')\n"
    "    _ = Step(step_num=1, name='two', purpose='second')\n"
)
_TOML = '[axiom_graph]\nproject_id = "proj"\n'


def _stored_files(dbp: Path) -> set[str]:
    return set(db.read_annotation_store(dbp).files)


def test_disabled_rule_is_neither_resolved_nor_new_when_reenabled(tmp_path: Path) -> None:
    """Disabling a rule hides its findings without resolving them; re-enabling shows them as not new."""
    _write(tmp_path / "axiom-graph.toml", _TOML)
    _write(tmp_path / "dup.py", _DUP_STEPS)
    dbp = _db_path(str(tmp_path))
    assert build_index(dbp, tmp_path).annotation_findings_new == 1

    _write(tmp_path / "axiom-graph.toml", _TOML + "\n[axiom_graph.validation.rules]\nB1 = false\n")
    read = read_annotation_findings(dbp, tmp_path)
    assert (read.findings, read.resolved) == ([], 0)
    built = build_index(dbp, tmp_path)
    assert (built.annotation_findings, built.annotation_findings_resolved) == ([], 0)

    _write(tmp_path / "axiom-graph.toml", _TOML)
    read = read_annotation_findings(dbp, tmp_path)
    assert [(f["rule_id"], f["new"]) for f in read.findings] == [("B1", False)]
    assert (read.new, read.resolved) == (0, 0)
    built = build_index(dbp, tmp_path)
    assert (built.annotation_findings_new, built.annotation_findings_resolved) == (0, 0)


@pytest.mark.parametrize(("change", "resolved"), [("file vanishes", 1), ("scan raises", 0)])
def test_vanished_file_resolves_and_failed_scan_keeps_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, resolved: int
) -> None:
    """A file gone from the walk drops its rows and reads as resolved; a file whose scan raises keeps them."""
    _write(tmp_path / "axiom-graph.toml", _TOML)
    dup = _write(tmp_path / "dup.py", _DUP_STEPS)
    _write(tmp_path / "plain.py", "def g():\n    return 2\n")
    dbp = _db_path(str(tmp_path))
    build_index(dbp, tmp_path)
    assert "dup.py" in _stored_files(dbp)

    if change == "file vanishes":
        dup.unlink()
    else:
        _touch_later(dup)
        real_scan = module_scanner.scan_module

        def _raise_on_dup(path, *args, **kwargs):
            if Path(path).name == "dup.py":
                raise RuntimeError("unparseable")
            return real_scan(path, *args, **kwargs)

        monkeypatch.setattr(module_scanner, "scan_module", _raise_on_dup)

    read = read_annotation_findings(dbp, tmp_path)
    built = build_index(dbp, tmp_path)
    for outcome_resolved, findings in (
        (read.resolved, read.findings),
        (built.annotation_findings_resolved, built.annotation_findings),
    ):
        assert outcome_resolved == resolved
        assert [f["rule_id"] for f in findings] == ([] if resolved else ["B1"])
    assert ("dup.py" in _stored_files(dbp)) is (not resolved)


_JS_B4 = (
    "import { workflow, Step, AutoStep } from 'axiom-annotations';\n\n"
    "export const run = workflow({purpose: 'orchestrate'})(async (cfg: any) => {\n"
    "  Step({stepNum: 1, name: 'Filter', purpose: 'Remove bad rows'});\n"
    "  AutoStep({stepNum: 2});\n"
    "  doWork();\n"
    "});\n"
)


def _web_rules(findings: list[dict]) -> list[str]:
    return sorted(f["rule_id"] for f in findings if f["module"].startswith("web/"))


@pytest.mark.skipif(not HAS_TREE_SITTER, reason="needs tree-sitter to store JS rows first")
def test_js_rows_hidden_without_tree_sitter_and_not_new_when_it_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without tree-sitter, stored JS/TS findings (B4 included) are neither current nor resolved, and return not new."""
    from axiom_graph.scanners import js_scanner  # noqa: PLC0415 -- optional tree-sitter

    _write(tmp_path / "axiom-graph.toml", _TOML + '\n[axiom_graph.scan]\njs_paths = ["web/*.ts"]\n')
    _write(tmp_path / "web" / "b1.ts", (JS_FIXTURE_DIR / "b1_duplicate.ts").read_text(encoding="utf-8"))
    _write(tmp_path / "web" / "flow.ts", _JS_B4)
    _write(tmp_path / "dup.py", _DUP_STEPS)
    dbp = _db_path(str(tmp_path))
    first = build_index(dbp, tmp_path)
    assert _web_rules(first.annotation_findings) == ["B1", "B4"], first.annotation_findings

    monkeypatch.setattr(js_scanner, "HAS_TREE_SITTER", False)
    read = read_annotation_findings(dbp, tmp_path)
    built = build_index(dbp, tmp_path)
    for findings, resolved in (
        (read.findings, read.resolved),
        (built.annotation_findings, built.annotation_findings_resolved),
    ):
        assert [f["module"] for f in findings] == ["dup.py"]
        assert resolved == 0

    monkeypatch.setattr(js_scanner, "HAS_TREE_SITTER", True)
    back = build_index(dbp, tmp_path)
    assert _web_rules(back.annotation_findings) == ["B1", "B4"]
    assert (back.annotation_findings_new, back.annotation_findings_resolved) == (0, 0)


def test_failed_store_write_leaves_scanned_files_to_be_rescanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When build cannot write the store, the files it scanned stay unstamped: check and the next build rescan them."""
    _write(tmp_path / "axiom-graph.toml", _TOML)
    dup = _write(tmp_path / "dup.py", _DUP_STEPS)
    _write(tmp_path / "plain.py", "def g():\n    return 2\n")
    dbp = _db_path(str(tmp_path))
    build_index(dbp, tmp_path)

    _write(dup, "def run_demo():\n    return 1\n")
    later = time.time() + 5
    os.utime(dup, (later, later))
    _write(tmp_path / "added.py", _DUP_STEPS.replace("run_demo", "run_added"))

    def _fail(*args, **kwargs):
        raise RuntimeError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(db, "replace_file_annotations_conn", _fail)
        failed = build_index(dbp, tmp_path)
    assert any("annotation findings could not be recorded" in w for w in failed.warnings), failed.warnings

    read = read_annotation_findings(dbp, tmp_path)
    assert read.files_rescanned == 2
    assert [(f["module"], f["rule_id"]) for f in read.findings] == [("added.py", "B1")]

    rebuilt = build_index(dbp, tmp_path)
    assert (rebuilt.files_scanned, rebuilt.files_skipped_mtime) == (2, 1)
    assert [(f["module"], f["rule_id"]) for f in rebuilt.annotation_findings] == [("added.py", "B1")]
    assert db.read_annotation_store(dbp).findings.get("dup.py", []) == []
    assert read_annotation_findings(dbp, tmp_path).files_rescanned == 0
