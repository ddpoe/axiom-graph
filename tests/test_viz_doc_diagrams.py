"""Subsystem tests for the viz doc viewer's mermaid source accumulator.

Tier 2 for the cross-module accumulator contract, Tier 1 for the narrower
edge case. The doc viewer renders one section at a time through
``renderMarkdown()``, which strips each mermaid fence into an empty
``<pre data-mermaid-idx>`` placeholder and stashes the fence text in the
accumulator ``runMermaid()`` reads back at paint time. These drive the
compiled frontend modules under node to pin that contract across sections.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from axiom_annotations import workflow

JS_DIR = Path(__file__).resolve().parents[1] / "axiom_graph" / "viz" / "static" / "js"

HARNESS = """\
import { renderMarkdown } from './doc-editor.js';
import { clearMermaidSources, getMermaidSources } from './doc-diagrams.js';

const DIAGRAM_A = '## Model\\n\\n```mermaid\\nflowchart TD\\n  A --> B\\n```\\n';
const DIAGRAM_B = '## Flow\\n\\n```mermaid\\nflowchart LR\\n  X --> Y\\n```\\n';
const PROSE = '## Decisions\\n\\nJust prose, no fence.\\n';

const indices = (html) =>
  [...html.matchAll(/data-mermaid-idx="(\\d+)"/g)].map((m) => Number(m[1]));

clearMermaidSources();
const diagramThenProse = renderMarkdown(DIAGRAM_A);
renderMarkdown(PROSE);
const afterProse = getMermaidSources().slice();

clearMermaidSources();
const first = renderMarkdown(DIAGRAM_A);
const second = renderMarkdown(DIAGRAM_B);
const twoDiagrams = getMermaidSources().slice();

clearMermaidSources();
renderMarkdown(DIAGRAM_A);
renderMarkdown('');
const afterEmpty = getMermaidSources().slice();

console.log(JSON.stringify({
  afterProse,
  diagramThenProseIndices: indices(diagramThenProse),
  twoDiagrams,
  twoDiagramIndices: [...indices(first), ...indices(second)],
  afterEmpty,
}));
"""


@pytest.fixture(scope="module")
def mermaid_render_results(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Run the harness against the compiled viz modules and return its JSON."""
    if shutil.which("node") is None:
        pytest.skip("node is not available")
    if not (JS_DIR / "doc-editor.js").exists():
        pytest.skip("compiled viz modules are not present")

    workdir = tmp_path_factory.mktemp("viz-js")
    shutil.copytree(JS_DIR, workdir, dirs_exist_ok=True)
    # The compiled modules are ESM; node needs a package scope that says so.
    (workdir / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    (workdir / "harness.mjs").write_text(HARNESS, encoding="utf-8")

    proc = subprocess.run(
        ["node", "harness.mjs"],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@workflow(purpose="Verify a mermaid section's source survives the rendering of later sections")
def test_sources_survive_a_later_prose_section(mermaid_render_results: dict) -> None:
    assert mermaid_render_results["afterProse"] == ["flowchart TD\n  A --> B"]
    assert mermaid_render_results["diagramThenProseIndices"] == [0]


@workflow(purpose="Verify each mermaid section gets its own placeholder index and source slot")
def test_two_diagram_sections_get_distinct_indices(
    mermaid_render_results: dict,
) -> None:
    assert mermaid_render_results["twoDiagramIndices"] == [0, 1]
    assert mermaid_render_results["twoDiagrams"] == [
        "flowchart TD\n  A --> B",
        "flowchart LR\n  X --> Y",
    ]


def test_empty_section_keeps_collected_sources(mermaid_render_results: dict) -> None:
    """An empty section short-circuits without touching the accumulator."""
    assert mermaid_render_results["afterEmpty"] == ["flowchart TD\n  A --> B"]
