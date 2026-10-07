"""Two narrow file-tool carve-outs in the PEV hooks.

``hooks/pev-doc-scope-md.sh`` lets the Auditor Write or Edit markdown and
nothing else: code is the Builder's and DocJSON goes through the doc tools.
``hooks/pev-worktree-scope.sh`` lets a PEV agent write inside this session's
Claude Code scratchpad, ``<temp>/claude[-uid]/<slug>/<session_id>/scratchpad``,
and still blocks the rest of the temp root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from axiom_annotations import workflow
from pev_hook_helpers import BASH, HOOKS_DIR, bash_has_jq, run_hook

pytestmark = [
    pytest.mark.skipif(not HOOKS_DIR.is_dir(), reason="PEV plugin source not present"),
    pytest.mark.skipif(BASH is None, reason="bash not available"),
    pytest.mark.skipif(not bash_has_jq(), reason="jq not available to the hooks"),
]

SESSION = "0a1b2c3d-sess"


def _file_tool(
    root: Path, agent: str, tool: str, file_path: Path | str, script: str, temp: Path | None = None, **extra
):
    """Run one file-tool hook from ``root`` with TMPDIR set to ``temp`` (default ``root/temp``)."""
    payload = {
        "agent_type": f"pev:pev-{agent}",
        "agent_id": "file-hook-test",
        "tool_name": tool,
        "tool_input": {"file_path": str(file_path), "content": "x"},
        "cwd": str(root),
        **extra,
    }
    return run_hook(script, payload, root, {"TMPDIR": (temp or root / "temp").as_posix()})


@workflow(
    purpose=(
        "The Auditor may Write or Edit markdown files only: a CHANGELOG.md edit is allowed, a .py edit is denied "
        "as a needs_fix, and a .docjson edit is denied with a pointer to the doc tools"
    )
)
@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_auditor_edits_markdown_never_code_or_docjson(tool, tmp_path):
    allowed = _file_tool(tmp_path, "auditor", tool, tmp_path / "CHANGELOG.md", "pev-doc-scope-md.sh")
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout == ""

    code = _file_tool(tmp_path, "auditor", tool, tmp_path / "pkg" / "mod.py", "pev-doc-scope-md.sh")
    decision = json.loads(code.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "needs_fix" in decision["permissionDecisionReason"]
    assert "mod.py" in decision["permissionDecisionReason"]

    doc = _file_tool(tmp_path, "auditor", tool, tmp_path / "docs" / "x.docjson", "pev-doc-scope-md.sh")
    decision = json.loads(doc.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "axiom_graph_update_section" in decision["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("target", "allowed"),
    [
        ("inside", True),
        ("relative", True),
        ("outside", False),
        ("dotdot", False),
    ],
)
def test_auditor_markdown_stays_inside_the_project_root(target, allowed, tmp_path):
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    paths = {
        "inside": root / "docs" / "notes.md",
        "relative": "CHANGELOG.md",
        "outside": tmp_path / "elsewhere" / "README.md",
        "dotdot": f"{root.as_posix()}/docs/../../escape.md",
    }
    result = _file_tool(root, "auditor", "Write", paths[target], "pev-doc-scope-md.sh")
    assert result.returncode == 0, result.stderr
    if allowed:
        assert result.stdout == ""
    else:
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "outside" in decision["permissionDecisionReason"]


@workflow(
    purpose=(
        "The Auditor runs in the cycle's worktree, nested inside the main checkout: both file hooks let it Write a "
        "markdown file in the worktree and deny one in main's tree, though main's path is a prefix of the worktree's"
    )
)
@pytest.mark.parametrize("script", ["pev-doc-scope-md.sh", "pev-worktree-scope.sh"])
def test_auditor_markdown_in_the_worktree_never_on_main(script, tmp_path):
    main = tmp_path / "main"
    worktree = main / ".claude" / "worktrees" / "the-cycle"
    (worktree / "docs").mkdir(parents=True)
    (main / "docs").mkdir()
    state = {"layout": "directory", "cycle_doc_id": "proj::docs/x/manifest", "worktree_path": str(worktree)}
    (worktree / ".pev-state.json").write_text(json.dumps(state), encoding="utf-8")

    inside = _file_tool(worktree, "auditor", "Write", worktree / "docs" / "notes.md", script)
    assert inside.returncode == 0, inside.stderr
    assert inside.stdout == ""

    for on_main in (main / "CHANGELOG.md", main / "docs" / "notes.md"):
        result = _file_tool(worktree, "auditor", "Write", on_main, script)
        if script == "pev-worktree-scope.sh":
            assert result.returncode == 2, on_main
            assert "outside the worktree" in result.stderr
        else:
            assert result.returncode == 0, result.stderr
            decision = json.loads(result.stdout)["hookSpecificOutput"]
            assert decision["permissionDecision"] == "deny", on_main
            assert "outside" in decision["permissionDecisionReason"]


@pytest.mark.parametrize("agent", ["builder", "reviewer"])
def test_markdown_hook_ignores_other_agents(agent, tmp_path):
    result = _file_tool(tmp_path, agent, "Write", tmp_path / "pkg" / "mod.py", "pev-doc-scope-md.sh")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def _scratchpad_setup(tmp_path: Path) -> Path:
    """A worktree with a state file, and this session's scratchpad dir; return the scratchpad."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".pev-state.json").write_text(json.dumps({"worktree_path": str(worktree)}), encoding="utf-8")
    scratch = tmp_path / "temp" / "claude" / "C--Users-me-proj" / SESSION / "scratchpad"
    scratch.mkdir(parents=True)
    return scratch


@pytest.mark.parametrize(
    ("relative", "session", "allowed"),
    [
        ("claude/C--Users-me-proj/{s}/scratchpad/notes.py", SESSION, True),
        ("claude/C--Users-me-proj/{s}/scratchpad/new-dir/out.txt", SESSION, True),
        ("claude-1000/-home-me-proj/{s}/scratchpad/notes.py", SESSION, True),
        ("claude/C--Users-me-proj/{s}/scratchpad/notes.py", "another-session", False),
        ("claude/C--Users-me-proj/{s}/scratchpad/notes.py", None, False),
        ("claude/C--Users-me-proj/{s}/notes.py", SESSION, False),
        ("claude/C--Users-me-proj/{s}/scratchpad/../../escape.py", SESSION, False),
        ("notes.py", SESSION, False),
        ("claude/notes.py", SESSION, False),
    ],
)
def test_scratchpad_carve_out_is_exact(relative, session, allowed, tmp_path):
    _scratchpad_setup(tmp_path)
    worktree = tmp_path / "wt"
    target = (tmp_path / "temp").as_posix() + "/" + relative.format(s=SESSION)
    extra = {"session_id": session} if session is not None else {}

    result = _file_tool(worktree, "builder", "Write", target, "pev-worktree-scope.sh", tmp_path / "temp", **extra)

    if allowed:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 2
        assert "outside the worktree" in result.stderr
