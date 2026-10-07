"""The PEV axiom-graph scope hook pins PEV agents' axiom-graph calls to the cycle worktree.

``hooks/pev-axiom-graph-scope.sh`` blocks an axiom-graph call from a PEV
subagent whose ``project_root`` is missing or is not the worktree named in
``.pev-state.json``.  ``axiom_graph_guide`` takes no arguments and reads no
project, and every PEV agent calls it first, so it passes without one.
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

HOOK = "pev-axiom-graph-scope.sh"


def _write_state(root: Path, worktree: Path) -> None:
    (root / ".pev-state.json").write_text(json.dumps({"worktree_path": str(worktree)}), encoding="utf-8")


def _payload(root: Path, tool: str, tool_input: dict, agent_type: str = "pev:pev-builder") -> dict:
    return {
        "agent_type": agent_type,
        "agent_id": "scope-test",
        "tool_name": f"mcp__axiom-graph__{tool}",
        "tool_input": tool_input,
        "cwd": str(root),
    }


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    _write_state(tmp_path, tmp_path)
    return tmp_path


@pytest.mark.parametrize("agent_type", ["pev:pev-builder", "pev:pev-auditor", "pev:pev-architect"])
def test_guide_passes_without_a_project_root(worktree: Path, agent_type: str) -> None:
    result = run_hook(HOOK, _payload(worktree, "axiom_graph_guide", {}, agent_type), worktree)

    assert result.returncode == 0, result.stderr


def test_a_project_tool_without_project_root_is_still_blocked(worktree: Path) -> None:
    result = run_hook(HOOK, _payload(worktree, "axiom_graph_search", {"query": "x"}), worktree)

    assert result.returncode == 2
    assert "missing project_root" in result.stderr


def test_a_project_tool_on_the_worktree_passes(worktree: Path) -> None:
    payload = _payload(worktree, "axiom_graph_search", {"project_root": str(worktree), "query": "x"})

    assert run_hook(HOOK, payload, worktree).returncode == 0


@pytest.mark.parametrize("tool", ["axiom_graph_search", "axiom_graph_clone_doc"])
def test_a_project_tool_on_another_root_is_blocked(
    worktree: Path, tmp_path_factory: pytest.TempPathFactory, tool: str
) -> None:
    other = tmp_path_factory.mktemp("main-checkout")
    payload = _payload(worktree, tool, {"project_root": str(other), "query": "x"})
    result = run_hook(HOOK, payload, worktree)

    assert result.returncode == 2
    assert "does not match worktree" in result.stderr


def _nested_worktree(tmp_path: Path, layout: str) -> tuple[Path, Path]:
    """A main checkout with the cycle's worktree nested under it, as EnterWorktree lays them out.

    Returns ``(main, worktree)``.  The worktree's state file names it in ``worktree_path``
    with ``layout``; main has no state file of its own.
    """
    main = tmp_path / "main"
    worktree = main / ".claude" / "worktrees" / "the-cycle"
    worktree.mkdir(parents=True)
    state = {"layout": layout, "cycle_doc_id": "proj::docs/x/manifest", "worktree_path": str(worktree)}
    (worktree / ".pev-state.json").write_text(json.dumps(state), encoding="utf-8")
    return main, worktree


@workflow(
    purpose=(
        "The audit roles run in the cycle's worktree, which sits inside the main checkout: an Auditor or Doc "
        "Reviewer of a cycle, or an instance's Reviewer or Doc Reviewer, may call axiom-graph with the worktree "
        "as project_root but is denied main's root, so no audit write reaches main's index before the merge"
    )
)
@pytest.mark.parametrize(
    ("layout", "agent_type"),
    [
        ("directory", "pev:pev-auditor"),
        ("directory", "pev:pev-doc-reviewer"),
        ("instance", "pev:pev-reviewer"),
        ("instance", "pev:pev-doc-reviewer"),
    ],
)
def test_audit_roles_are_pinned_to_the_worktree_not_main(tmp_path: Path, layout: str, agent_type: str) -> None:
    main, worktree = _nested_worktree(tmp_path, layout)
    for tool in ("axiom_graph_mark_clean", "axiom_graph_update_section"):
        on_worktree = _payload(worktree, tool, {"project_root": str(worktree)}, agent_type)
        assert run_hook(HOOK, on_worktree, worktree).returncode == 0, (tool, agent_type)

        on_main = _payload(worktree, tool, {"project_root": str(main)}, agent_type)
        result = run_hook(HOOK, on_main, worktree)
        assert result.returncode == 2, (tool, agent_type)
        assert "does not match worktree" in result.stderr
