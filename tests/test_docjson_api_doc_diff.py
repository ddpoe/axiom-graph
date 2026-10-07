"""Tests for ``get_doc_diff``: old vs new sections of a DocJSON doc against a baseline.

Tier 1 -- plain pytest against real temporary git repos (inline ``docs/``,
no submodule).  Covers renamed docs, renamed-and-edited docs, staged
uncommitted renames, new docs and the unresolvable-rename error.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from axiom_graph.docjson.api import get_doc_diff
from axiom_graph.index import builder


def _git(args: list[str], cwd: Path) -> str:
    """Run a git command in *cwd* and return its stripped stdout."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _doc(sections: dict[str, str]) -> str:
    """Return DocJSON text for a doc whose sections map id -> content."""
    return json.dumps(
        {
            "title": "Guide",
            "sections": [{"id": sid, "heading": sid.title(), "content": body} for sid, body in sections.items()],
        },
        indent=2,
    )


_SECTIONS = {
    "overview": "What the guide covers.",
    "setup": "Install the package.",
    "usage": "Run the command.",
}


def _commit_all(repo: Path, message: str) -> str:
    """Stage everything, commit, and return the new HEAD SHA."""
    _git(["add", "-A"], repo)
    _git(["commit", "-m", message], repo)
    return _git(["rev-parse", "HEAD"], repo)


def _index(repo: Path) -> Path:
    """Build the axiom-graph index for *repo* and return its DB path."""
    builder.build(repo, project_id="proj", discovery_only=False)
    return repo / ".axiom_graph" / "graph.db"


def _section_map(sections: list[dict]) -> dict[str, str]:
    return {s["id"]: s["content"] for s in sections}


def test_doc_diff_follows_extension_rename(git_project: Path):
    """A doc renamed with no content change diffs as unchanged, read from its old path."""
    docs = git_project / "docs"
    docs.mkdir()
    (docs / "guide.json").write_text(_doc(_SECTIONS), encoding="utf-8")
    baseline = _commit_all(git_project, "add guide")
    _git(["mv", "docs/guide.json", "docs/guide.docjson"], git_project)
    _commit_all(git_project, "rename guide")
    db_path = _index(git_project)

    result = get_doc_diff(db_path, git_project, "proj::docs/guide", baseline_sha=baseline)

    assert "error" not in result
    assert len(result["old_sections"]) == len(_SECTIONS)
    assert result["old_sections"] == result["new_sections"]
    assert result["baseline_path"] == "docs/guide.json"
    assert result["path"] == "docs/guide.docjson"
    assert result["baseline_rev"] == baseline
    assert "submodule_sha" not in result


def test_doc_diff_endpoint_reports_baseline_path(git_project: Path):
    """The viz doc-diff endpoint passes the rename-aware paths through to the panel."""
    from axiom_graph.viz.docs import get_doc_diff_endpoint
    from axiom_graph.viz.server import _apply_project

    docs = git_project / "docs"
    docs.mkdir()
    (docs / "guide.json").write_text(_doc(_SECTIONS), encoding="utf-8")
    baseline = _commit_all(git_project, "add guide")
    _git(["mv", "docs/guide.json", "docs/guide.docjson"], git_project)
    _commit_all(git_project, "rename guide")
    _index(git_project)
    _apply_project(git_project)

    result = get_doc_diff_endpoint("proj::docs/guide", sha=baseline)

    assert result["doc_id"] == "proj::docs/guide"
    assert result["old_sections"] == result["new_sections"]
    assert (result["path"], result["baseline_path"]) == ("docs/guide.docjson", "docs/guide.json")


def test_doc_diff_renamed_and_edited_shows_only_the_edit(git_project: Path):
    """A doc moved and edited diffs as the edit alone, not as a wholly new doc."""
    docs = git_project / "docs"
    docs.mkdir()
    (docs / "guide.json").write_text(_doc(_SECTIONS), encoding="utf-8")
    baseline = _commit_all(git_project, "add guide")
    (docs / "manual").mkdir()
    _git(["mv", "docs/guide.json", "docs/manual/guide.docjson"], git_project)
    edited = dict(_SECTIONS, usage="Run the command with --verbose.")
    (docs / "manual" / "guide.docjson").write_text(_doc(edited), encoding="utf-8")
    _commit_all(git_project, "move and edit guide")
    db_path = _index(git_project)

    result = get_doc_diff(db_path, git_project, "proj::docs/manual/guide", baseline_sha=baseline)

    assert "error" not in result
    old, new = _section_map(result["old_sections"]), _section_map(result["new_sections"])
    assert old.keys() == new.keys()
    changed = {sid for sid in new if old[sid] != new[sid]}
    assert changed == {"usage"}
    assert result["baseline_path"] == "docs/guide.json"


def test_doc_diff_follows_staged_uncommitted_rename(git_project: Path):
    """A rename staged in the index but not yet committed is followed."""
    docs = git_project / "docs"
    docs.mkdir()
    (docs / "guide.json").write_text(_doc(_SECTIONS), encoding="utf-8")
    baseline = _commit_all(git_project, "add guide")
    _git(["mv", "docs/guide.json", "docs/guide.docjson"], git_project)
    db_path = _index(git_project)

    result = get_doc_diff(db_path, git_project, "proj::docs/guide", baseline_sha=baseline)

    assert "error" not in result
    assert result["old_sections"] == result["new_sections"]
    assert result["baseline_path"] == "docs/guide.json"


def test_doc_diff_new_doc_has_empty_old_side(git_project: Path):
    """A doc that did not exist at the baseline diffs with no old sections."""
    baseline = _git(["rev-parse", "HEAD"], git_project)
    docs = git_project / "docs"
    docs.mkdir()
    (docs / "guide.docjson").write_text(_doc(_SECTIONS), encoding="utf-8")
    _commit_all(git_project, "add guide")
    db_path = _index(git_project)

    result = get_doc_diff(db_path, git_project, "proj::docs/guide", baseline_sha=baseline)

    assert "error" not in result
    assert result["old_sections"] == []
    assert len(result["new_sections"]) == len(_SECTIONS)
    assert result["baseline_path"] is None


def test_doc_diff_unresolvable_rename_is_error(git_project: Path):
    """When git cannot tell whether the doc was renamed, the diff errors instead of reading as new."""
    docs = git_project / "docs"
    docs.mkdir()
    for name in ("a", "b", "c"):
        (docs / f"{name}.json").write_text(_doc({**_SECTIONS, "name": name}), encoding="utf-8")
    baseline = _commit_all(git_project, "add docs")
    # Rename and edit every doc so only inexact rename detection could pair
    # them, then cap the rename matrix so git skips that detection.
    for name in ("a", "b", "c"):
        _git(["mv", f"docs/{name}.json", f"docs/{name}.docjson"], git_project)
        (docs / f"{name}.docjson").write_text(
            _doc({**_SECTIONS, "name": name, "usage": "Run it."}),
            encoding="utf-8",
        )
    _commit_all(git_project, "rename and edit docs")
    _git(["config", "diff.renameLimit", "1"], git_project)
    db_path = _index(git_project)

    result = get_doc_diff(db_path, git_project, "proj::docs/a", baseline_sha=baseline)

    assert result["error"] == "baseline_path_unresolved"
    assert "docs/a.docjson" in result["reason"]
    assert "old_sections" not in result
