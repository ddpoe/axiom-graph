"""The PEV docjson guard watches raw edits of *.docjson documents and nothing else.

PEV_DOCJSON_GUARD picks the response to a detected raw write: ``warn`` (the
default) lets it through with a note, ``block`` denies it, ``off`` is silent.
Raw reads are never denied; the first one in a session gets a one-line hint.
Every test sets ``PEV_DOCJSON_GUARD`` and ``TMPDIR`` explicitly, so the
developer's own environment and the read-hint markers never leak in.
"""

from __future__ import annotations

import json

import pytest
from axiom_annotations import workflow
from pev_hook_helpers import BASH, HOOKS_DIR, bash_has_jq, run_hook

SCRIPT = "pev-docjson-guard.sh"
MODES = ["warn", "block", "off"]
MISSING_JQ = "jq-not-installed-for-this-test"

pytestmark = [
    pytest.mark.skipif(not HOOKS_DIR.is_dir(), reason="PEV plugin source not present"),
    pytest.mark.skipif(BASH is None, reason="bash not available"),
    pytest.mark.skipif(not bash_has_jq(), reason="jq not installed"),
]


def _call(
    tool_name: str,
    tool_input: dict,
    tmp_path,
    mode: str = "block",
    *,
    session_id: str | None = None,
    agent_type: str | None = None,
    jq: str | None = None,
) -> dict | None:
    """Run the guard for one tool call; return the parsed hook output or None."""
    payload = {"tool_name": tool_name, "tool_input": tool_input, "cwd": str(tmp_path)}
    if session_id is not None:
        payload["session_id"] = session_id
    if agent_type is not None:
        payload["agent_type"] = agent_type
    env = {"PEV_DOCJSON_GUARD": mode, "TMPDIR": tmp_path.as_posix()}
    if jq is not None:
        env["PEV_HOOKS_JQ"] = jq
    result = run_hook(SCRIPT, payload, tmp_path, env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def _assert_denied(decision: dict | None, name: str) -> None:
    assert decision is not None
    output = decision["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert output["permissionDecisionReason"].startswith(f"{name} is a DocJSON document")
    assert "axiom_graph_update_section" in output["permissionDecisionReason"]


def _context(decision: dict | None) -> str:
    """Return the additionalContext of a context-only answer (never a permission decision)."""
    assert decision is not None
    output = decision["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in output
    return output["additionalContext"]


def _assert_warned(decision: dict | None, name: str) -> None:
    context = _context(decision)
    assert context.startswith(f"{name} is a DocJSON document")
    assert "axiom_graph_update_section" in context
    assert "axiom_graph_accept_doc_edits" in context
    assert "PEV_DOCJSON_GUARD=block" in context


def _assert_answer(decision: dict | None, mode: str, name: str) -> None:
    if mode == "block":
        _assert_denied(decision, name)
    elif mode == "warn":
        _assert_warned(decision, name)
    else:
        assert decision is None


def _assert_not_denied(decision: dict | None) -> None:
    if decision is not None:
        assert "permissionDecision" not in decision["hookSpecificOutput"]


def _assert_read_hint(decision: dict | None, name: str) -> None:
    context = _context(decision)
    assert context.startswith(f"{name} is a DocJSON document")
    assert "axiom_graph_read_doc" in context
    assert "outline=true" in context
    assert "section_ids" in context
    assert "axiom_graph_search" in context
    assert "scope=docs" in context


# --- raw writes: one detection, three responses ---------------------------


@workflow(
    purpose="Verify every raw write the guard detects through Write, Edit or MultiEdit is passed with a doc-tools note in warn mode, denied in block mode and ignored in off mode",
)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("tool_name", ["Write", "Edit", "MultiEdit"])
def test_file_tools_on_docjson_follow_the_mode(tool_name, mode, tmp_path):
    decision = _call(tool_name, {"file_path": "docs/features/x/prd.docjson"}, tmp_path, mode)

    _assert_answer(decision, mode, "prd.docjson")


def test_default_mode_is_warn(tmp_path):
    payload = {"tool_name": "Write", "tool_input": {"file_path": "docs/a.docjson"}, "cwd": str(tmp_path)}
    env = {"TMPDIR": tmp_path.as_posix(), "PEV_DOCJSON_GUARD": ""}

    result = run_hook(SCRIPT, payload, tmp_path, env)

    _assert_warned(json.loads(result.stdout), "a.docjson")


def test_unknown_mode_is_treated_as_warn(tmp_path):
    decision = _call("Write", {"file_path": "docs/a.docjson"}, tmp_path, "strict")

    _assert_warned(decision, "a.docjson")


@pytest.mark.parametrize("mode", ["warn", "block"])
def test_windows_path_is_reported_as_a_bare_name_in_valid_json(mode, tmp_path):
    decision = _call("Edit", {"file_path": "C:\\repo\\docs\\my plan.docjson"}, tmp_path, mode)

    _assert_answer(decision, mode, "my plan.docjson")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("file_path", ["docs/features/x/prd.json", "notes.docjson.bak", "src/app.py", ""])
def test_file_tools_on_other_files_are_allowed(file_path, mode, tmp_path):
    assert _call("Write", {"file_path": file_path}, tmp_path, mode) is None


BASH_WRITES = [
    ("echo '{}' > docs/a.docjson", "a.docjson"),
    ("cat new.json >> docs/a.docjson", "a.docjson"),
    ("jq . src.json | tee docs/a.docjson", "a.docjson"),
    ("jq . src.json | tee -a docs/a.docjson", "a.docjson"),
    ("sed -i 's/old/new/' docs/a.docjson", "a.docjson"),
    ("sed -i.bak -e 's/old/new/' docs/a.docjson", "a.docjson"),
    ("perl -pi -e 's/old/new/' docs/a.docjson", "a.docjson"),
    ("truncate -s 0 docs/a.docjson", "a.docjson"),
    ("dd if=/dev/null of=docs/a.docjson", "a.docjson"),
    ("cd docs && sed -i 's/a/b/' plan.docjson", "plan.docjson"),
    ("sed --in-place 's/old/new/' docs/a.docjson", "a.docjson"),
    ("printf '{}' >| docs/a.docjson", "a.docjson"),
    ("cp /tmp/x.json docs/a.docjson", "a.docjson"),
    ("mv /tmp/x.json docs/a.docjson", "a.docjson"),
    ("install -m 644 /tmp/x.json docs/a.docjson", "a.docjson"),
    ("touch docs/a.docjson", "a.docjson"),
    ("rm docs/a.docjson", "a.docjson"),
    ("python -c \"open('docs/a.docjson','w').write('{}')\"", "a.docjson"),
    ("python3 -c \"open('docs/a.docjson','w')\"", "a.docjson"),
    ("node -e \"require('fs').writeFileSync('docs/a.docjson','{}')\"", "a.docjson"),
    ('cat > "docs/my plan.docjson"', "my plan.docjson"),
    ("cat > 'docs/my plan.docjson'", "my plan.docjson"),
    ("sed -i 's/a/b/' 'docs/my plan.docjson'", "my plan.docjson"),
    ("echo '{}' > docs/a.docjson; ls docs", "a.docjson"),
    ("sed -i 's/a/b/' docs/a.docjson && git add -A", "a.docjson"),
    ("rm docs/a.docjson|| true", "a.docjson"),
]


@workflow(
    purpose="Verify every Bash write shape the guard detects on a .docjson document is passed with a doc-tools note in warn mode, denied in block mode and ignored in off mode",
)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("command", "name"), BASH_WRITES)
def test_bash_writes_to_docjson_follow_the_mode(command, name, mode, tmp_path):
    decision = _call("Bash", {"command": command}, tmp_path, mode, session_id="writes")

    _assert_answer(decision, mode, name)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "command",
    [
        "git mv docs/a.json docs/a.docjson",
        "cd docs && git mv a.json a.docjson",
        "echo hi > docs/a.json",
        "sed -i 's/a/b/' docs/a.json",
        # `.docjson` inside a longer name is not a document: a dotted module
        # path, or a file with a further extension.
        "sed -i 's/from axiom_graph.docjson.api import x/from y import x/' tests/test_a.py",
        "perl -pi -e 's/axiom_graph.docjson.render_agent/z/' src/mod.py",
        "python -c \"import axiom_graph.docjson.api as a; open('out.txt','w').write(a.__name__)\"",
        "cp docs/a.json notes.docjson.bak",
        "echo hi > notes.docjson.bak",
        "rm notes.docjson.bak",
    ],
)
def test_git_mv_and_non_docjson_writes_are_silent(command, mode, tmp_path):
    """`git mv` is the sanctioned rename: neither a write nor a read."""
    assert _call("Bash", {"command": command}, tmp_path, mode) is None


def test_notebook_on_other_files_is_allowed(tmp_path):
    assert _call("NotebookEdit", {"notebook_path": "analysis.ipynb"}, tmp_path) is None


@pytest.mark.parametrize("mode", ["warn", "block"])
def test_pev_agents_are_guarded_too(mode, tmp_path):
    decision = _call("Write", {"file_path": "docs/a.docjson"}, tmp_path, mode, agent_type="pev:pev-builder")

    _assert_answer(decision, mode, "a.docjson")


@pytest.mark.parametrize("mode", ["warn", "block"])
def test_answer_is_valid_json_when_the_path_holds_a_control_character(mode, tmp_path):
    decision = _call("Edit", {"file_path": "docs/a\tb.docjson"}, tmp_path, mode)

    assert decision is not None
    if mode == "block":
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        _context(decision)


# --- without jq: the same write checks over the raw payload --------------

NO_JQ_WRITES = [
    ("Write", {"file_path": "docs/a.docjson"}),
    ("Edit", {"file_path": "docs/a.docjson"}),
    ("Bash", {"command": "echo '{}' > docs/a.docjson"}),
    ("Bash", {"command": "cp /tmp/x.json docs/a.docjson"}),
    ("Bash", {"command": 'cat > "docs/my plan.docjson"'}),
    ("Bash", {"command": "python -c \"open('docs/a.docjson','w').write('{}')\""}),
]


@workflow(
    purpose="Verify that without jq the guard still detects raw .docjson writes from the raw payload and answers per the mode: a note in warn, a deny in block, nothing in off",
)
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("tool_name", "tool_input"), NO_JQ_WRITES)
def test_main_session_without_jq_still_detects_docjson_writes(tool_name, tool_input, mode, tmp_path):
    decision = _call(tool_name, tool_input, tmp_path, mode, jq=MISSING_JQ)

    if mode == "off":
        assert decision is None
        return
    assert decision is not None
    output = decision["hookSpecificOutput"]
    if mode == "block":
        assert output["permissionDecision"] == "deny"
        message = output["permissionDecisionReason"]
    else:
        message = _context(decision)
        assert "axiom_graph_accept_doc_edits" in message
    assert ".docjson" in message
    assert "jq" in message


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Write", {"file_path": "docs/a.json"}),
        ("Write", {"file_path": "src/app.py"}),
        ("Bash", {"command": "echo hi > notes.txt"}),
        ("Bash", {"command": "cat docs/a.docjson"}),
        ("Bash", {"command": "grep -n title docs/a.docjson > /tmp/hits.txt"}),
        ("Bash", {"command": "python -c \"import json; print(json.load(open('docs/a.docjson')))\""}),
        ("Read", {"file_path": "docs/a.docjson"}),
        ("Grep", {"pattern": "title", "glob": "*.docjson"}),
        ("Write", {"file_path": "notes.md", "content": "see docs/a.docjson"}),
        ("Bash", {"command": "sed -i 's/axiom_graph.docjson.api/x/' tests/test_a.py"}),
        ("Bash", {"command": "echo hi > notes.docjson.bak"}),
    ],
)
def test_main_session_without_jq_is_left_alone_for_other_files_and_reads(tool_name, tool_input, mode, tmp_path):
    """Without jq, reads and other files pass silently: the read hint needs jq."""
    assert _call(tool_name, tool_input, tmp_path, mode, session_id="s1", jq=MISSING_JQ) is None


def test_pev_agent_without_jq_still_gets_the_install_hint(tmp_path):
    decision = _call(
        "Write", {"file_path": "src/app.py"}, tmp_path, "warn", agent_type="pev:pev-builder", jq=MISSING_JQ
    )

    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "jq" in decision["hookSpecificOutput"]["permissionDecisionReason"]


# --- raw reads: never denied, hinted once per session ---------------------

READS = [
    ("Read", {"file_path": "docs/a.docjson"}),
    ("Read", {"file_path": "src/app.py"}),
    ("Grep", {"pattern": "title", "path": "docs/a.docjson"}),
    ("Grep", {"pattern": "title", "glob": "**/*.docjson"}),
    ("Grep", {"pattern": "title", "path": "src"}),
    ("Bash", {"command": "cat docs/a.docjson"}),
    ("Bash", {"command": "jq '.sections[0]' docs/a.docjson > /tmp/section.json"}),
    ("Bash", {"command": "python -c \"import json; print(json.load(open('docs/a.docjson'))['title'])\""}),
    ("Bash", {"command": "poetry run python - <<'EOF'\nimport json\nprint(json.load(open('docs/a.docjson')))\nEOF"}),
]

# The remaining read-only shapes, checked once each in block mode (the mode
# that denies writes) and hinted as reads.
MORE_BASH_READS = [
    "grep -n title docs/a.docjson",
    "sed -n '1,20p' docs/a.docjson",
    "git diff -- docs/a.docjson",
    "python3 -c \"print(open('docs/a.docjson', encoding='utf-8').read()[:200])\"",
    "node -e \"console.log(require('fs').readFileSync('docs/a.docjson','utf8'))\"",
]


@pytest.mark.parametrize("command", MORE_BASH_READS)
def test_more_read_only_bash_shapes_get_the_hint_not_a_deny(command, tmp_path):
    _assert_read_hint(_call("Bash", {"command": command}, tmp_path, "block"), "a.docjson")


@pytest.mark.parametrize("mode", ["warn", "block"])
@pytest.mark.parametrize(
    ("agent_type", "jq"),
    [(None, None), ("pev:pev-builder", None), (None, MISSING_JQ), ("pev:pev-builder", MISSING_JQ)],
)
@pytest.mark.parametrize(("tool_name", "tool_input"), READS)
def test_reads_are_never_denied(tool_name, tool_input, agent_type, jq, mode, tmp_path):
    _assert_not_denied(_call(tool_name, tool_input, tmp_path, mode, agent_type=agent_type, jq=jq))


@workflow(
    purpose="Verify the first raw read of a .docjson in a session gets one hint line pointing at read_doc and search, later reads in that session get nothing, and a new session gets the hint again",
)
def test_read_hint_fires_once_per_session(tmp_path):
    first = _call("Read", {"file_path": "docs/a.docjson"}, tmp_path, "warn", session_id="session-1")
    _assert_read_hint(first, "a.docjson")

    assert _call("Read", {"file_path": "docs/b.docjson"}, tmp_path, "warn", session_id="session-1") is None
    assert _call("Grep", {"pattern": "x", "glob": "*.docjson"}, tmp_path, "warn", session_id="session-1") is None
    assert _call("Bash", {"command": "cat docs/a.docjson"}, tmp_path, "warn", session_id="session-1") is None

    again = _call("Bash", {"command": "cat docs/a.docjson"}, tmp_path, "warn", session_id="session-2")
    _assert_read_hint(again, "a.docjson")


def test_read_hint_without_a_session_id_fires_every_time(tmp_path):
    for _ in range(2):
        _assert_read_hint(_call("Read", {"file_path": "docs/a.docjson"}, tmp_path, "warn"), "a.docjson")


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "name"),
    [
        ("Grep", {"pattern": "x", "path": "docs/a.docjson"}, "a.docjson"),
        ("Grep", {"pattern": "x", "glob": "*.docjson"}, "*.docjson"),
        ("Bash", {"command": "sed -n '1,20p' 'docs/my plan.docjson'"}, "my plan.docjson"),
    ],
)
def test_read_hint_covers_grep_and_read_only_bash(tool_name, tool_input, name, tmp_path):
    _assert_read_hint(_call(tool_name, tool_input, tmp_path, "block", session_id="s"), name)


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Read", {"file_path": "docs/a.json"}),
        ("Grep", {"pattern": "x", "path": "src"}),
        ("Bash", {"command": "git mv docs/a.json docs/a.docjson"}),
        ("Bash", {"command": "ls docs"}),
    ],
)
def test_no_read_hint_for_other_files_or_git_mv(tool_name, tool_input, tmp_path):
    assert _call(tool_name, tool_input, tmp_path, "warn", session_id="s") is None


def test_off_mode_gives_no_read_hint(tmp_path):
    assert _call("Read", {"file_path": "docs/a.docjson"}, tmp_path, "off", session_id="s") is None


def test_session_id_is_sanitised_into_a_marker_inside_tmpdir(tmp_path):
    hint_dir = tmp_path / "hints"
    hint_dir.mkdir()
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": "docs/a.docjson"},
        "cwd": str(tmp_path),
        "session_id": "../escape me/1",
    }
    env = {"PEV_DOCJSON_GUARD": "warn", "TMPDIR": hint_dir.as_posix()}

    result = run_hook(SCRIPT, payload, tmp_path, env)

    _assert_read_hint(json.loads(result.stdout), "a.docjson")
    assert [p.name for p in hint_dir.iterdir()] == ["pev-docjson-read-hint-escapeme1"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hints"]


COMMIT_MESSAGE_MENTIONS = [
    'git commit -m "rename docs/a.docjson to b.docjson"',
    "git commit -m 'write > docs/a.docjson'",
    'git -C wt commit -am "tee docs/a.docjson"',
    'git commit --message="rm docs/a.docjson"',
    "git commit -F - <<'EOF'\nmove docs/a.docjson\nand > docs/b.docjson\nEOF",
]


@pytest.mark.parametrize("mode", ["warn", "block"])
@pytest.mark.parametrize("command", COMMIT_MESSAGE_MENTIONS)
def test_docjson_named_in_a_commit_message_is_neither_read_nor_write(command, mode, tmp_path):
    assert _call("Bash", {"command": command}, tmp_path, mode, session_id="commit-msg") is None


@pytest.mark.parametrize("mode", ["warn", "block"])
def test_a_real_write_beside_a_commit_message_is_still_caught(mode, tmp_path):
    command = 'git commit -m "update a.docjson" && echo {} > docs/a.docjson'
    _assert_answer(_call("Bash", {"command": command}, tmp_path, mode), mode, "a.docjson")


def test_without_jq_only_the_command_is_judged_not_the_description(tmp_path):
    tool_input = {"command": "git status", "description": "then write > docs/a.docjson"}
    assert _call("Bash", tool_input, tmp_path, "block", jq=MISSING_JQ) is None
    tool_input = {"command": "echo {} > docs/a.docjson", "description": "harmless"}
    decision = _call("Bash", tool_input, tmp_path, "block", jq=MISSING_JQ)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
