"""The efficiency analyzer reports each agent's context size, once per agent.

Every assistant turn in a subagent transcript carries the API ``usage``; the
context the agent saw on that turn is ``input_tokens`` plus the cache-creation
and cache-read tokens.  The report shows, per run, the context on the first
turn, the peak and the number of turns, and for a Builder the context at each
task boundary (each write of a ``builder::inc-N.task-M.manifest`` section).
A resumed session holds the same subagent again; it is counted once.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
from axiom_annotations import workflow

SCRIPT = Path(__file__).resolve().parent.parent / "pev_nexus_agents" / "pev" / "scripts" / "analyze_pev_session.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="PEV plugin source not present")

CYCLE = "pev-2026-10-01-context-cycle"
BUILDER_DIR = f"proj::docs/pev/cycles/{CYCLE}/builder"


@pytest.fixture(scope="module")
def analyzer():
    spec = importlib.util.spec_from_file_location("analyze_pev_session", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _dispatch(tool_id: str, agent_type: str) -> dict:
    prompt = f"Cycle {CYCLE}. Manifest docs/pev/cycles/{CYCLE}/manifest."
    return {
        "type": "assistant",
        "timestamp": "2026-10-01T10:00:00.000Z",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Agent",
                    "input": {"subagent_type": agent_type, "description": tool_id, "prompt": prompt},
                }
            ]
        },
    }


def _turn(msg_id: str, context: int, tool: str = "Read", inp: dict | None = None) -> dict:
    """One assistant record; *context* is split over the three input counters."""
    return {
        "type": "assistant",
        "timestamp": "2026-10-01T10:01:00.000Z",
        "message": {
            "id": msg_id,
            "content": [{"type": "tool_use", "id": f"t-{msg_id}-{tool}", "name": tool, "input": inp or {}}],
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 990,
                "cache_read_input_tokens": context - 1000,
                "output_tokens": 50,
            },
        },
    }


def _builder_transcript() -> list[dict]:
    add = "mcp__axiom-graph__axiom_graph_add_section"
    update = "mcp__axiom-graph__axiom_graph_update_section"
    return [
        _turn("m1", 20_000, "Read", {"file_path": "a.py"}),
        # One API response split over two records: one turn, not two.
        _turn("m2", 30_000, "Grep", {"pattern": "x"}),
        _turn("m2", 30_000, "Edit", {"file_path": "a.py"}),
        _turn(
            "m3",
            45_000,
            add,
            {"doc_id": BUILDER_DIR, "sections": [{"section_id": "manifest", "parent_id": "inc-1.task-1"}]},
        ),
        _turn("m4", 90_000, "Edit", {"file_path": "b.py"}),
        _turn("m5", 70_000, update, {"section_id": f"{BUILDER_DIR}::inc-1.task-2.manifest"}),
        _turn("m6", 75_000, update, {"section_id": f"{BUILDER_DIR}::inc-1.task-2.progress"}),
    ]


def _reviewer_transcript() -> list[dict]:
    return [_turn("r1", 15_000), _turn("r2", 25_000)]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _write_session(path: Path) -> Path:
    """A session dispatching a Builder and a Reviewer, with their transcripts."""
    _write_jsonl(path, [_dispatch("b-1", "pev:pev-builder"), _dispatch("r-1", "pev:pev-reviewer")])
    sub = path.with_suffix("") / "subagents"
    for agent_id, tool_id, agent_type, records in (
        ("aaa111", "b-1", "pev:pev-builder", _builder_transcript()),
        ("bbb222", "r-1", "pev:pev-reviewer", _reviewer_transcript()),
    ):
        _write_jsonl(sub / f"agent-{agent_id}.jsonl", records)
        meta = {"agentType": agent_type, "toolUseId": tool_id}
        (sub / f"agent-{agent_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


@pytest.fixture
def session(tmp_path):
    return _write_session(tmp_path / "projects" / "C--repo" / "first.jsonl")


@pytest.fixture
def resumed(session):
    """A resumed session: the same dispatches and the same subagent files again."""
    copy = session.with_name("second.jsonl")
    shutil.copy(session, copy)
    shutil.copytree(session.with_suffix(""), copy.with_suffix(""))
    return copy


def _run(analysis, agent_type):
    return next(r for r in analysis.subagents if r.agent_type == agent_type)


def test_context_metrics_give_first_turn_peak_and_turns(analyzer, session):
    analysis = analyzer.parse_session(str(session), cycle_filter=CYCLE)

    builder = analyzer.context_metrics(_run(analysis, "pev-builder"))
    assert (builder["first_context"], builder["peak_context"], builder["turns"]) == (20_000, 90_000, 6)
    reviewer = analyzer.context_metrics(_run(analysis, "pev-reviewer"))
    assert (reviewer["first_context"], reviewer["peak_context"], reviewer["turns"]) == (15_000, 25_000, 2)


def test_builder_task_boundaries_come_from_manifest_writes_in_both_shapes(analyzer, session):
    analysis = analyzer.parse_session(str(session), cycle_filter=CYCLE)

    metrics = analyzer.context_metrics(_run(analysis, "pev-builder"))

    assert metrics["task_boundaries"] == [("inc-1.task-1", 45_000), ("inc-1.task-2", 70_000)]


@workflow(
    purpose="The staged efficiency report shows every run's first-turn and peak context, its turns, "
    "and a row per Builder task boundary, in the cycle's own directory, tagged pev-efficiency only"
)
def test_docjson_report_shows_context_rows_in_the_cycle_directory(analyzer, session, tmp_path):
    analysis = analyzer.parse_session(str(session), cycle_filter=CYCLE)

    path, slug = analyzer.write_docjson(analysis, output_dir=str(tmp_path / "staged"), assume_yes=True)

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert slug == f"pev/cycles/{CYCLE}/efficiency"
    assert payload["tags"] == ["pev-efficiency"]
    context = next(s for s in payload["sections"] if s["id"] == "context")["content"]
    assert "| pev-builder | b-1 | 6 | 20,000 | 90,000 |" in context
    assert "| pev-reviewer | r-1 | 2 | 15,000 | 25,000 |" in context
    assert "| b-1 | inc-1.task-1 | 45,000 |" in context
    assert "| b-1 | inc-1.task-2 | 70,000 |" in context


def test_a_resumed_sessions_copy_of_an_agent_is_dropped(analyzer, session, resumed):
    first = analyzer.parse_session(str(session), cycle_filter=CYCLE)
    second = analyzer.parse_session(str(resumed), cycle_filter=CYCLE)

    kept = analyzer.dedupe_runs([first, second])

    assert [r.agent_id for r in first.subagents] == ["aaa111", "bbb222"]
    assert second.subagents == []
    assert kept == [first]


@workflow(
    purpose="--find-cycle --summary over an original and a resumed session prints one context line "
    "per agent, with the Builder's task boundaries, and counts the duplicated agent once"
)
def test_find_cycle_summary_prints_each_agents_context_once(analyzer, session, resumed, monkeypatch, capsys):
    monkeypatch.setattr(analyzer, "CLAUDE_PROJECTS_DIR", session.parent.parent)
    monkeypatch.setattr(
        sys, "argv", ["analyze_pev_session.py", "--find-cycle", CYCLE, "--summary", "--project-root", "C:/repo"]
    )

    analyzer.main()

    out = capsys.readouterr().out
    builder_lines = [line for line in out.splitlines() if line.strip().startswith("pev-builder")]
    assert len(builder_lines) == 1
    assert "ctx 20.0k->90.0k, 6 turns" in builder_lines[0]
    assert "task boundaries: inc-1.task-1 45.0k, inc-1.task-2 70.0k" in out
    assert out.count("ctx 15.0k->25.0k, 2 turns") == 1
