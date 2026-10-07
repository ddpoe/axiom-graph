"""Node diffs locate the node by identity in each side, never at its indexed line range.

Every scenario builds a real git repository and indexes it with the real
indexer; diffs run against real ``git show`` output.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from axiom_annotations import workflow
from click.testing import CliRunner

from axiom_graph.cli import main
from axiom_graph.index import builder, db
from axiom_graph.index.staleness import record_staleness
from axiom_graph.lifecycle.api import _baseline_candidate_ids, _parse_level3, _prior_node_ids, get_node_diff
from axiom_graph.lifecycle.mcp_tools import axiom_graph_diff
from axiom_graph.scanners import js_scanner
from axiom_graph.scanners.js_scanner import HAS_TREE_SITTER
from axiom_graph.scanners.node_hashing import scan_blob_at_location

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _commit(project: Path, message: str) -> str:
    """Stage everything, commit, and return the new HEAD SHA."""
    subprocess.run(["git", "add", "-A"], cwd=project, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=project, capture_output=True, check=True)
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _build(project: Path, full: bool = False) -> Path:
    """Index *project* with the real indexer and record staleness; return the DB path."""
    builder.build(project, project_id="proj", discovery_only=not full)
    db_path = project / ".axiom_graph" / "graph.db"
    record_staleness(db_path, project, db.all_nodes(db_path))
    return db_path


def _write(path: Path, text: str) -> None:
    """Write *text* to *path*, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_FORMAT_BAR = (
    "def format_bar(label, count, total):\n"
    '    """Format a bar."""\n'
    "    if total == 0:\n"
    '        return f"- {label}: 0"\n'
    "    pct = count / total * 100\n"
    '    return f"- {label}: {count} ({pct:.0f}%)"\n'
)


def _unparseable_baseline(project: Path) -> tuple[str, str]:
    """Commit a module that does not parse, then fix it and index; return (baseline sha, node id)."""
    _write(project / "broken.py", "def stable():\n    return 1\n\n\ndef bad(:\n    pass\n")
    baseline = _commit(project, "broken module")
    _write(project / "broken.py", "def stable():\n    return 1\n\n\ndef bad():\n    pass\n")
    _commit(project, "fix module")
    _build(project, full=True)
    return baseline, "proj::broken::stable"


_ADD_TS = "export function add(a: number, b: number): number {\n  return a + b;\n}\n"


def _typescript_project(project: Path, baseline_text: str, current_text: str) -> str:
    """Commit ``web/util.ts`` as *baseline_text*, then *current_text*, index; return the baseline sha."""
    _write(
        project / "axiom-graph.toml",
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\njs_paths = ["web/**/*.ts"]\n',
    )
    _write(project / "web" / "util.ts", baseline_text)
    baseline = _commit(project, "add util")
    _write(project / "web" / "util.ts", current_text)
    _commit(project, "change util")
    _build(project, full=True)
    return baseline


def _shifted_module(project: Path) -> tuple[str, str]:
    """Commit a function, add code above it, index; return (baseline sha, node id)."""
    _write(project / "shift.py", _FORMAT_BAR)
    baseline = _commit(project, "add format_bar")
    above = "".join(f"def pad_{i}():\n    return {i}\n\n\n" for i in range(8))
    _write(project / "shift.py", above + _FORMAT_BAR)
    _commit(project, "add code above")
    _build(project, full=True)
    return baseline, "proj::shift::format_bar"


# ---------------------------------------------------------------------------
# Tier 1 -- scan helper and rename chain
# ---------------------------------------------------------------------------


def test_scan_blob_reports_parse_failure_not_absence(tmp_path: Path):
    """A Python blob that does not parse is an error, not a scan with no functions."""
    scan = scan_blob_at_location("def f(:\n", tmp_path, "pkg/m.py", "proj")
    assert scan.error is not None
    assert scan.nodes == {}


def test_scan_blob_ids_follow_the_given_location(tmp_path: Path):
    """Content is scanned as the file at the given path, so ids match that path."""
    scan = scan_blob_at_location("def f():\n    return 1\n", tmp_path, "pkg/m.py", "proj")
    assert scan.error is None
    assert scan.nodes["proj::pkg.m::f"].level_3_location == "pkg/m.py#L1-L2"


def test_scan_blob_unsupported_kind_is_an_error(tmp_path: Path):
    """A file kind with no identity scan is reported as an error."""
    scan = scan_blob_at_location("key: value\n", tmp_path, "conf.yaml", "proj")
    assert scan.error is not None


def test_scan_blob_invalid_docjson_is_an_error(tmp_path: Path):
    """A DocJSON blob that is not valid JSON is an error."""
    scan = scan_blob_at_location("{not json", tmp_path, "docs/g.docjson", "proj")
    assert scan.error is not None


def test_scan_blob_markdown_yields_doc_and_section_ids(tmp_path: Path):
    """A Markdown blob scans to its document node and one node per H2 section."""
    scan = scan_blob_at_location(
        "# Notes\n\n## Alpha\n\nFirst.\n\n## Beta\n\nSecond.\n", tmp_path, "docs/notes.md", "proj"
    )
    assert scan.error is None
    assert set(scan.nodes) == {"proj::docs/notes.md", "proj::docs/notes.md#alpha", "proj::docs/notes.md#beta"}
    assert scan.nodes["proj::docs/notes.md#alpha"].level_2 == "First."


def test_parse_level3_splits_a_section_slug_off_the_path():
    """A slug fragment is not part of the file path and carries no line range."""
    assert _parse_level3("docs/notes.md#alpha") == ("docs/notes.md", None, None)
    assert _parse_level3("pkg/m.py#L3-L9") == ("pkg/m.py", 3, 9)
    assert _parse_level3("pkg/m.py#L4") == ("pkg/m.py", 4, 4)
    assert _parse_level3("pkg/m.py") == ("pkg/m.py", None, None)


def test_baseline_candidates_rebase_prior_ids_onto_current_file():
    """Prior ids keep their within-file part, including step suffixes, on the current file prefix."""
    candidates = _baseline_candidate_ids(
        "proj::new.mod::renamed::step-2",
        ["proj::old.mod::original::step-2", "proj::old.mod"],
    )
    assert candidates == ["proj::new.mod::renamed::step-2", "proj::new.mod::original::step-2"]


def test_prior_node_ids_walk_the_rename_ledger_transitively(git_project: Path):
    """A node renamed twice lists both earlier ids, nearest first."""
    _write(git_project / "m.py", "def a(x):\n    y = x * 2\n    return y + 1\n")
    _commit(git_project, "a")
    db_path = _build(git_project, full=True)
    db.record_code_rename(db_path, "proj::m::c", "proj::m::b", "m.py")
    db.record_code_rename(db_path, "proj::m::b", "proj::m::a", "m.py")
    assert _prior_node_ids(db_path, "proj::m::a") == ["proj::m::b", "proj::m::c"]


# ---------------------------------------------------------------------------
# Tier 2 -- subsystem scenarios
# ---------------------------------------------------------------------------


@workflow(purpose="An unchanged function in a moved file, below removed lines, diffs as unchanged")
def test_moved_file_with_shift_reads_unchanged(git_project: Path):
    above = "".join(f"def gone_{i}():\n    return {i}\n\n\n" for i in range(6))
    _write(git_project / "scripts" / "report.py", above + _FORMAT_BAR)
    baseline = _commit(git_project, "add report")
    (git_project / "pkg" / "scripts").mkdir(parents=True)
    subprocess.run(
        ["git", "mv", "scripts/report.py", "pkg/scripts/report.py"], cwd=git_project, capture_output=True, check=True
    )
    _write(git_project / "pkg" / "scripts" / "report.py", "def kept():\n    return 0\n\n\n" + _FORMAT_BAR)
    _commit(git_project, "move report and drop lines above")
    db_path = _build(git_project, full=True)

    result = get_node_diff(db_path, git_project, "proj::pkg.scripts.report::format_bar", baseline_sha=baseline)

    assert "error" not in result
    assert result["baseline_path"] == "scripts/report.py"
    assert result["old_content"] == result["new_content"] == _FORMAT_BAR.rstrip("\n")


@pytest.mark.skipif(not HAS_TREE_SITTER, reason="JS/TS scanning requires the js extra")
@workflow(purpose="An unchanged TypeScript function below inserted code diffs as unchanged")
def test_typescript_function_below_shift_reads_unchanged(git_project: Path):
    _write(
        git_project / "axiom-graph.toml",
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\njs_paths = ["web/**/*.ts"]\n',
    )
    add = "export function add(a: number, b: number): number {\n  return a + b;\n}\n"
    _write(git_project / "web" / "util.ts", add)
    baseline = _commit(git_project, "add util")
    above = "".join(f"export function pad{i}(): number {{\n  return {i};\n}}\n\n" for i in range(5))
    _write(git_project / "web" / "util.ts", above + add)
    _commit(git_project, "add code above")
    db_path = _build(git_project, full=True)

    result = get_node_diff(db_path, git_project, "proj::web.util::add", baseline_sha=baseline)

    assert "error" not in result
    assert result["old_content"] == result["new_content"] == add.rstrip("\n")


@workflow(
    purpose="A function renamed since the baseline diffs against its old-named body; "
    "a function added since then has an empty old side"
)
def test_renamed_and_new_functions(git_project: Path):
    body = "    y = x * 2\n    z = y + 1\n    return z\n"
    _write(git_project / "m.py", "def old_name(x):\n" + body)
    baseline = _commit(git_project, "add old_name")
    _build(git_project, full=True)
    _write(git_project / "m.py", "def new_name(x):\n" + body + "\n\ndef added():\n    return 1\n")
    _commit(git_project, "rename and add")
    db_path = _build(git_project)

    renamed = get_node_diff(db_path, git_project, "proj::m::new_name", baseline_sha=baseline)
    assert "error" not in renamed
    assert renamed["old_content"] == ("def old_name(x):\n" + body).rstrip("\n")
    assert renamed["new_content"] == ("def new_name(x):\n" + body).rstrip("\n")

    added = get_node_diff(db_path, git_project, "proj::m::added", baseline_sha=baseline)
    assert "error" not in added
    assert added["old_content"] == ""
    assert added["new_content"] == "def added():\n    return 1"


@workflow(purpose="A DocJSON section diffs as its own heading and content, unaffected by edits elsewhere in the doc")
def test_docjson_section_diffs_as_itself(git_project: Path):
    def doc(sections: list[dict]) -> str:
        return json.dumps({"title": "Guide", "sections": sections}, indent=2)

    intro = {"id": "intro", "heading": "Intro", "content": "Hello."}
    usage_old = {"id": "usage", "heading": "Usage", "content": "Run it.", "sections": [{"id": "cli", "heading": "CLI"}]}
    usage_new = dict(usage_old, content="Run it twice.")
    faq = {"id": "faq", "heading": "FAQ", "content": "None yet."}
    _write(git_project / "docs" / "guide.docjson", doc([intro, usage_old, faq]))
    baseline = _commit(git_project, "add guide")
    added = {"id": "news", "heading": "News", "content": "A new section above."}
    _write(git_project / "docs" / "guide.docjson", doc([added, intro, usage_new, faq]))
    _commit(git_project, "add a section, edit usage")
    db_path = _build(git_project, full=True)

    untouched = get_node_diff(db_path, git_project, "proj::docs/guide::faq", baseline_sha=baseline)
    assert "error" not in untouched
    assert untouched["old_content"] == untouched["new_content"] == "FAQ\n\nNone yet."

    edited = get_node_diff(db_path, git_project, "proj::docs/guide::usage", baseline_sha=baseline)
    assert edited["old_content"] == "Usage\n\nRun it."
    assert edited["new_content"] == "Usage\n\nRun it twice."


@workflow(purpose="A Markdown section diffs as its own heading and body; the document node diffs as the whole file")
def test_markdown_section_diffs_as_itself(git_project: Path):
    notes = git_project / "docs" / "notes.md"
    old_text = "# Notes\n\n## Alpha\n\nFirst draft.\n\n```\nrun()\n```\n\n## Beta\n\nUnchanged.\n"
    _write(notes, old_text)
    baseline = _commit(git_project, "add notes")
    new_text = (
        "# Notes\n\n## Gamma\n\nAdded above.\n\n## Alpha\n\nSecond draft.\n\n```\nrun()\n```\n\n## Beta\n\nUnchanged.\n"
    )
    _write(notes, new_text)
    _commit(git_project, "add a section, edit alpha")
    db_path = _build(git_project, full=True)

    edited = get_node_diff(db_path, git_project, "proj::docs/notes.md#alpha", baseline_sha=baseline)
    assert "error" not in edited
    assert edited["old_content"] == "Alpha\n\nFirst draft.\n\n```\nrun()\n```"
    assert edited["new_content"] == "Alpha\n\nSecond draft.\n\n```\nrun()\n```"
    assert edited["path"] == "docs/notes.md"

    untouched = get_node_diff(db_path, git_project, "proj::docs/notes.md#beta", baseline_sha=baseline)
    assert untouched["old_content"] == untouched["new_content"] == "Beta\n\nUnchanged."

    added = get_node_diff(db_path, git_project, "proj::docs/notes.md#gamma", baseline_sha=baseline)
    assert added["old_content"] == ""
    assert added["new_content"] == "Gamma\n\nAdded above."

    whole = get_node_diff(db_path, git_project, "proj::docs/notes.md", baseline_sha=baseline)
    assert whole["old_content"] == old_text
    assert whole["new_content"] == new_text


_CALC = "def add(a, b):\n    return a + b\n\n\ndef keep():\n    return 1\n"


@workflow(
    purpose="With no baseline given, a node gone stale diffs against the last commit before it went stale, never "
    "a checkpoint taken after; a verified node still diffs against its newest checkpoint"
)
def test_default_baseline_of_a_stale_node_predates_the_change(git_project: Path):
    _write(git_project / "calc.py", _CALC)
    before = _commit(git_project, "add calc")
    db_path = _build(git_project)
    _write(git_project / "calc.py", _CALC.replace("a + b", "a + b + 0"))
    edited = _commit(git_project, "edit add")
    record_staleness(db_path, git_project, db.all_nodes(db_path))
    checkpoint = CliRunner().invoke(main, ["history", "checkpoint", str(git_project)])
    assert checkpoint.exit_code == 0, checkpoint.output

    stale = get_node_diff(db_path, git_project, "proj::calc::add")
    assert stale["baseline_sha"] == before
    assert "a + b + 0" in stale["new_content"]
    assert "a + b + 0" not in stale["old_content"]
    assert stale["baseline_reason"].startswith("last commit recorded before the node went CONTENT_UPDATED")

    verified = get_node_diff(db_path, git_project, "proj::calc::keep")
    assert verified["baseline_sha"] == edited
    assert verified["baseline_reason"] == "newest CHECKPOINT entry with a git SHA"


def test_stale_node_with_no_commit_before_the_change_has_no_baseline(tmp_path: Path):
    """A node whose history holds no commit from before it went stale reports no_baseline, not an empty diff."""
    for args in (["init", "--template="], ["config", "user.email", "t@t"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / ".axiom_graph").mkdir()
    db.init_db(tmp_path / ".axiom_graph" / "graph.db")
    _write(tmp_path / "calc.py", _CALC)
    db_path = _build(tmp_path)
    _commit(tmp_path, "add calc")
    _write(tmp_path / "calc.py", _CALC.replace("a + b", "a + b + 0"))
    _commit(tmp_path, "edit add")
    record_staleness(db_path, tmp_path, db.all_nodes(db_path))

    result = get_node_diff(db_path, tmp_path, "proj::calc::add")

    assert result["error"] == "no_baseline"
    assert "no earlier history entry has a git SHA" in result["reason"]


@workflow(purpose="A baseline file that does not parse yields node_position_unresolved, never an empty old side")
def test_unparseable_baseline_is_unresolved(git_project: Path):
    baseline, node_id = _unparseable_baseline(git_project)
    db_path = git_project / ".axiom_graph" / "graph.db"

    result = get_node_diff(db_path, git_project, node_id, baseline_sha=baseline)

    assert result["error"] == "node_position_unresolved"
    assert "does not parse" in result["reason"]
    assert "old_content" not in result


@workflow(purpose="A current file that does not parse yields node_position_unresolved naming the current side")
def test_unparseable_current_file_is_unresolved(git_project: Path):
    _write(git_project / "m.py", "def stable():\n    return 1\n")
    baseline = _commit(git_project, "add m")
    db_path = _build(git_project, full=True)
    _write(git_project / "m.py", "def stable():\n    return 1\n\n\ndef bad(:\n    pass\n")

    result = get_node_diff(db_path, git_project, "proj::m::stable", baseline_sha=baseline)

    assert result["error"] == "node_position_unresolved"
    assert result["reason"].startswith("current file m.py:")
    assert "does not parse" in result["reason"]
    assert "new_content" not in result


@pytest.mark.skipif(not HAS_TREE_SITTER, reason="JS/TS scanning requires the js extra")
@workflow(
    purpose="A TypeScript baseline that parses with errors and lacks the node yields node_position_unresolved, "
    "not an empty old side"
)
def test_typescript_partial_parse_without_the_node_is_unresolved(git_project: Path):
    broken = "export function add(a: number, b: number: number {{{\n  return a + ;\n"
    baseline = _typescript_project(git_project, broken, _ADD_TS)
    db_path = git_project / ".axiom_graph" / "graph.db"

    result = get_node_diff(db_path, git_project, "proj::web.util::add", baseline_sha=baseline)

    assert result["error"] == "node_position_unresolved"
    assert result["reason"].startswith("baseline file web/util.ts")
    assert "parsed with errors" in result["reason"]
    assert "old_content" not in result


@pytest.mark.skipif(not HAS_TREE_SITTER, reason="indexing the TypeScript node requires the js extra")
@workflow(purpose="Without tree-sitter a TypeScript node's diff is node_position_unresolved, not an empty diff")
def test_typescript_without_tree_sitter_is_unresolved(git_project: Path, monkeypatch: pytest.MonkeyPatch):
    baseline = _typescript_project(git_project, _ADD_TS, "// helpers\n" + _ADD_TS)
    db_path = git_project / ".axiom_graph" / "graph.db"
    monkeypatch.setattr(js_scanner, "HAS_TREE_SITTER", False)

    result = get_node_diff(db_path, git_project, "proj::web.util::add", baseline_sha=baseline)

    assert result["error"] == "node_position_unresolved"
    assert "tree-sitter is not installed" in result["reason"]


@pytest.mark.skipif(not HAS_TREE_SITTER, reason="xstate scanning requires the js extra")
@workflow(purpose="An xstate machine in a TypeScript file below inserted code is located by identity on both sides")
def test_xstate_machine_below_shift_located_by_identity(git_project: Path):
    machine = (
        "export const m = createMachine({\n"
        "  id: 'lights',\n"
        "  initial: 'green',\n"
        "  states: {\n"
        "    green: { on: { TICK: 'red' } },\n"
        "    red: { on: { TICK: 'green' } },\n"
        "  },\n"
        "});\n"
    )
    head = "import { createMachine } from 'xstate';\n\n"
    above = "".join(f"export function pad{i}(): number {{\n  return {i};\n}}\n\n" for i in range(5))
    baseline = _typescript_project(git_project, head + machine, head + above + machine)
    db_path = git_project / ".axiom_graph" / "graph.db"
    assert db.get_node(db_path, "proj::web.util::lights@machine") is not None

    result = get_node_diff(db_path, git_project, "proj::web.util::lights@machine", baseline_sha=baseline)

    assert "error" not in result
    assert result["old_content"] == result["new_content"] == "export const m = createMachine({"


@workflow(
    purpose="The MCP diff tool reports +0 / -0 for an unchanged shifted node and passes the unresolved error through"
)
def test_mcp_diff_reports_identity_outcomes(git_project: Path):
    clean_base, clean_id = _shifted_module(git_project)
    summary = json.loads(axiom_graph_diff(str(git_project), clean_id, baseline_sha=clean_base, summary_only=True))
    assert summary["summary"] == "+0 / -0 lines in body"

    bad_base, bad_id = _unparseable_baseline(git_project)
    err = json.loads(axiom_graph_diff(str(git_project), bad_id, baseline_sha=bad_base, summary_only=True))
    assert err["error"] == "node_position_unresolved"


@workflow(purpose="axiom-graph diff prints the MCP tool's JSON for a clean diff and exits 0")
def test_cli_diff_clean(git_project: Path):
    baseline, node_id = _shifted_module(git_project)
    runner = CliRunner()

    summary = runner.invoke(main, ["diff", node_id, str(git_project), "--baseline", baseline, "--summary"])
    assert summary.exit_code == 0, summary.output
    expected = axiom_graph_diff(str(git_project), node_id, baseline_sha=baseline, summary_only=True)
    assert summary.output.rstrip("\n") == expected
    assert json.loads(summary.output)["summary"] == "+0 / -0 lines in body"

    full = runner.invoke(main, ["diff", node_id, str(git_project), "--baseline", baseline])
    assert full.exit_code == 0, full.output
    report = json.loads(full.output)
    assert report["old_content"] == report["new_content"]
    assert report["summary"] == "+0 / -0 lines in body"


@workflow(purpose="axiom-graph diff prints every node's slot, the failing one as its error JSON, and exits 1")
def test_cli_diff_error_and_batch(git_project: Path):
    _, clean_id = _shifted_module(git_project)
    # One baseline for both nodes: the commit where broken.py does not parse;
    # shift.py is already in its final form there.
    bad_base, bad_id = _unparseable_baseline(git_project)

    result = CliRunner().invoke(main, ["diff", clean_id, bad_id, str(git_project), "--baseline", bad_base, "--summary"])

    assert result.exit_code == 1
    slots = result.output.rstrip("\n").split("\n\n---\n\n")
    assert len(slots) == 2
    assert json.loads(slots[0])["summary"] == "+0 / -0 lines in body"
    assert json.loads(slots[1])["error"] == "node_position_unresolved"


_VARIANCE = "def variance(xs):\n    m = sum(xs) / len(xs)\n    return sum((x - m) ** 2 for x in xs) / len(xs)\n"
_VARIANCE_EDITED = (
    "def variance(xs):\n    m = sum(xs) / len(xs)\n    n = len(xs) - 1\n    return sum((x - m) ** 2 for x in xs) / n\n"
)


@workflow(
    purpose=(
        "Verify a node indexed while uncommitted, then committed and edited, diffs against the commit "
        "holding its pre-edit text, not the earlier recorded commit that lacks it"
    )
)
def test_stale_node_indexed_before_its_commit_diffs_against_the_commit_that_holds_it(git_project: Path):
    _write(git_project / "stats.py", _VARIANCE)
    db_path = _build(git_project, full=True)
    held = _commit(git_project, "add variance")
    _write(git_project / "stats.py", _VARIANCE_EDITED)
    _build(git_project)

    result = get_node_diff(db_path, git_project, "proj::stats::variance")

    assert "error" not in result, result
    assert result["baseline_sha"] == held
    assert result["old_content"].rstrip("\n") == _VARIANCE.rstrip("\n")
    assert "n = len(xs) - 1" in result["new_content"]
    assert "holds the node as it was before" in result["baseline_reason"]


@workflow(
    purpose=(
        "Verify a stale node no commit holds as it was before its edit has no baseline: never an all-new "
        "diff when it was never committed, never an unchanged one when its first commit already holds the edit"
    )
)
@pytest.mark.parametrize("commit_the_edit", [False, True], ids=["never-committed", "first-commit-holds-the-edit"])
def test_stale_node_never_committed_before_its_edit_has_no_baseline(git_project: Path, commit_the_edit: bool):
    _write(git_project / "stats.py", _VARIANCE)
    db_path = _build(git_project, full=True)
    _write(git_project / "stats.py", _VARIANCE_EDITED)
    if commit_the_edit:
        _commit(git_project, "add variance, already edited")
    _build(git_project)

    result = get_node_diff(db_path, git_project, "proj::stats::variance")

    assert result["error"] == "no_baseline", result
    assert "did not exist" in result["reason"]
