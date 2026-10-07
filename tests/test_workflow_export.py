"""Tests for exporting workflows from the CLI and the MCP tool.

Both write the dashboard's export page, or its JSON bundle, to a file
without starting the viz server, so nothing here imports the viz extra.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from axiom_annotations import Step, workflow
from click.testing import CliRunner

from axiom_graph.cli import main
from axiom_graph.index import builder
from axiom_graph.workflows.api import workflow_bundle_to_dict, workflow_export_bundle
from axiom_graph.workflows.export import export_label, render_export_html
from axiom_graph.workflows.mcp_tools import axiom_graph_workflow_export


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _build_handoff(project_root: Path) -> None:
    """Write a workflow whose AutoStep hands off to a task in another file.

    ``pipeline.py`` holds ``run``, whose step 2 delegates to ``prep`` in
    ``tasks.py``, and ``report``, which delegates nowhere.
    """
    _write(
        project_root / "tasks.py",
        """\
from axiom_annotations import task, Step


@task(purpose="Prepare the rows")
def prep(rows):
    _ = Step(step_num=1, name='load rows', purpose='read the input')
    _ = Step(step_num=2, name='save rows', purpose='write the output')
""",
    )
    _write(
        project_root / "pipeline.py",
        """\
from axiom_annotations import workflow, Step, AutoStep

from tasks import prep


@workflow(purpose="Run the pipeline")
def run(rows):
    _ = Step(step_num=1, name='validate', purpose='check inputs')
    _ = AutoStep(step_num=2, name='prepare')
    prep(rows)


@workflow(purpose="Summarise the run")
def report():
    _ = Step(step_num=1, name='summarise', purpose='write the summary')
""",
    )
    builder.build(project_root, project_id="proj", discovery_only=False)


def _export(*args: str):
    """Run ``axiom-graph workflows export`` with *args* and return the result."""
    return CliRunner().invoke(main, ["workflows", "export", *args])


@workflow(
    purpose="workflows export writes a self-contained page for a workflow and the task it hands off to in another file",
)
def test_cli_export_writes_a_self_contained_page_with_every_file_reached(tmp_path):
    _ = Step(
        step_num=1,
        name="Index a workflow that hands off to another file",
        purpose="run's AutoStep delegates to prep, a task defined in tasks.py",
    )
    _build_handoff(tmp_path)
    out = tmp_path / "share" / "run.html"

    _ = Step(step_num=2, name="Export it from the CLI", purpose="name the workflow and an output path in a new folder")
    result = _export("run", "-o", str(out), str(tmp_path))

    _ = Step(
        step_num=3,
        name="Check the command's report",
        purpose="it names the file written and counts one workflow across two files",
    )
    assert result.exit_code == 0, result.output
    assert str(out) in result.output
    assert "1 workflow · 2 files" in result.output

    _ = Step(
        step_num=4,
        name="Check the page",
        purpose="it carries both files, says so in its header, and references nothing outside itself",
        critical="an external src or href breaks the page for a reader without network access",
    )
    html = out.read_text(encoding="utf-8")
    assert "1 workflow &middot; 2 files" in html
    assert ">pipeline.py</option>" in html and ">tasks.py</option>" in html
    assert html.count('<div class="src"') == 2
    assert "src=" not in html
    assert "href=" not in html


@workflow(purpose="A JSON export of mixed workflow and task ids is exactly the export bundle")
def test_json_export_of_mixed_workflow_and_task_ids_is_the_bundle(tmp_path):
    _build_handoff(tmp_path)
    out = tmp_path / "bundle.json"

    result = _export("run", "prep", "--format", "json", "-o", str(out), str(tmp_path))

    assert result.exit_code == 0, result.output
    expected = workflow_bundle_to_dict(workflow_export_bundle(tmp_path, ["run", "prep"]))
    assert json.loads(out.read_text(encoding="utf-8")) == expected
    assert [wf["role"] for wf in expected["workflows"]] == ["workflow", "task"]


@workflow(purpose="--file exports every workflow and task defined in that source file")
def test_file_selection_exports_every_envelope_in_the_file(tmp_path):
    _build_handoff(tmp_path)
    out = tmp_path / "file.json"

    result = _export("--file", str(tmp_path / "pipeline.py"), "--format", "json", "-o", str(out), str(tmp_path))

    assert result.exit_code == 0, result.output
    bundle = json.loads(out.read_text(encoding="utf-8"))
    assert sorted(wf["name"] for wf in bundle["workflows"]) == ["report", "run"]


@workflow(purpose="An export naming an unknown id or a file with no workflows is refused and writes nothing")
def test_unknown_id_or_empty_file_is_refused_and_nothing_is_written(tmp_path):
    _build_handoff(tmp_path)
    out = tmp_path / "refused.html"

    result = _export("run", "nope", "--file", "missing.py", "-o", str(out), str(tmp_path))

    assert result.exit_code == 1
    assert "nope" in result.output and "missing.py" in result.output
    assert not out.exists()

    nothing = _export("-o", str(out), str(tmp_path))
    assert nothing.exit_code == 1
    assert "No workflows selected" in nothing.output
    assert not out.exists()


@workflow(purpose="workflows export runs on an install without the viz extra, rendering the code uncoloured")
def test_cli_export_runs_without_the_viz_extra(tmp_path):
    _build_handoff(tmp_path)
    out = tmp_path / "plain.html"
    # A None entry in sys.modules makes any import of that package fail.
    script = textwrap.dedent(
        f"""
        import sys
        sys.modules["fastapi"] = None
        sys.modules["pygments"] = None
        from axiom_graph.cli import main
        main(["workflows", "export", "run", "-o", {str(out)!r}, {str(tmp_path)!r}])
        """
    )

    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8")

    assert proc.returncode == 0, proc.stderr
    html = out.read_text(encoding="utf-8")
    assert "def prep" in html
    assert "tok-" not in html


@workflow(purpose="The MCP export tool writes the page under the project and refuses an unknown id without writing")
def test_mcp_export_tool_writes_the_page_and_refuses_unknown_ids(tmp_path):
    _build_handoff(tmp_path)

    reply = axiom_graph_workflow_export(str(tmp_path), workflow_ids=["run"])

    page = tmp_path / "workflow-export.html"
    assert reply == f"Wrote {page}: 1 workflow · 2 files"
    assert "<h2>run</h2>" in page.read_text(encoding="utf-8")

    refused = axiom_graph_workflow_export(str(tmp_path), workflow_ids=["nope"], output_path="out/refused.html")
    assert refused.startswith("ERROR:") and "nope" in refused
    assert not (tmp_path / "out" / "refused.html").exists()

    unindexed = tmp_path / "unindexed"
    unindexed.mkdir()
    assert axiom_graph_workflow_export(str(unindexed), workflow_ids=["run"]).startswith("ERROR: No index found")
    assert not (unindexed / "workflow-export.html").exists()


def test_page_header_pluralizes_each_count():
    html = render_export_html({"workflows": [{"name": "solo", "steps": []}], "sources": {"solo.py": "x = 1\n"}})

    assert "1 workflow &middot; 1 file<" in html
    assert export_label(2, 1) == "2 workflows · 1 file"
    assert export_label(1, 3) == "1 workflow · 3 files"
