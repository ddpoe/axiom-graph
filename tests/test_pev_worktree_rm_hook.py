"""PEV subagents may rm relative paths inside their cycle worktree without a prompt.

``hooks/pev-worktree-rm.sh`` answers ``allow`` only for a PEV subagent running in
the worktree named by ``.pev-state.json`` whose whole command is one plain ``rm``
of relative paths. Everything else gets no opinion, so the consumer's own
permission rules decide.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pev_hook_helpers import BASH, HOOKS_DIR, bash_has_jq, run_hook

pytestmark = [
    pytest.mark.skipif(not HOOKS_DIR.is_dir(), reason="PEV plugin source not present"),
    pytest.mark.skipif(BASH is None, reason="bash not available"),
    pytest.mark.skipif(not bash_has_jq(), reason="jq not available to the hooks"),
]


def _decision(root: Path, command: str, agent_type: str = "pev:pev-builder", cwd: Path | None = None) -> str | None:
    result = run_hook(
        "pev-worktree-rm.sh",
        {"agent_type": agent_type, "tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or root)},
        root,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    (tmp_path / ".pev-state.json").write_text(json.dumps({"worktree_path": str(tmp_path)}), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("command", ["rm _scratch.py", "rm -f a.py b/c.txt", "rm -rf .pev-scratch/tmp"])
def test_plain_relative_rm_in_the_cycle_worktree_is_allowed(worktree: Path, command: str) -> None:
    assert _decision(worktree, command) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "rm ../outside.py",
        "rm /etc/hosts",
        "rm C:/x.py",
        "rm ~/x.py",
        "rm *.py",
        "rm a.py && git push",
        "echo hi; rm x",
        "rm $(cat list)",
        "git rm a.py",
        "ls",
    ],
)
def test_anything_else_gets_no_opinion(worktree: Path, command: str) -> None:
    assert _decision(worktree, command) is None


def test_non_pev_sessions_are_unaffected(worktree: Path) -> None:
    assert _decision(worktree, "rm a.py", agent_type="") is None


def test_rm_outside_the_cycle_worktree_gets_no_opinion(
    worktree: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / ".pev-state.json").write_text(json.dumps({"worktree_path": str(worktree)}), encoding="utf-8")
    assert _decision(elsewhere, "rm a.py") is None


def test_no_state_file_means_no_opinion(tmp_path: Path) -> None:
    assert _decision(tmp_path, "rm a.py") is None


def test_a_leading_cd_into_the_worktree_is_accepted(worktree: Path) -> None:
    (worktree / "sub").mkdir()
    assert _decision(worktree, f'cd "{worktree}" && rm _scratch.py') == "allow"
    assert _decision(worktree, f"cd {worktree.as_posix()} && rm -f a.py") == "allow"
    assert _decision(worktree, "cd sub && rm a.py") == "allow"


@pytest.mark.parametrize(
    "command",
    ["cd .. && rm a.py", "cd sub && rm a.py && git push", "cd sub; rm a.py", "cd sub && rm ../a.py"],
)
def test_a_cd_that_leaves_the_worktree_or_chains_more_gets_no_opinion(worktree: Path, command: str) -> None:
    (worktree / "sub").mkdir()
    assert _decision(worktree, command) is None


def test_a_cd_to_another_folder_gets_no_opinion(worktree: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    assert _decision(worktree, f'cd "{elsewhere}" && rm a.py') is None
