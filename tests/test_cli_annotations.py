"""Tier-3 CLI tests for the Phase 3.2 annotation validation surface.

Covers the build-summary findings block, `check --json` shape, and the
`--strict-annotations` exit-code gate.  These tests drive the CLI through
``click.testing.CliRunner`` so they exercise the real command surface.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom_annotations import workflow
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
    assert "Annotation findings" in result.output, result.output
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

    # Case 1: findings present → exit code 1.
    _write_b1_fixture(tmp_path)
    runner.invoke(cli, ["build", str(tmp_path)])
    r1 = runner.invoke(cli, ["check", str(tmp_path), "--strict-annotations"])
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
