"""The efficiency analyzer finds and keeps only the sessions and calls of one cycle.

``--find-cycle`` searches every Claude Code project folder for the sessions
that orchestrated a PEV cycle.  A session counts when it dispatches a ``pev-*``
agent for the cycle -- not when it merely mentions the id -- and the cycle id
may first appear deep in a long log.  Within a session, each call is
attributed to the cycle it names most (calls naming none stay with the
current cycle), so a filtered report holds only the requested cycle's work
and carries its id.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "pev_nexus_agents" / "pev" / "scripts" / "analyze_pev_session.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="PEV plugin source not present")

CYCLE = "pev-2026-10-01-target-cycle"
SIBLING = "pev-2026-09-30-sibling-cycle"
LATER = "pev-2026-10-02-later-cycle"


@pytest.fixture(scope="module")
def analyzer():
    spec = importlib.util.spec_from_file_location("analyze_pev_session", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _tool_use(tool_id: str, name: str, inp: dict) -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-10-01T10:00:00.000Z",
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]},
    }


def _dispatch(tool_id: str, agent_type: str, prompt: str) -> dict:
    return _tool_use(tool_id, "Agent", {"subagent_type": agent_type, "description": tool_id, "prompt": prompt})


def _user(text: str) -> dict:
    return {"type": "user", "timestamp": "2026-10-01T09:00:00.000Z", "message": {"content": text}}


def _manifest_prompt(own: str, cited: str | None = None) -> str:
    prompt = f"Cycle {own}. Manifest docs/pev/cycles/{own}.docjson, sections {own}::architect.pitch."
    if cited:
        prompt += f" Related prior work: {cited}."
    return prompt


def _write_session(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _owning_session(projects: Path) -> Path:
    """A main-checkout session: a long preamble, then the cycle, then a later cycle."""
    preamble = [_user(f"unrelated chat turn {i}") for i in range(300)]
    preamble.append(_tool_use("pre-1", "Bash", {"command": "git status"}))
    cycle = [
        _tool_use("orch-1", "mcp__axiom-graph__axiom_graph_write_doc", {"doc_file": f"docs/pev/cycles/{CYCLE}.json"}),
        _dispatch("arch-1", "pev:pev-architect", _manifest_prompt(CYCLE, cited=SIBLING)),
        _tool_use("orch-2", "Bash", {"command": "git log -1"}),
        _dispatch("rev-1", "superpowers:code-reviewer", "Review the branch diff."),
        _dispatch("later-1", "pev:pev-builder", _manifest_prompt(LATER)),
        _tool_use("orch-3", "Bash", {"command": "git status"}),
    ]
    return _write_session(projects / "C--repo" / "owning-session.jsonl", preamble + cycle)


def _sibling_session(projects: Path) -> Path:
    """A sibling cycle's session in a worktree folder whose prompt cites the target once."""
    records = [_dispatch("sib-1", "pev:pev-architect", _manifest_prompt(SIBLING, cited=CYCLE))]
    return _write_session(projects / "C--repo--claude-worktrees-sibling" / "sibling-session.jsonl", records)


def _mention_only_session(projects: Path) -> Path:
    """A session that reads the cycle's manifest but dispatches nothing for it."""
    records = [
        _user(f"what happened in {CYCLE}?"),
        _tool_use("read-1", "mcp__axiom-graph__axiom_graph_read_doc", {"doc_id": f"proj::docs/pev/cycles/{CYCLE}"}),
        _dispatch("gp-1", "general-purpose", f"Summarise {CYCLE}."),
    ]
    return _write_session(projects / "c--repo" / "mention-session.jsonl", records)


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    _owning_session(root)
    _sibling_session(root)
    _mention_only_session(root)
    return root


# ── named_cycle ──────────────────────────────────────────────────────────────


def test_named_cycle_prefers_the_most_named_id_then_the_first(analyzer):
    assert analyzer.named_cycle(_manifest_prompt(SIBLING, cited=CYCLE)) == SIBLING
    assert analyzer.named_cycle(f"{CYCLE} and {SIBLING}") == CYCLE
    assert analyzer.named_cycle("no cycle here") is None


def test_named_cycle_excludes_trailing_punctuation(analyzer):
    assert analyzer.named_cycle(f"Run {CYCLE}.") == CYCLE
    assert analyzer.named_cycle(f"Run {CYCLE}-") == CYCLE


# ── find_sessions_for_cycle ──────────────────────────────────────────────────


def test_find_cycle_matches_only_the_session_that_dispatched_for_it(analyzer, projects):
    matches = analyzer.find_sessions_for_cycle(CYCLE, projects_dir=projects)

    assert [Path(m).name for m in matches] == ["owning-session.jsonl"]


def test_find_cycle_reads_past_a_long_preamble(analyzer, projects):
    owning = projects / "C--repo" / "owning-session.jsonl"
    lines = owning.read_text(encoding="utf-8").splitlines()
    first = next(i for i, line in enumerate(lines) if CYCLE in line)
    assert first > 300, "fixture must name the cycle only after a long preamble"

    assert str(owning) in analyzer.find_sessions_for_cycle(CYCLE, projects_dir=projects)


def test_find_cycle_finds_the_sibling_cycle_in_its_worktree_folder(analyzer, projects):
    matches = analyzer.find_sessions_for_cycle(SIBLING, projects_dir=projects)

    assert [Path(m).name for m in matches] == ["sibling-session.jsonl"]


def test_find_cycle_skips_subagent_transcripts(analyzer, tmp_path):
    root = tmp_path / "projects"
    _write_session(
        root / "C--repo" / "parent" / "subagents" / "agent-a1.jsonl",
        [_dispatch("nested-1", "pev:pev-builder", _manifest_prompt(CYCLE))],
    )

    assert analyzer.find_sessions_for_cycle(CYCLE, projects_dir=root) == []


def test_find_cycle_orders_sessions_oldest_first(analyzer, tmp_path):
    root = tmp_path / "projects"
    newer = _write_session(root / "a" / "newer.jsonl", [_dispatch("d1", "pev:pev-auditor", _manifest_prompt(CYCLE))])
    older = _write_session(root / "b" / "older.jsonl", [_dispatch("d2", "pev:pev-builder", _manifest_prompt(CYCLE))])
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    assert analyzer.find_sessions_for_cycle(CYCLE, projects_dir=root) == [str(older), str(newer)]


def test_find_cycle_with_no_projects_folder_finds_nothing(analyzer, tmp_path):
    assert analyzer.find_sessions_for_cycle(CYCLE, projects_dir=tmp_path / "absent") == []


# ── parse_session attribution ────────────────────────────────────────────────


def test_filtered_session_keeps_only_the_cycles_dispatches(analyzer, projects):
    analysis = analyzer.parse_session(str(projects / "C--repo" / "owning-session.jsonl"), cycle_filter=CYCLE)

    assert analysis.cycle_id == CYCLE
    assert [r.tool_use_id for r in analysis.subagents] == ["arch-1", "rev-1"]


def test_filtered_session_drops_orchestrator_calls_outside_the_cycle(analyzer, projects):
    analysis = analyzer.parse_session(str(projects / "C--repo" / "owning-session.jsonl"), cycle_filter=CYCLE)

    assert [tc.input_summary for tc in analysis.orchestrator_tools] == ["['doc_file']", "git log -1"]


def test_filtered_report_carries_the_requested_cycle_id(analyzer, projects):
    sibling = str(projects / "C--repo--claude-worktrees-sibling" / "sibling-session.jsonl")

    assert analyzer.parse_session(sibling, cycle_filter=CYCLE).subagents == []
    analysis = analyzer.parse_session(sibling, cycle_filter=SIBLING)
    assert analysis.cycle_id == SIBLING
    assert [r.tool_use_id for r in analysis.subagents] == ["sib-1"]


def test_unfiltered_session_keeps_everything_and_names_the_first_cycle(analyzer, projects):
    analysis = analyzer.parse_session(str(projects / "C--repo" / "owning-session.jsonl"))

    assert analysis.cycle_id == CYCLE
    assert [r.tool_use_id for r in analysis.subagents] == ["arch-1", "rev-1", "later-1"]
    assert len(analysis.orchestrator_tools) == 4


# ── project scoping ──────────────────────────────────────────────────────────


def _in(record: dict, cwd: Path) -> dict:
    return {**record, "cwd": str(cwd)}


def test_find_cycle_with_a_project_root_leaves_out_another_project_that_reused_the_id(analyzer, tmp_path):
    """Run ids are a date plus a slug, so an earlier dry run in a sibling folder can reuse one."""
    project = tmp_path / "work" / "fresh-3-2"
    sibling_project = tmp_path / "work" / "fresh-3-21"  # its path starts with fresh-3-2 but is not under it
    worktree = project / ".claude" / "worktrees" / "pev-cycle"
    sibling_worktree = sibling_project / ".claude" / "worktrees" / "pev-cycle"
    root = tmp_path / "projects"
    dispatch = _dispatch("d1", "pev:pev-architect", _manifest_prompt(CYCLE))
    main = _write_session(root / analyzer.project_slug(str(project)) / "main.jsonl", [_in(dispatch, project)])
    in_worktree = _write_session(root / "moved-folder" / "wt.jsonl", [_in(dispatch, worktree)])
    no_cwd = _write_session(root / (analyzer.project_slug(str(worktree)).lower()) / "old.jsonl", [dispatch])
    foreign = _write_session(
        root / analyzer.project_slug(str(sibling_project)) / "foreign.jsonl", [_in(dispatch, sibling_project)]
    )
    foreign_no_cwd = _write_session(
        root / analyzer.project_slug(str(sibling_project)) / "foreign-old.jsonl", [dispatch]
    )
    foreign_worktree_no_cwd = _write_session(
        root / analyzer.project_slug(str(sibling_worktree)) / "foreign-wt.jsonl", [dispatch]
    )
    for i, path in enumerate([main, in_worktree, no_cwd, foreign, foreign_no_cwd, foreign_worktree_no_cwd]):
        os.utime(path, (1_000_000 + i, 1_000_000 + i))

    scoped = analyzer.find_sessions_for_run(CYCLE, projects_dir=root, project_root=str(project))

    assert scoped == [str(main), str(in_worktree), str(no_cwd)]
    assert str(foreign) in analyzer.find_sessions_for_run(CYCLE, projects_dir=root)


def test_a_worktree_or_git_bash_path_resolves_to_its_main_checkout(analyzer):
    assert analyzer.main_checkout(r"C:\repo\.claude\worktrees\pev-x") == r"C:\repo"
    assert analyzer.main_checkout("/home/u/repo") == "/home/u/repo"
    assert analyzer.normalize_path("/c/Users/Me/repo/") == analyzer.normalize_path(r"C:\Users\me\repo")
    assert analyzer.normalize_path("/mnt/c/Users/Me/repo") == analyzer.normalize_path(r"C:\Users\me\repo")
    assert analyzer.normalize_path("/home/u/Repo") != analyzer.normalize_path("/home/u/repo")


@pytest.mark.parametrize("root", ["/c/Users/Me/repo", "/mnt/c/Users/Me/repo"], ids=["git-bash", "wsl"])
def test_a_record_without_cwd_matches_the_windows_folder_of_a_git_bash_or_wsl_root(analyzer, tmp_path, root):
    """Claude Code on Windows names the folder after the drive path (C--Users-...), whatever shell gave the root."""
    projects = tmp_path / "projects"
    record = _dispatch("d1", "pev:pev-architect", _manifest_prompt(CYCLE))
    own = _write_session(projects / analyzer.project_slug(r"C:\Users\Me\repo") / "own.jsonl", [record])
    in_worktree = _write_session(
        projects / analyzer.project_slug(r"C:\Users\Me\repo\.claude\worktrees\pev-x") / "wt.jsonl", [record]
    )
    sibling = _write_session(projects / analyzer.project_slug(r"C:\Users\Me\repo2") / "other.jsonl", [record])

    assert analyzer._record_in_project(record, own, root)
    assert analyzer._record_in_project(record, in_worktree, root)
    assert not analyzer._record_in_project(record, sibling, root)
