"""The build summary must say what it actually counted, on both surfaces.

``axiom-graph build`` and the ``axiom_graph_build`` MCP tool print the same
report in two different formats.  Its file counts cover Python files only, so
an unlabelled ``0`` reads as "no documentation was scanned" when it means
nothing of the sort; and the count of documentation files skipped by the mtime
fast pass, which is the number that answers that question, was carried on the
summary and printed nowhere.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom_annotations import workflow
from click.testing import CliRunner

from axiom_graph.cli import main
from axiom_graph.index import builder

PROJ_TOML = '[axiom_graph]\nproject_id = "proj"\n'


def _project_with_docs(root: Path) -> None:
    """Create and index a project with one module and two DocJSON documents."""
    (root / "axiom-graph.toml").write_text(PROJ_TOML, encoding="utf-8")
    (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    docs_dir = root / "docs"
    docs_dir.mkdir()
    for slug in ("one", "two"):
        (docs_dir / f"{slug}.json").write_text(
            json.dumps({"title": slug.title(), "sections": [{"id": "body", "heading": "Body", "content": "Body."}]}),
            encoding="utf-8",
        )
    # First build stamps the file mtimes the fast pass compares against.
    builder.build(root)


@workflow(
    purpose="Both the CLI and the MCP build summary report how many documentation files the mtime fast pass skipped, "
    "and label their file counts as Python-only",
)
def test_build_summary_reports_skipped_docs_on_both_surfaces(tmp_path: Path) -> None:
    from axiom_graph.lifecycle.mcp_tools import axiom_graph_build

    _project_with_docs(tmp_path)

    mcp_output = axiom_graph_build(str(tmp_path))
    assert "docs skipped    : 2 (markdown + DocJSON, mtime unchanged)" in mcp_output
    assert "files scanned   : " in mcp_output
    assert "(Python)" in mcp_output
    assert "(Python, mtime unchanged)" in mcp_output

    result = CliRunner().invoke(main, ["build", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "docs skipped  : 2 (markdown + DocJSON, mtime unchanged)" in result.output
    assert "files scanned : " in result.output
    assert "(Python)" in result.output
    assert "(Python, mtime unchanged)" in result.output
    assert "Done." in result.output


def test_build_summary_reads_docs_skipped_from_a_raw_dict(capsys) -> None:
    """The CLI formatter also serves callers that hand it the raw summary dict."""
    from axiom_graph.cli._core import _echo_build_summary

    _echo_build_summary(
        {
            "warnings": [],
            "files_scanned": 3,
            "files_skipped_mtime": 1,
            "docs_skipped_mtime": 7,
            "nodes_written": 0,
            "nodes_skipped": 0,
            "edges_written": 0,
            "edges_skipped": 0,
        }
    )
    out = capsys.readouterr().out
    assert "docs skipped  : 7 (markdown + DocJSON, mtime unchanged)" in out
    assert "files scanned : 3 (Python)" in out
    assert "files skipped : 1 (Python, mtime unchanged)" in out
