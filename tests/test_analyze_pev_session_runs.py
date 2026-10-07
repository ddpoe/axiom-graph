"""The efficiency analyzer reports audit and instance runs as it does cycles.

``--find-run`` takes a cycle, audit or instance id.  An audit run is found by
the ``pev-*`` agents its session dispatches for it; an instance is worked in
the main session itself, so a write naming it is enough, and the main
session's calls from the ``/pev-instance`` command on count as the
instance's own.  Each report is staged with the run directory's
``efficiency`` doc id and carries only the ``pev-efficiency`` tag.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from axiom_annotations import workflow

SCRIPT = Path(__file__).resolve().parent.parent / "pev_nexus_agents" / "pev" / "scripts" / "analyze_pev_session.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="PEV plugin source not present")

AUDIT = "pev-audit-dev-docs-2026-10-01-sweep"
OTHER_AUDIT = "pev-audit-annotations-2026-10-02-pass"
INSTANCE = "pev-instance-2026-10-01-fix-typo"
CYCLE = "pev-2026-10-01-some-cycle"
CLONE = "mcp__axiom-graph__axiom_graph_clone_doc"
UPDATE = "mcp__axiom-graph__axiom_graph_update_section"


@pytest.fixture(scope="module")
def analyzer():
    spec = importlib.util.spec_from_file_location("analyze_pev_session", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _tool_use(tool_id: str, name: str, inp: dict, context: int | None = None) -> dict:
    message = {"id": f"msg-{tool_id}", "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}
    if context is not None:
        message["usage"] = {"input_tokens": context, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    return {"type": "assistant", "timestamp": "2026-10-01T10:00:00.000Z", "message": message}


def _dispatch(tool_id: str, agent_type: str, prompt: str) -> dict:
    return _tool_use(tool_id, "Agent", {"subagent_type": agent_type, "description": tool_id, "prompt": prompt})


def _command(name: str) -> dict:
    text = f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>"
    return {"type": "user", "timestamp": "2026-10-01T09:00:00.000Z", "message": {"content": text}}


def _write_session(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _audit_session(projects: Path) -> Path:
    manifest = f"proj::docs/pev/audits/{AUDIT}/audit"
    return _write_session(
        projects / "C--repo" / "audit-session.jsonl",
        [
            _tool_use("a-clone", CLONE, {"new_id": f"pev/audits/{AUDIT}/audit"}),
            _dispatch("a-shard", "pev:pev-audit-dev-shard", f"Audit {AUDIT}. Manifest {manifest}."),
            _tool_use("a-git", "Bash", {"command": "git status"}),
            _dispatch("c-arch", "pev:pev-architect", f"Cycle {CYCLE}. Manifest docs/pev/cycles/{CYCLE}/manifest."),
            _tool_use("c-git", "Bash", {"command": "git log -1"}),
        ],
    )


def _other_audit_session(projects: Path) -> Path:
    prompt = f"Audit {OTHER_AUDIT}. Earlier run: {AUDIT}. Manifest docs/pev/audits/{OTHER_AUDIT}/audit."
    return _write_session(
        projects / "C--repo" / "other-audit-session.jsonl",
        [_dispatch("o-shard", "pev:pev-audit-annotations-fixer", prompt)],
    )


def _instance_session(projects: Path) -> Path:
    checkin = f"proj::docs/pev/instances/{INSTANCE}/checkin"
    return _write_session(
        projects / "C--repo" / "instance-session.jsonl",
        [
            _dispatch("c-build", "pev:pev-builder", f"Cycle {CYCLE}. Manifest docs/pev/cycles/{CYCLE}/manifest."),
            _command("pev-instance"),
            _tool_use("i-read", "Read", {"file_path": "src/a.py"}, context=30_000),
            _tool_use("i-clone", CLONE, {"new_id": f"pev/instances/{INSTANCE}/checkin"}, context=40_000),
            _tool_use("i-edit", "Edit", {"file_path": "src/a.py"}, context=55_000),
            _dispatch("i-rev", "pev:pev-reviewer", f"Review instance {INSTANCE}. Checkin {checkin}."),
            _tool_use("i-done", UPDATE, {"section_id": f"{checkin}::meta"}, context=50_000),
        ],
    )


def _mention_session(projects: Path) -> Path:
    read = "mcp__axiom-graph__axiom_graph_read_doc"
    return _write_session(
        projects / "C--repo" / "mention-session.jsonl",
        [_tool_use("m-read", read, {"doc_id": f"proj::docs/pev/instances/{INSTANCE}/checkin"})],
    )


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    _audit_session(root)
    _other_audit_session(root)
    _instance_session(root)
    _mention_session(root)
    return root


@workflow(
    purpose="The run finder keeps only the sessions that worked one audit run, and only that run's "
    "dispatches and calls; a later cycle in the same session is dropped"
)
def test_audit_run_finder_keeps_only_that_runs_sessions_and_calls(analyzer, projects):
    matches = analyzer.find_sessions_for_run(AUDIT, projects_dir=projects)

    assert [Path(m).name for m in matches] == ["audit-session.jsonl"]
    analysis = analyzer.parse_session(matches[0], cycle_filter=AUDIT)
    assert analysis.cycle_id == AUDIT
    assert [r.tool_use_id for r in analysis.subagents] == ["a-shard"]
    assert [tc.input_summary for tc in analysis.orchestrator_tools] == ["['new_id']", "git status"]


@workflow(
    purpose="An instance run is found by the writes that name it, and its main-session calls from the "
    "/pev-instance command on are its own, with their context, while a reader session is not matched"
)
def test_instance_run_counts_its_main_session_calls(analyzer, projects):
    matches = analyzer.find_sessions_for_run(INSTANCE, projects_dir=projects)

    assert [Path(m).name for m in matches] == ["instance-session.jsonl"]
    analysis = analyzer.parse_session(matches[0], cycle_filter=INSTANCE)
    assert [r.tool_use_id for r in analysis.subagents] == ["i-rev"]
    assert [tc.name for tc in analysis.orchestrator_tools] == ["Read", CLONE, "Edit", UPDATE]
    main = analyzer.context_metrics(analysis.main_run)
    assert (main["first_context"], main["peak_context"], main["turns"]) == (30_000, 55_000, 4)


@pytest.mark.parametrize(
    ("run_id", "folder"),
    [
        (CYCLE, f"pev/cycles/{CYCLE}"),
        (AUDIT, f"pev/audits/{AUDIT}"),
        (INSTANCE, f"pev/instances/{INSTANCE}"),
    ],
)
def test_staged_report_lands_in_the_run_directory_with_only_the_efficiency_tag(analyzer, tmp_path, run_id, folder):
    analysis = analyzer.SessionAnalysis(file_path="s.jsonl", cycle_id=run_id)

    path, slug = analyzer.write_docjson(analysis, output_dir=str(tmp_path), assume_yes=True)
    _, second = analyzer.write_docjson(analysis, output_dir=str(tmp_path), session_index=2)

    assert slug == f"{folder}/efficiency"
    assert second == f"{folder}/efficiency-s2"
    assert json.loads(Path(path).read_text(encoding="utf-8"))["tags"] == ["pev-efficiency"]


def test_finder_accepts_an_old_single_file_run(analyzer, tmp_path):
    root = tmp_path / "projects"
    old = _write_session(
        root / "C--repo" / "old.jsonl",
        [_tool_use("u-1", UPDATE, {"section_id": f"proj::docs/pev/instances/{INSTANCE}::status"})],
    )

    assert analyzer.find_sessions_for_run(INSTANCE, projects_dir=root) == [str(old)]
    assert [tc.name for tc in analyzer.parse_session(str(old), cycle_filter=INSTANCE).orchestrator_tools] == [UPDATE]


@pytest.mark.parametrize(
    "write",
    [
        (
            "mcp__axiom-graph__axiom_graph_write_doc",
            {"doc_file": f"C:\\Temp\\pev-efficiency\\{INSTANCE}-efficiency.docjson"},
        ),
        (UPDATE, {"section_id": f"proj::docs/pev/instances/{INSTANCE}/efficiency-s2::summary"}),
        (UPDATE, {"section_id": f"proj::docs/pev/cycles/efficiency/{INSTANCE}-efficiency::summary"}),
        (UPDATE, {"section_id": f"proj::docs/pev/cycles-efficiency/{INSTANCE}-efficiency::summary"}),
    ],
)
def test_finder_skips_a_session_that_only_wrote_the_runs_efficiency_report(analyzer, tmp_path, write):
    root = tmp_path / "projects"
    _write_session(root / "C--repo" / "report.jsonl", [_tool_use("e-1", *write)])

    assert analyzer.find_sessions_for_run(INSTANCE, projects_dir=root) == []


def _usage(tokens: int) -> dict:
    return {"input_tokens": tokens, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


def test_main_session_counts_each_manifest_write_once_and_turns_without_a_tool_call(analyzer, tmp_path):
    manifest = f"proj::docs/pev/cycles/{CYCLE}/builder::inc-1.task-1.manifest"
    two_calls = {
        "id": "msg-two",
        "usage": _usage(40_000),
        "content": [
            {"type": "tool_use", "id": "t-write", "name": UPDATE, "input": {"section_id": manifest}},
            {"type": "tool_use", "id": "t-read", "name": "Read", "input": {"file_path": "src/a.py"}},
        ],
    }
    text_only = {
        "id": "msg-text",
        "usage": _usage(70_000),
        "content": [{"type": "text", "text": "Reviewing the plan before the next dispatch."}],
    }
    session = _write_session(
        tmp_path / "main.jsonl",
        [
            _dispatch("c-arch", "pev:pev-architect", f"Cycle {CYCLE}. Manifest docs/pev/cycles/{CYCLE}/manifest."),
            {"type": "assistant", "message": two_calls},
            {"type": "assistant", "message": text_only},
        ],
    )

    main = analyzer.context_metrics(analyzer.parse_session(str(session), cycle_filter=CYCLE).main_run)

    assert main["task_boundaries"] == [("inc-1.task-1", 40_000)]
    assert (main["turns"], main["peak_context"]) == (2, 70_000)


def test_instance_keeps_held_calls_when_pre_flight_reads_another_runs_doc(analyzer, tmp_path):
    read_doc = "mcp__axiom-graph__axiom_graph_read_doc"
    session = _write_session(
        tmp_path / "preflight.jsonl",
        [
            _command("pev-instance"),
            _tool_use("i-read", "Read", {"file_path": "src/a.py"}),
            _tool_use("i-old", read_doc, {"doc_id": f"proj::docs/pev/cycles/{CYCLE}/manifest"}),
            _tool_use("i-clone", CLONE, {"new_id": f"pev/instances/{INSTANCE}/checkin"}),
            _tool_use("i-edit", "Edit", {"file_path": "src/a.py"}),
        ],
    )

    tools = analyzer.parse_session(str(session), cycle_filter=INSTANCE).orchestrator_tools

    assert [tc.name for tc in tools] == ["Read", read_doc, CLONE, "Edit"]


@pytest.mark.parametrize(
    ("write", "works_on_run"),
    [
        (
            (
                UPDATE,
                {
                    "section_id": f"proj::docs/pev/instances/{INSTANCE}/checkin::notes",
                    "content": f'Last report: "proj::docs/pev/instances/{INSTANCE}/efficiency"',
                },
            ),
            True,
        ),
        (
            (
                "mcp__axiom-graph__axiom_graph_update_doc_meta",
                {"doc_id": f"proj::docs/pev/instances/{INSTANCE}/efficiency", "tags": ["pev-efficiency"]},
            ),
            False,
        ),
    ],
)
def test_efficiency_report_is_judged_by_the_write_target_not_its_content(analyzer, tmp_path, write, works_on_run):
    root = tmp_path / "projects"
    session = _write_session(root / "C--repo" / "s.jsonl", [_tool_use("w-1", *write)])

    assert analyzer.find_sessions_for_run(INSTANCE, projects_dir=root) == ([str(session)] if works_on_run else [])
