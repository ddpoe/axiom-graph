"""Tier-3 CLI tests for the Phase 3.2 annotation validation surface.

Covers the build-summary findings block, `check --json` shape, and the
`--strict-annotations` exit-code gate.  These tests drive the CLI through
``click.testing.CliRunner`` so they exercise the real command surface.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from axiom_annotations import Step, workflow
from click.testing import CliRunner

from axiom_graph.cli import main as cli


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _write_b1_fixture(project_root: Path) -> None:
    """Write a module with a B1 (duplicate step_num) violation."""
    _write(
        project_root / "mymod.py",
        """\
from axiom_annotations import workflow, Step


@workflow(purpose="duplicate step numbers")
def run_demo():
    \"\"\"Demo.\"\"\"
    _ = Step(step_num=1, name='one', purpose='first')
    _ = Step(step_num=1, name='two', purpose='second')
""",
    )


def _write_clean_fixture(project_root: Path) -> None:
    """Write a clean module with no annotation violations."""
    _write(
        project_root / "mymod.py",
        """\
from axiom_annotations import workflow, Step


@workflow(purpose="clean workflow")
def run_demo():
    \"\"\"Demo.\"\"\"
    _ = Step(step_num=1, name='one', purpose='first')
    _ = Step(step_num=2, name='two', purpose='second')
""",
    )


@workflow(purpose="axiom-graph build prints an 'Annotation findings' block when scanner emits any")
def test_build_summary_includes_annotation_findings(tmp_path):
    """Running `axiom-graph build` should show the annotation findings block
    in its terminal output when the scanner emitted at least one finding.
    """
    _write_b1_fixture(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["build", str(tmp_path)])
    assert result.exit_code == 0, f"build failed: {result.output}"
    assert "Annotation findings: 1 (1 new, 0 resolved)" in result.output, result.output
    assert "B1" in result.output, result.output


@workflow(purpose="axiom-graph check --json emits annotation_findings as a top-level key with stable shape")
def test_check_json_has_annotation_findings(tmp_path):
    """The `--json` output of `axiom-graph check` must contain an
    ``annotation_findings`` list; each entry exposes a stable shape
    (rule_id, severity, module, function, line, message).
    """
    _write_b1_fixture(tmp_path)
    runner = CliRunner()
    # Build first so graph.db exists.
    build_r = runner.invoke(cli, ["build", str(tmp_path)])
    assert build_r.exit_code == 0, build_r.output

    result = runner.invoke(cli, ["check", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0, result.output
    # The output may have leading log noise; parse the last JSON object.
    data = json.loads(result.output[result.output.index("{") :])
    assert "annotation_findings" in data
    findings = data["annotation_findings"]
    assert isinstance(findings, list)
    assert any(f["rule_id"] == "B1" for f in findings), findings
    # Shape check — every finding has the expected keys.
    for f in findings:
        for k in ("rule_id", "severity", "module", "function", "line", "message"):
            assert k in f, f"missing key {k!r} in finding {f}"


@workflow(purpose="--strict-annotations exits 1 on findings, 0 without")
def test_strict_annotations_exit_code(tmp_path):
    """Gate behaviour for --strict-annotations."""
    runner = CliRunner()

    # Case 1: a finding the build already stored (so not new) → exit code 1.
    _write_b1_fixture(tmp_path)
    runner.invoke(cli, ["build", str(tmp_path)])
    r1 = runner.invoke(cli, ["check", str(tmp_path), "--strict-annotations"])
    assert "Annotation findings: 1 (0 new, 0 resolved)" in r1.output, r1.output
    assert r1.exit_code == 1, f"expected exit 1 with findings, got {r1.exit_code}: {r1.output}"

    # Case 2: no findings → exit code 0.
    _write_clean_fixture(tmp_path)
    runner.invoke(cli, ["build", str(tmp_path)])
    r2 = runner.invoke(cli, ["check", str(tmp_path), "--strict-annotations"])
    assert r2.exit_code == 0, f"expected exit 0 on clean project, got {r2.exit_code}: {r2.output}"


@workflow(purpose="--fail-on=stale composes with --strict-annotations (either trigger causes exit 1)")
def test_strict_annotations_composes_with_fail_on(tmp_path):
    """Running `--fail-on=stale --strict-annotations` with only annotation
    findings (no staleness) must exit 1 — the flags compose.
    """
    _write_b1_fixture(tmp_path)
    runner = CliRunner()
    build_r = runner.invoke(cli, ["build", str(tmp_path)])
    assert build_r.exit_code == 0

    r = runner.invoke(
        cli,
        ["check", str(tmp_path), "--fail-on=stale", "--strict-annotations"],
    )
    assert r.exit_code == 1, f"expected exit 1, got {r.exit_code}: {r.output}"


_SECOND_B1_FUNCTION = """


@workflow(purpose="another duplicate")
def run_other():
    \"\"\"Other.\"\"\"
    _ = Step(step_num=1, name='a', purpose='first')
    _ = Step(step_num=1, name='b', purpose='second')
"""


def _rewrite_later(path: Path, content: str) -> None:
    """Rewrite *path* and move its mtime forward, so the next command sees an edit."""
    path.write_text(content)
    later = time.time() + 5
    os.utime(path, (later, later))


def _listed(output: str) -> list[str]:
    """The finding lines a build or text check listed."""
    return [line.strip() for line in output.splitlines() if line.strip().startswith("! [")]


@workflow(
    purpose=(
        "An edit that adds a finding and moves an existing one is reported once: check and the next build list "
        "only the added finding, later runs list nothing new, and removing it reads as resolved"
    )
)
def test_new_finding_is_reported_once_and_resolves(tmp_path):
    runner = CliRunner()
    口 = Step(step_num=1, name="Build a project with one finding", purpose="The build stores the finding")
    _write_b1_fixture(tmp_path)
    module = tmp_path / "mymod.py"
    original = module.read_text()
    assert runner.invoke(cli, ["build", str(tmp_path)]).exit_code == 0

    口 = Step(
        step_num=2,
        name="Add a finding and shift the existing one down a line",
        purpose="A line-only move is not a new finding; the added one is",
    )
    _rewrite_later(module, "# a new first line\n" + original + _SECOND_B1_FUNCTION)

    口 = Step(
        step_num=3,
        name="Check lists only the new finding; JSON carries the full list with one new flag",
        purpose="check reads the store, rescans only the edited file, and never writes",
    )
    text = runner.invoke(cli, ["check", str(tmp_path)])
    assert "Annotation findings: 2 (1 new, 0 resolved)" in text.output, text.output
    listed = _listed(text.output)
    assert len(listed) == 1 and "run_other" in listed[0], text.output
    as_json = runner.invoke(cli, ["check", str(tmp_path), "--format", "json"])
    findings = json.loads(as_json.output[as_json.output.index("{") :])["annotation_findings"]
    assert len(findings) == 2, findings
    assert [f["function"] for f in findings if f["new"]] == ["run_other"], findings

    口 = Step(
        step_num=4,
        name="The next build reports it new once; later builds and checks do not",
        purpose="Unchanged findings are not repeated",
    )
    first = runner.invoke(cli, ["build", str(tmp_path)])
    assert "Annotation findings: 2 (1 new, 0 resolved)" in first.output, first.output
    assert len(_listed(first.output)) == 1 and "run_other" in _listed(first.output)[0], first.output
    second = runner.invoke(cli, ["build", str(tmp_path)])
    assert "Annotation findings: 2 (0 new, 0 resolved)" in second.output, second.output
    assert _listed(second.output) == [], second.output
    after = runner.invoke(cli, ["check", str(tmp_path)])
    assert "Annotation findings: 2 (0 new, 0 resolved)" in after.output, after.output
    assert _listed(after.output) == [], after.output

    口 = Step(step_num=5, name="Remove the added finding", purpose="The build counts it as resolved")
    _rewrite_later(module, original)
    removed = runner.invoke(cli, ["build", str(tmp_path)])
    assert "Annotation findings: 1 (0 new, 1 resolved)" in removed.output, removed.output


def test_check_reports_unreadable_findings_store_as_one_line_error(tmp_path):
    """An index from a newer schema ends check with a one-line error and exit 1, not a traceback."""
    import sqlite3  # noqa: PLC0415

    from axiom_graph.db.migrations import CURRENT_SCHEMA_VERSION  # noqa: PLC0415

    _write_b1_fixture(tmp_path)
    runner = CliRunner()
    assert runner.invoke(cli, ["build", str(tmp_path)]).exit_code == 0
    conn = sqlite3.connect(tmp_path / ".axiom_graph" / "graph.db")
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    result = runner.invoke(cli, ["check", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "Error: could not read annotation findings:" in result.output, result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit), result.exception
