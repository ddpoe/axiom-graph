"""The PEV budget hooks keep an agent able to save its state and hand back.

Once a PEV agent's tool count reaches its limit, ``hooks/pev-tool-gate.sh`` denies
everything outside a per-role allowlist. Roles that save state as new sections
must still be able to call ``axiom_graph_add_section`` past that point, and the
deny message must point them at it. The hand-back tool is always allowed and
never counted, and a hook input without an ``agent_id`` is neither counted nor
gated (``hooks/pev-tool-counter.sh``, ``hooks/pev-subagent-stop.sh``).
"""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest
from axiom_annotations import workflow
from pev_hook_helpers import BASH, HOOKS_DIR, bash_has_jq, run_hook

pytestmark = [
    pytest.mark.skipif(not HOOKS_DIR.is_dir(), reason="PEV plugin source not present"),
    pytest.mark.skipif(BASH is None, reason="bash not available"),
    pytest.mark.skipif(not bash_has_jq(), reason="jq not available to bash"),
]

ADD_SECTION = "mcp__axiom-graph__axiom_graph_add_section"
READ_DOC = "mcp__axiom-graph__axiom_graph_read_doc"
ADD_LINK = "mcp__axiom-graph__axiom_graph_add_link"
HAND_BACK = "SubagentHandback"


@pytest.fixture
def spent_agent_id():
    """An agent id whose counter file already holds a count past every role's limit."""
    value = f"tool-gate-test-{uuid.uuid4().hex}"
    counter = f"/tmp/pev-counter-{value}.txt"
    subprocess.run([BASH, "-c", f"echo 500 > {counter}"], check=True)
    yield value
    subprocess.run([BASH, "-c", f"rm -f {counter}"], check=False)


def _payload(agent_type: str, agent_id: str | None, tool_name: str) -> dict:
    payload = {"agent_type": agent_type, "tool_name": tool_name, "tool_input": {}}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _gate(agent_type: str, agent_id: str | None, tool_name: str, tmp_path) -> subprocess.CompletedProcess:
    return run_hook("pev-tool-gate.sh", _payload(agent_type, agent_id, tool_name), tmp_path)


def _count(agent_type: str, agent_id: str | None, tool_name: str, tmp_path) -> subprocess.CompletedProcess:
    return run_hook("pev-tool-counter.sh", _payload(agent_type, agent_id, tool_name), tmp_path)


def _counter_value(path: str) -> str | None:
    """The counter file's content, or None when it does not exist."""
    probe = subprocess.run([BASH, "-c", f'[ -f "{path}" ] && cat "{path}"'], capture_output=True, text=True)
    return probe.stdout.strip() if probe.returncode == 0 else None


def _deny_reason(result: subprocess.CompletedProcess) -> str:
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "agent_type",
    ["pev:pev-architect", "pev:pev-builder", "pev:pev-reviewer", "pev:pev-auditor", "pev:pev-doc-reviewer"],
)
def test_add_section_survives_the_budget_gate(agent_type, spent_agent_id, tmp_path):
    allowed = _gate(agent_type, spent_agent_id, ADD_SECTION, tmp_path)
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout == ""

    denied = _gate(agent_type, spent_agent_id, READ_DOC, tmp_path)
    reason = json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "axiom_graph_add_section for a checkpoint" in reason


@pytest.mark.parametrize("agent_type", ["pev:pev-spike"])
def test_deny_message_omits_add_section_for_roles_without_it(agent_type, spent_agent_id, tmp_path):
    denied = _gate(agent_type, spent_agent_id, ADD_SECTION, tmp_path)
    reason = json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "axiom_graph_update_section for a progress section" in reason
    assert "axiom_graph_add_section for a checkpoint" not in reason


@workflow(
    purpose=(
        "A PEV agent whose budget is spent can always hand back: the gate allows SubagentHandback on the first "
        "attempt and the counter does not count it"
    )
)
@pytest.mark.parametrize("agent_type", ["pev:pev-builder", "pev:pev-auditor", "pev:pev-spike"])
def test_hand_back_is_allowed_past_budget_and_not_counted(agent_type, spent_agent_id, tmp_path):
    counter = f"/tmp/pev-counter-{spent_agent_id}.txt"

    allowed = _gate(agent_type, spent_agent_id, HAND_BACK, tmp_path)
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout == ""

    counted = _count(agent_type, spent_agent_id, HAND_BACK, tmp_path)
    assert counted.returncode == 0, counted.stderr
    assert counted.stdout == ""
    assert _counter_value(counter) == "500"


@pytest.mark.parametrize("tool_name", ["SubagentHandbackX", "mcp__other__SubagentHandback"])
def test_hand_back_match_is_exact(tool_name, spent_agent_id, tmp_path):
    denied = _gate("pev:pev-builder", spent_agent_id, tool_name, tmp_path)
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_deny_message_says_handing_back_is_always_allowed(spent_agent_id, tmp_path):
    reason = _deny_reason(_gate("pev:pev-reviewer", spent_agent_id, READ_DOC, tmp_path))
    assert "hand back with CONTINUING status; handing back is always allowed" in reason
    assert "axiom_graph_update_section for a progress section" in reason


@pytest.fixture
def type_keyed_counter():
    """The agent_type-keyed counter path, seeded past budget; restored afterwards."""
    path = "/tmp/pev-counter-pev-pev-builder.txt"
    before = _counter_value(path)
    subprocess.run([BASH, "-c", f"echo 500 > {path}"], check=True)
    yield path
    if before is None:
        subprocess.run([BASH, "-c", f"rm -f {path}"], check=False)
    else:
        subprocess.run([BASH, "-c", f"echo {before} > {path}"], check=False)


@workflow(
    purpose=(
        "A hook input with an agent_type but no agent_id is neither counted nor gated: the counter writes no "
        "file, the gate denies nothing, and the stop hook touches no agent_type-keyed counter"
    )
)
def test_missing_agent_id_is_neither_counted_nor_gated(type_keyed_counter, tmp_path):
    counted = _count("pev:pev-builder", None, READ_DOC, tmp_path)
    assert counted.returncode == 0, counted.stderr
    assert counted.stdout == ""
    assert _counter_value(type_keyed_counter) == "500"

    gated = _gate("pev:pev-builder", None, READ_DOC, tmp_path)
    assert gated.returncode == 0, gated.stderr
    assert gated.stdout == ""

    stopped = run_hook("pev-subagent-stop.sh", {"agent_type": "pev:pev-builder", "cwd": str(tmp_path)}, tmp_path)
    assert stopped.returncode == 0, stopped.stderr
    assert _counter_value(type_keyed_counter) == "500"


@pytest.mark.parametrize(
    ("agent_type", "allowed"),
    [("pev:pev-auditor", True), ("pev:pev-builder", False), ("pev:pev-reviewer", False)],
)
def test_add_link_past_budget_is_auditor_only(agent_type, allowed, spent_agent_id, tmp_path):
    result = _gate(agent_type, spent_agent_id, ADD_LINK, tmp_path)
    if allowed:
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""
    else:
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_audit_dev_shard_is_not_budget_gated(spent_agent_id, tmp_path):
    counter = f"/tmp/pev-counter-{spent_agent_id}.txt"

    gated = _gate("pev:pev-audit-dev-shard", spent_agent_id, READ_DOC, tmp_path)
    assert gated.returncode == 0, gated.stderr
    assert gated.stdout == ""

    counted = _count("pev:pev-audit-dev-shard", spent_agent_id, READ_DOC, tmp_path)
    assert counted.stdout == ""
    assert _counter_value(counter) == "500"
