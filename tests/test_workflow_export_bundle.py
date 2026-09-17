"""Tests for the workflow export bundle and the standalone HTML it renders.

The bundle is the contract: a JSON document holding the selected
workflows, their expanded steps, and every source file those steps and
their delegate targets point at.  The HTML export is that same bundle
rendered, so a reader without the repository can open one file and click
through the outline into the code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from axiom_annotations import Step, workflow
from fastapi.testclient import TestClient

from axiom_graph.index import builder
from axiom_graph.viz import server
from axiom_graph.workflows.api import workflow_bundle_to_dict, workflow_export_bundle
from axiom_graph.workflows.mcp_tools import axiom_graph_workflow_detail

PLOT_DATA = "proj::pipeline::build_plot_data@workflow"
REPORT = "proj::pipeline::render_report@workflow"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _build_two_workflows(project_root: Path) -> None:
    """Write two workflows that share a file and reach three others.

    ``pipeline.py`` holds both workflows.  ``build_plot_data`` delegates
    into ``tasks.py``, which delegates on into ``probes.py``;
    ``render_report`` delegates into ``reporting.py``.
    """
    _write(
        project_root / "probes.py",
        """\
from axiom_annotations import task, Step


@task(purpose="Locate the upstream cache", inputs="a project root", outputs="a cache path")
def locate_cache():
    _ = Step(step_num=1, name='scan the cache dir', purpose='find the newest cache')
""",
    )
    _write(
        project_root / "tasks.py",
        """\
from axiom_annotations import task, Step, AutoStep

from probes import locate_cache


@task(purpose="Assemble the protein table", inputs="a cache path", outputs="a tidy frame")
def build_proteins():
    _ = AutoStep(step_num=1, name='find the cache')
    locate_cache()
    _ = Step(step_num=2, name='derive grid', purpose='derive the common dose grid')
""",
    )
    _write(
        project_root / "reporting.py",
        """\
from axiom_annotations import task, Step


@task(purpose="Render the report", inputs="a tidy frame", outputs="an html file")
def render():
    _ = Step(step_num=1, name='fill the template', purpose='render the report body')
""",
    )
    _write(
        project_root / "pipeline.py",
        """\
from axiom_annotations import workflow, Step, AutoStep

from reporting import render
from tasks import build_proteins


@workflow(purpose="Build plot data")
def build_plot_data():
    _ = Step(step_num=1, name='load inputs', purpose='read the source frame')
    _ = AutoStep(step_num=2, name='bootstrap over the protein set')
    build_proteins()


@workflow(purpose="Render the report")
def render_report():
    _ = AutoStep(step_num=1, name='render')
    render()
""",
    )
    builder.build(project_root, project_id="proj", discovery_only=False)


def _setup_server(project_root: Path) -> TestClient:
    """Point the viz server module state at a project and return a test client."""
    server._PROJECT_ROOT = project_root
    server._DB_PATH = project_root / ".axiom_graph" / "graph.db"
    server._DFLOW_DB_PATH = None
    server._TEST_PATHS = []
    server._EXCLUDE_DIRS = []
    return TestClient(server.app)


@workflow(
    purpose="Exporting several workflows carries every file they touch, each exactly once",
)
def test_export_sources_are_the_deduplicated_union_of_every_file_reached(tmp_path):
    _ = Step(
        step_num=1,
        name="index two workflows sharing a file",
        purpose="both workflows live in pipeline.py and delegate into three other modules",
    )
    _build_two_workflows(tmp_path)

    _ = Step(step_num=2, name="export both workflows", purpose="build the bundle for the whole selection")
    bundle = workflow_bundle_to_dict(workflow_export_bundle(tmp_path, [PLOT_DATA, REPORT]))

    _ = Step(
        step_num=3,
        name="check the source map",
        purpose="the map is exactly the union of workflow, step-marker and target files",
        critical="pipeline.py backs both workflows and must still appear once",
    )
    assert set(bundle["sources"]) == {"pipeline.py", "probes.py", "reporting.py", "tasks.py"}
    assert "def build_plot_data" in bundle["sources"]["pipeline.py"]
    assert "def render_report" in bundle["sources"]["pipeline.py"]


@workflow(
    purpose="Every step and target in an export resolves to source the export carries",
)
def test_export_has_no_dead_click_throughs(tmp_path):
    _ = Step(step_num=1, name="index the workflows", purpose="build the fixture index")
    _build_two_workflows(tmp_path)

    _ = Step(
        step_num=2,
        name="export one workflow only",
        purpose="its delegate targets live in modules the caller never named",
    )
    bundle = workflow_bundle_to_dict(workflow_export_bundle(tmp_path, [PLOT_DATA]))

    _ = Step(
        step_num=3,
        name="follow every click-through",
        purpose="each step location and each target location lands on carried source",
    )
    assert {"tasks.py", "probes.py"} <= set(bundle["sources"])
    steps = bundle["workflows"][0]["steps"]
    assert steps, "the exported workflow should carry expanded steps"
    for step in steps:
        assert step["location"] in bundle["sources"]
        if step["target"] and step["target"]["location"]:
            assert step["target"]["location"] in bundle["sources"]


@workflow(purpose="The rendered HTML export references nothing outside itself")
def test_html_export_is_self_contained(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    resp = client.get("/api/workflow-export", params={"ids": f"{PLOT_DATA},{REPORT}", "format": "html"})

    assert resp.status_code == 200
    html = resp.text
    assert "src=" not in html
    assert "href=" not in html
    assert '<script type="application/json"' in html
    assert "build_plot_data" in html


@workflow(purpose="A workflow reads identically through the export bundle and the detail tool's JSON")
def test_bundle_entry_and_detail_json_describe_a_workflow_identically(tmp_path):
    _build_two_workflows(tmp_path)

    bundle = workflow_bundle_to_dict(workflow_export_bundle(tmp_path, [PLOT_DATA]))
    via_detail = json.loads(axiom_graph_workflow_detail(str(tmp_path), PLOT_DATA, format="json"))

    assert bundle["workflows"][0] == via_detail


SCAN_ALL = "proj::scanner::scan_all@workflow"


def _build_local_minor_steps(project_root: Path) -> None:
    """Write a workflow that declares a minor step inside its own file.

    ``scan_all`` loops over its inputs and marks each pass with an
    ``AutoStep`` numbered ``1.1``.  The function it delegates to carries
    no annotation, so nothing expands into the outline and the step's
    only source of nesting is its own dotted number.  This is the shape
    where nesting-by-expansion and nesting-by-number disagree.
    """
    _write(
        project_root / "scanner.py",
        """\
from axiom_annotations import workflow, Step, AutoStep


def read_one(path):
    return path


@workflow(purpose="Scan every input")
def scan_all(paths):
    _ = Step(step_num=1, name='collect inputs', purpose='gather the paths to scan')
    for path in paths:
        _ = AutoStep(step_num=1.1, name='read one input')
        read_one(path)
    _ = Step(step_num=2, name='summarise', purpose='report what was read')
""",
    )
    builder.build(project_root, project_id="proj", discovery_only=False)


def _step_by_num(steps: list[dict], step_num: str) -> dict:
    """Return the one exported step carrying ``step_num``."""
    matches = [s for s in steps if s["step_num"] == step_num]
    assert len(matches) == 1, f"expected exactly one step {step_num}, got {len(matches)}"
    return matches[0]


def _export_html(client: TestClient, ids: str) -> str:
    """Return the rendered standalone export for a comma-separated selection."""
    resp = client.get("/api/workflow-export", params={"ids": ids, "format": "html"})
    assert resp.status_code == 200
    return resp.text


@workflow(
    purpose="A step's outline depth comes from its dotted number, not from how it was reached",
)
def test_step_depth_follows_the_dotted_step_number(tmp_path):
    _ = Step(
        step_num=1,
        name="index a workflow with an in-file minor step",
        purpose="its 1.1 delegates to an unannotated function, so nothing expands",
    )
    _build_local_minor_steps(tmp_path)

    _ = Step(step_num=2, name="export it", purpose="build the bundle for the single workflow")
    bundle = workflow_bundle_to_dict(workflow_export_bundle(tmp_path, [SCAN_ALL]))
    steps = bundle["workflows"][0]["steps"]

    _ = Step(
        step_num=3,
        name="check the minor step nests",
        purpose="1.1 sits one level under 1 even though both markers live in the same file",
        critical="reading depth off the expansion chain leaves this step flush with its parent",
    )
    assert _step_by_num(steps, "1")["depth"] == 0
    assert _step_by_num(steps, "1.1")["depth"] == 1
    assert _step_by_num(steps, "2")["depth"] == 0

    _ = Step(
        step_num=4,
        name="check the rule holds everywhere",
        purpose="every step agrees with its own number",
    )
    for step in steps:
        assert step["depth"] == step["step_num"].count(".")


@workflow(purpose="Depth still tracks the number when steps arrive through delegates")
def test_expanded_step_depth_also_follows_the_dotted_number(tmp_path):
    _build_two_workflows(tmp_path)

    bundle = workflow_bundle_to_dict(workflow_export_bundle(tmp_path, [PLOT_DATA]))
    steps = bundle["workflows"][0]["steps"]

    assert any(s["step_num"].count(".") == 2 for s in steps), "fixture should reach two levels deep"
    for step in steps:
        assert step["depth"] == step["step_num"].count(".")


@workflow(purpose="The exported outline numbers each step exactly once")
def test_export_outline_does_not_double_number_steps(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    _ = Step(
        step_num=1,
        name="render the outline",
        purpose="an ordered list paints its own sequence beside the printed step numbers",
        critical="the outline prints step numbers itself, so its list must carry no marker",
    )
    html = _export_html(client, PLOT_DATA)

    assert "<ol" not in html
    assert 'class="step-num"' in html


@workflow(purpose="The outline indents a minor step declared in the workflow's own file")
def test_export_indents_an_in_file_minor_step(tmp_path):
    _build_local_minor_steps(tmp_path)
    client = _setup_server(tmp_path)

    _ = Step(
        step_num=1,
        name="compare the rendered nesting",
        purpose="the 1.1 row carries a deeper indent than the 1 and 2 rows around it",
    )
    html = _export_html(client, SCAN_ALL)

    rendered = re.findall(r'data-step-num="([\d.]+)"[^>]*style="--depth:(\d+)"', html)
    assert dict(rendered) == {"1": "0", "1.1": "1", "2": "0"}


@workflow(purpose="The export carries each source file's text once, not twice")
def test_html_export_does_not_duplicate_source_text(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    html = _export_html(client, PLOT_DATA)

    _ = Step(
        step_num=1,
        name="read the embedded data",
        purpose="the structure stays readable to a tool; the source copy goes as a duplicate",
        critical="the rendered code blocks already carry every line, so a second copy is dead weight",
    )
    island = re.search(r'<script type="application/json" id="workflow-bundle">(.*?)</script>', html, re.S)
    assert island, "the export should still embed its structure"
    embedded = json.loads(island.group(1).replace("<\\/", "</"))
    assert "workflows" in embedded
    assert "sources" not in embedded

    _ = Step(
        step_num=2,
        name="check the code survived",
        purpose="the reader still sees every carried file",
    )
    assert "build_plot_data" in html


@workflow(purpose="The export reads in a dark or a light browser without editing it")
def test_html_export_ships_both_themes(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    html = _export_html(client, PLOT_DATA)

    assert "prefers-color-scheme: dark" in html
    assert '[data-theme="dark"]' in html
    assert '[data-theme="light"]' in html
    assert 'id="theme-toggle"' in html


@workflow(purpose="Exported source is syntax-highlighted without reaching for the network")
def test_html_export_highlights_source(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    _ = Step(
        step_num=1,
        name="look for token markup",
        purpose="keywords carry a token class the stylesheet colours in either theme",
    )
    html = _export_html(client, PLOT_DATA)

    assert 'class="tok-k"' in html
    assert re.search(r"\.tok-k\s*\{", html), "both themes should colour the token classes"


@workflow(purpose="A multi-workflow export opens on a list of what it contains")
def test_html_export_lists_contents_for_several_workflows(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    _ = Step(
        step_num=1,
        name="export two workflows",
        purpose="the reader needs a way to jump between them",
    )
    many = _export_html(client, f"{PLOT_DATA},{REPORT}")

    toc = re.search(r'<nav class="toc".*?</nav>', many, re.S)
    assert toc, "a multi-workflow export should open on a table of contents"
    assert "build_plot_data" in toc.group(0)
    assert "render_report" in toc.group(0)

    _ = Step(
        step_num=2,
        name="export one workflow",
        purpose="a single-workflow export has nothing to jump between",
    )
    one = _export_html(client, PLOT_DATA)
    assert '<nav class="toc"' not in one


@workflow(purpose="The export picker's modal is reachable from the view whose button opens it")
def test_export_modal_is_not_nested_in_the_view_scoped_sidebar():
    _ = Step(
        step_num=1,
        name="locate the modal in the page",
        purpose="the workflows view hides the graph sidebar wholesale",
        critical="a modal inside a display:none ancestor cannot be shown by unhiding the modal",
    )
    page = (Path(__file__).resolve().parents[1] / "axiom_graph" / "viz" / "static" / "index.html").read_text(
        encoding="utf-8"
    )

    sidebar = page.index('<aside id="sidebar"')
    sidebar_end = page.index("</aside>", sidebar)
    assert page.index('id="wf-export-overlay"') > sidebar_end


UNBOUND = "proj::logged::run_all@workflow"


def _build_unbound_autostep(project_root: Path) -> None:
    """Write a workflow whose AutoStep binds to nothing.

    The scanner binds an ``AutoStep`` to the call immediately following it.
    Here a log line sits between the marker and the real call, so no
    delegate edge is emitted at all — the step carries an ``auto`` badge
    and no target, which reads as a broken link unless the outline says
    what happened.
    """
    _write(
        project_root / "logged.py",
        """\
import logging

from axiom_annotations import workflow, Step, AutoStep

logger = logging.getLogger(__name__)


def do_work(item):
    return item


@workflow(purpose="Process every item")
def run_all(items):
    _ = Step(step_num=1, name='gather items', purpose='collect the work list')
    for item in items:
        _ = AutoStep(step_num=1.1, name='process one item')
        logger.debug('processing %s', item)
        do_work(item)
""",
    )
    builder.build(project_root, project_id="proj", discovery_only=False)


@workflow(purpose="An AutoStep that resolved no target says so instead of showing nothing")
def test_export_marks_an_autostep_that_bound_no_target(tmp_path):
    _ = Step(
        step_num=1,
        name="index a workflow whose AutoStep cannot bind",
        purpose="a log line sits between the marker and the call it means",
    )
    _build_unbound_autostep(tmp_path)
    client = _setup_server(tmp_path)

    bundle = workflow_bundle_to_dict(workflow_export_bundle(tmp_path, [UNBOUND]))
    step = _step_by_num(bundle["workflows"][0]["steps"], "1.1")

    _ = Step(
        step_num=2,
        name="confirm the gap is real, then that it is shown",
        purpose="the bundle carries no target, and the outline explains the absence",
        critical="a bare auto badge with no arrow reads as a rendering fault",
    )
    assert step["is_auto"] is True
    assert step["target"] is None

    html = _export_html(client, UNBOUND)
    assert 'class="unbound"' in html
    assert "No delegate target resolved" in html
    assert 'data-tip="No delegate target resolved' in html


@workflow(purpose="Only the steps that something nests under offer a collapse control")
def test_export_offers_collapse_controls_where_steps_nest(tmp_path):
    _build_local_minor_steps(tmp_path)
    client = _setup_server(tmp_path)

    html = _export_html(client, SCAN_ALL)

    _ = Step(
        step_num=1,
        name="match each row against its control",
        purpose="step 1 has 1.1 under it; 1.1 and 2 have nothing under them",
    )
    controls = {
        num: 'class="step-disclose"' in row
        for num, row in re.findall(r'data-step-num="([\d.]+)"(.*?)</li>', html, re.S)
    }
    assert controls == {"1": True, "1.1": False, "2": False}


@workflow(purpose="The contents list stays put instead of scrolling over the outline")
def test_export_contents_list_sits_outside_the_scrolling_outline(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    html = _export_html(client, f"{PLOT_DATA},{REPORT}")

    _ = Step(
        step_num=1,
        name="check the nesting and the control",
        purpose="a list inside the scrolling pane would ride over the section it points at",
    )
    assert html.index('<nav class="toc"') < html.index('<div class="pane outline">')
    assert 'class="toc-disclose"' in html


@workflow(purpose="Step detail folds independently of the step tree")
def test_export_folds_detail_separately_from_structure(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    html = _export_html(client, PLOT_DATA)

    _ = Step(
        step_num=1,
        name="check both controls exist",
        purpose="the arrow folds child steps; the ellipsis folds a step's own extra intent",
        critical="one control driving both axes leaves either one unreachable",
    )
    assert 'id="fold-all"' in html
    assert 'id="unfold-all"' in html
    assert 'id="detail-seg"' in html
    assert 'data-detail="compact"' in html
    assert 'data-detail="all"' in html

    _ = Step(
        step_num=2,
        name="check the default density",
        purpose="the document opens on names and purposes, not on everything",
    )
    assert 'data-detail="purpose"' in html[: html.index("<head")]


@workflow(purpose="Only a step with more than a purpose offers to expand")
def test_export_offers_detail_control_only_where_there_is_detail(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    html = _export_html(client, PLOT_DATA)

    _ = Step(
        step_num=1,
        name="pair each control with the block it opens",
        purpose="a step showing the ellipsis has a detail block, and the reverse holds too",
    )
    rows = dict(re.findall(r'data-step-num="([\d.]+)"(.*?)</li>', html, re.S))
    assert rows, "the export should carry steps"
    for num, row in rows.items():
        assert ('class="step-more"' in row) == ('class="step-detail"' in row), (
            f"step {num} should pair its control with its detail block"
        )
    assert any('class="step-more"' in row for row in rows.values())


SPANNED = "proj::spanned::run@workflow"


def _build_multiline_blocks(project_root: Path) -> None:
    """Write a file whose decorator and step marker each span several lines.

    ``helper``'s decorator opens on line 4 and closes on line 8, with its
    signature on line 9.  ``run``'s first Step marker opens on line 14 and
    closes on line 18.  Both are the shapes a single-line highlight reads
    badly on.
    """
    _write(
        project_root / "spanned.py",
        """from axiom_annotations import workflow, task, Step, AutoStep


@task(
    purpose="Do the thing",
    inputs="an input",
    outputs="an output",
)
def helper():
    return None


@workflow(purpose="Run it")
def run():
    _ = Step(
        step_num=1,
        name='set up',
        purpose='prepare the run',
    )
    _ = AutoStep(step_num=2, name='call the helper')
    helper()
""",
    )
    builder.build(project_root, project_id="proj", discovery_only=False)


@workflow(purpose="Following a link lights the whole block it names, not one line of it")
def test_export_spans_decorators_and_markers(tmp_path):
    _ = Step(
        step_num=1,
        name="index a file with multi-line blocks",
        purpose="a five-line decorator and a five-line Step marker",
    )
    _build_multiline_blocks(tmp_path)
    client = _setup_server(tmp_path)

    html = _export_html(client, SPANNED)
    spans = json.loads(
        re.search(r'<script type="application/json" id="line-spans">(.*?)</script>', html, re.S)
        .group(1)
        .replace(r"<\/", "</")
    )["spanned.py"]

    _ = Step(
        step_num=2,
        name="check the decorator span stops at the signature",
        purpose="the intent is the decorator; the def line below it is not part of it",
        critical="using the function's stored end would paint the entire body instead",
    )
    assert spans["4"] == 8

    _ = Step(
        step_num=3,
        name="check the marker span covers the whole call",
        purpose="a Step written across five lines lights as one block",
    )
    assert spans["15"] == 19


@workflow(purpose="A file the export cannot parse still renders, with single-line links")
def test_export_spans_tolerate_unparsable_sources(tmp_path):
    _ = Step(
        step_num=1,
        name="span a file that is not Python",
        purpose="a span map is a nicety; failing to build one must not fail the export",
    )
    from axiom_graph.viz.workflows import _block_spans

    assert _block_spans("notes.md", "# not python") == {}
    assert _block_spans("broken.py", "def (:") == {}


@workflow(purpose="Long lines wrap by default and the reader can turn it off")
def test_export_wraps_long_lines_by_default(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    html = _export_html(client, PLOT_DATA)

    _ = Step(
        step_num=1,
        name="check the default and the control",
        purpose="the longest lines in an export are the purpose and critical strings",
        critical="scrolling sideways to finish a sentence is the worst case for a reading artifact",
    )
    assert 'data-wrap="on"' in html
    assert 'id="wrap-toggle"' in html
    assert ':root[data-wrap="on"] .src pre{white-space:pre-wrap' in html


@workflow(purpose="Each workflow is visually separated from the one before it")
def test_export_separates_consecutive_workflows(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    html = _export_html(client, f"{PLOT_DATA},{REPORT}")

    _ = Step(
        step_num=1,
        name="check the rule and the indent rail",
        purpose="a rule divides consecutive workflows; the body indents under its heading",
    )
    assert ".wf + .wf{" in html
    assert '<div class="wf-body">' in html
    assert html.count('<div class="wf-body">') == 2

    _ = Step(
        step_num=2,
        name="check a collapsed workflow keeps its location",
        purpose="folding a workflow should not hide where it lives",
    )
    assert html.index('class="wf-file"') < html.index('<div class="wf-body">')


@workflow(purpose="The export can be searched across every file it carries")
def test_export_searches_all_carried_files(tmp_path):
    _build_two_workflows(tmp_path)
    client = _setup_server(tmp_path)

    html = _export_html(client, PLOT_DATA)

    _ = Step(
        step_num=1,
        name="check the search controls ship",
        purpose="a box, a running count, and next/previous navigation",
        critical="the browser's own find reaches only the visible file, since the rest are hidden",
    )
    assert 'id="code-search"' in html
    assert 'id="code-search-count"' in html
    assert 'id="code-prev"' in html
    assert 'id="code-next"' in html

    _ = Step(
        step_num=2,
        name="check every carried file is reachable by it",
        purpose="each file's lines are in the document, hidden or not, so all are searchable",
    )
    blocks = re.findall(r'<div class="src" id="(src-\d+)"', html)
    assert len(blocks) == len(
        json.loads(re.search(r'id="path-slugs">(.*?)</script>', html, re.S).group(1).replace("<" + chr(92) + "/", "</"))
    )

    _ = Step(
        step_num=3,
        name="check search and jump highlights stay distinct",
        purpose="a reader following a link while a search is live must tell the two apart",
    )
    assert ".cl.match{background:var(--match)}" in html
    assert "--match:" in html
