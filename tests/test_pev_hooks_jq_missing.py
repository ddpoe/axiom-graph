"""PEV hooks fail closed for PEV agents when jq is missing, and stay silent otherwise.

Every wired PEV hook reads its JSON input with jq. These tests run each hook
script under bash with ``PEV_HOOKS_JQ`` pointing at a binary that does not
exist, which is how the shared preamble (``hooks/lib/pev-hook-common.sh``)
is told jq is unavailable. A real PATH without jq can't be built portably:
on Linux jq usually sits in ``/usr/bin`` next to bash and coreutils.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from pev_hook_helpers import BASH, HOOKS_DIR, run_hook

# (script, hook event it is wired to)
WIRED_HOOKS = [
    ("pev-worktree-scope.sh", "pretool"),
    ("pev-bash-scope.sh", "pretool"),
    ("pev-worktree-rm.sh", "pretool"),
    ("pev-docjson-guard.sh", "pretool"),
    ("pev-doc-scope.sh", "pretool"),
    ("pev-doc-scope-md.sh", "pretool"),
    ("pev-axiom-graph-scope.sh", "pretool"),
    ("pev-tool-gate.sh", "pretool"),
    ("pev-tool-counter.sh", "posttool"),
    ("pev-subagent-stop.sh", "stop"),
]

MISSING_JQ = "jq-not-installed-for-this-test"


pytestmark = [
    pytest.mark.skipif(not HOOKS_DIR.is_dir(), reason="PEV plugin source not present"),
    pytest.mark.skipif(BASH is None, reason="bash not available"),
]


def _run_hook(script: str, payload: dict, tmp_path: Path, jq: str) -> subprocess.CompletedProcess:
    # The docjson guard's mode is pinned so a developer's own setting can't leak in.
    return run_hook(script, payload, tmp_path, {"PEV_HOOKS_JQ": jq, "PEV_DOCJSON_GUARD": "warn"})


@pytest.fixture
def agent_id():
    """A unique agent id; removes the counter file pev-tool-counter.sh may write for it."""
    value = f"jq-missing-test-{uuid.uuid4().hex}"
    yield value
    subprocess.run([BASH, "-c", f"rm -f /tmp/pev-counter-{value}.txt"], check=False)


def _payload(agent_type: str, tmp_path: Path, agent_id: str = "jq-missing-test") -> dict:
    return {
        "agent_type": agent_type,
        "agent_id": agent_id,
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "out.txt"), "project_root": str(tmp_path)},
        "cwd": str(tmp_path),
    }


@pytest.mark.parametrize(("script", "event"), WIRED_HOOKS)
def test_pev_agent_without_jq_gets_install_hint(script, event, tmp_path):
    result = _run_hook(script, _payload("pev:pev-builder", tmp_path), tmp_path, MISSING_JQ)

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    if event == "pretool":
        decision = output["hookSpecificOutput"]
        assert decision["hookEventName"] == "PreToolUse"
        assert decision["permissionDecision"] == "deny"
        message = decision["permissionDecisionReason"]
    elif event == "posttool":
        context = output["hookSpecificOutput"]
        assert context["hookEventName"] == "PostToolUse"
        message = context["additionalContext"]
    else:
        message = output["systemMessage"]
    assert "jq" in message
    assert "winget install jqlang.jq" in message


@pytest.mark.parametrize(("script", "event"), WIRED_HOOKS)
def test_non_pev_caller_without_jq_is_left_alone(script, event, tmp_path):
    result = _run_hook(script, _payload("general-purpose", tmp_path), tmp_path, MISSING_JQ)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(("script", "event"), WIRED_HOOKS)
def test_main_session_without_jq_is_left_alone(script, event, tmp_path):
    payload = _payload("general-purpose", tmp_path)
    del payload["agent_type"]

    result = _run_hook(script, payload, tmp_path, MISSING_JQ)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(("script", "event"), WIRED_HOOKS)
def test_pev_marker_inside_tool_content_is_not_a_pev_agent(script, event, tmp_path):
    """A main session writing text that contains the PEV agent marker is not denied."""
    payload = _payload("general-purpose", tmp_path)
    del payload["agent_type"]
    payload["tool_input"]["content"] = '{"agent_type":"pev:pev-builder"}'

    result = _run_hook(script, payload, tmp_path, MISSING_JQ)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("mode", ["warn", "block", "off"])
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Read", {"file_path": "docs/a.docjson"}),
        ("Read", {"file_path": "src/app.py"}),
        ("Grep", {"pattern": "title", "glob": "*.docjson"}),
        ("Grep", {"pattern": "title", "path": "src"}),
        ("Bash", {"command": "cat docs/a.docjson"}),
        ("Bash", {"command": "git status"}),
    ],
)
def test_docjson_guard_never_denies_a_pev_agents_read_without_jq(tool_name, tool_input, mode, tmp_path):
    """The guard's matcher includes Bash|Read|Grep, so it must not hand reads the fail-closed install hint."""
    payload = {
        "agent_type": "pev:pev-builder",
        "agent_id": "jq-missing-test",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(tmp_path),
        "session_id": "jq-missing-read",
    }
    env = {"PEV_HOOKS_JQ": MISSING_JQ, "PEV_DOCJSON_GUARD": mode, "TMPDIR": tmp_path.as_posix()}

    result = run_hook("pev-docjson-guard.sh", payload, tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(("script", "event"), WIRED_HOOKS)
def test_pev_agent_with_jq_outside_a_cycle_is_allowed(script, event, tmp_path, agent_id):
    """With jq present and no .pev-state.json, the hooks allow the call silently."""
    probe = subprocess.run([BASH, "-c", "command -v jq"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("jq not installed")

    result = _run_hook(script, _payload("pev:pev-builder", tmp_path, agent_id), tmp_path, "jq")

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
