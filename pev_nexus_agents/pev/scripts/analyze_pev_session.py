#!/usr/bin/env python3
"""Analyze Claude Code session logs for PEV cycle efficiency metrics.

Usage:
    # Analyze a specific session file
    python "${CLAUDE_PLUGIN_ROOT}/scripts/analyze_pev_session.py" <session.jsonl>

    # Filter to a specific PEV cycle
    python "${CLAUDE_PLUGIN_ROOT}/scripts/analyze_pev_session.py" <session.jsonl> --cycle pev-2026-03-24-js-ts-scanner

    # Search all sessions for a cycle
    python "${CLAUDE_PLUGIN_ROOT}/scripts/analyze_pev_session.py" --find-cycle pev-2026-03-24-js-ts-scanner

    # Show full tool sequence (not just summary)
    python "${CLAUDE_PLUGIN_ROOT}/scripts/analyze_pev_session.py" <session.jsonl> --verbose

    # Stage the report as DocJSON in the system temp folder, then write it into
    # the cycle's directory (docs/pev/cycles/<cycle-id>/efficiency) with
    # axiom_graph_write_doc
    python "${CLAUDE_PLUGIN_ROOT}/scripts/analyze_pev_session.py" <session.jsonl> --docjson

Transcript format (last checked against Claude Code 2.1.287):
    This script reads Claude Code's private session-log format, which changes
    without notice. ``TESTED_CLAUDE_CODE_VERSION`` records the newest version it
    was checked against; analysing a session written by a newer version prints a
    warning, so a silent zero-tool-call report points at a format change.

    - Session log: ``~/.claude/projects/<cwd-slug>/<session-id>.jsonl``. The
      file is relocated when the session changes directory (``relocated`` /
      ``worktree-state`` records), so a session started in the main checkout
      can end up under a worktree's folder and the reverse; the slug's drive
      letter case also varies. ``--find-cycle`` therefore searches every
      project dir and reads each log in full, and keeps a session only when
      the records that dispatch the run carry a ``cwd`` inside this project
      (``--project-root``; its ``.claude/worktrees/`` included), so another
      project that reused the run id is left out.
    - Cycle attribution: a session can mention many cycles (a sibling cycle's
      prompt, a doc read). Each tool call that names a ``pev-YYYY-MM-DD-*`` id
      moves the session's current cycle to the id it names most; calls that
      name none stay with the current cycle. ``--cycle`` / ``--find-cycle``
      keep only the calls and dispatches attributed to the requested cycle,
      and ``--find-cycle`` matches only sessions that dispatch a PEV agent
      (``pev-*``) for it.
    - Subagent tool calls: each subagent has its own transcript at
      ``<session-id>/subagents/agent-<agentId>.jsonl`` plus ``.meta.json``
      (``agentType``, ``toolUseId`` = the dispatching Agent call's id).
      Older sessions inlined them as ``progress`` / ``agent_progress`` records
      in the main log; both are read.
    - Subagent totals: background agents report
      ``<usage><subagent_tokens>N</subagent_tokens><tool_uses>N</tool_uses>
      <duration_ms>N</duration_ms></usage>`` inside a ``<task-notification>``
      keyed by ``<tool-use-id>``; older sessions put ``total_tokens: N`` lines in
      the Agent tool_result. Both are read.
    - Agent types are plugin-namespaced (``pev:pev-builder``); they are
      normalised to the bare ``pev-builder`` form.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

#: Newest Claude Code version whose session-log layout this script was checked
#: against (see "Transcript format" in the module docstring).  Bump it after
#: re-checking the parser against a newer version's logs.
TESTED_CLAUDE_CODE_VERSION = "2.1.287"

#: A PEV cycle id: ``pev-YYYY-MM-DD-<slug>``.  The slug is alphanumerics and
#: hyphens and ends on an alphanumeric, so a trailing sentence period or hyphen
#: never joins the id (it would leak into the efficiency filename).
CYCLE_ID_PATTERN = re.compile(r"pev-\d{4}-\d{2}-\d{2}-[A-Za-z0-9-]*[A-Za-z0-9]")

#: Any PEV run id: a cycle (``pev-YYYY-MM-DD-<slug>``), an audit
#: (``pev-audit-{dev-docs|consumer-docs|annotations}-YYYY-MM-DD-<slug>``) or an
#: instance (``pev-instance-YYYY-MM-DD-<slug>``).  Same slug rule as above.
RUN_ID_PATTERN = re.compile(
    r"pev-(?:audit-(?:dev-docs|consumer-docs|annotations)-|instance-)?\d{4}-\d{2}-\d{2}-[A-Za-z0-9-]*[A-Za-z0-9]"
)

#: Where each kind of run keeps its directory in the docs tree.
RUN_DOC_FOLDERS = {"cycle": "pev/cycles", "audit": "pev/audits", "instance": "pev/instances"}

#: The user's ``/pev-instance`` command in a session log (plain or plugin-namespaced).
_INSTANCE_COMMAND = re.compile(r"<command-name>/?(?:pev:)?pev-instance</command-name>")
#: The same skill started with the ``Skill`` tool.
_INSTANCE_SKILL = re.compile(r'"skill":\s*"(?:pev:)?pev-instance"')

# Tool classification for efficiency analysis
EXPLORATION_TOOLS = {
    "Grep",
    "Glob",
    "Read",
    "Bash",
    "mcp__axiom-graph__axiom_graph_search",
    "mcp__axiom-graph__axiom_graph_source",
    "mcp__axiom-graph__axiom_graph_read_doc",
    "mcp__axiom-graph__axiom_graph_list",
    "mcp__axiom-graph__axiom_graph_graph",
    "mcp__axiom-graph__axiom_graph_render",
    "mcp__axiom-graph__axiom_graph_report",
    "mcp__axiom-graph__axiom_graph_sql",
    "mcp__axiom-graph__axiom_graph_diff",
    "mcp__axiom-graph__axiom_graph_history",
    "mcp__axiom-graph__axiom_graph_list_undocumented",
    "mcp__axiom-graph__axiom_graph_list_reference_points",
    "ToolSearch",
}

IMPLEMENTATION_TOOLS = {
    "Edit",
    "Write",
    "NotebookEdit",
    "mcp__axiom-graph__axiom_graph_update_section",
    "mcp__axiom-graph__axiom_graph_patch_section",
    "mcp__axiom-graph__axiom_graph_write_doc",
    "mcp__axiom-graph__axiom_graph_add_section",
    "mcp__axiom-graph__axiom_graph_add_link",
    "mcp__axiom-graph__axiom_graph_delete_link",
    "mcp__axiom-graph__axiom_graph_update_doc_meta",
    "mcp__axiom-graph__axiom_graph_mark_clean",
    "mcp__axiom-graph__axiom_graph_reverify",
    "mcp__axiom-graph__axiom_graph_accept_doc_edits",
    "mcp__axiom-graph__axiom_graph_purge_node",
}

INFRA_TOOLS = {
    "mcp__axiom-graph__axiom_graph_build",
    "mcp__axiom-graph__axiom_graph_check",
    "mcp__axiom-graph__axiom_graph_checkout",
}

# Agent types that are read-only reviewers (handoff gap is expected to be 100%)
REVIEWER_AGENT_TYPES = {
    "superpowers:code-reviewer",
    "pev-reviewer",
}
ARCHITECT_AGENT_TYPES = {"pev-architect"}
AUDITOR_AGENT_TYPES = {"pev-auditor"}
BUILDER_AGENT_TYPES = {"pev-builder"}
DOC_REVIEWER_AGENT_TYPES = {"pev-doc-reviewer"}
#: The handoff gap (calls before the first edit/write) measures how much a
#: Builder had to re-explore.  Read-heavy roles (Architect, Auditor, Doc
#: Reviewer) read before they write by design, so they are not flagged.
GAP_FLAGGED_AGENT_TYPES = BUILDER_AGENT_TYPES


# ── Data structures ──────────────────────────────────────────────────────────


@dataclass
class ToolCall:
    index: int
    name: str
    input_summary: str
    category: str  # exploration, implementation, infra, test, waste


@dataclass
class SubagentRun:
    agent_type: str
    description: str
    tool_use_id: str
    agent_id: str | None = None
    prompt_preview: str = ""
    prompt_length: int = 0
    has_context_bundle: bool = False  # True if dispatch inlined pitch + source
    tool_calls: list[ToolCall] = field(default_factory=list)
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    # Fallback stats from <usage> tags when progress records are unavailable
    usage_tool_count: int | None = None
    usage_total_tokens: int | None = None
    usage_duration_ms: int | None = None
    # Context size per assistant turn (input + cache-creation + cache-read tokens)
    turn_contexts: list[int] = field(default_factory=list)
    # Builder task boundaries: ("inc-N.task-M", context on the turn that wrote the manifest)
    task_boundaries: list[tuple[str, int]] = field(default_factory=list)
    seen_message_ids: set[str] = field(default_factory=set, repr=False)


@dataclass
class SessionAnalysis:
    file_path: str
    cycle_id: str | None = None
    orchestrator_tools: list[ToolCall] = field(default_factory=list)
    subagents: list[SubagentRun] = field(default_factory=list)
    total_records: int = 0
    claude_code_version: str | None = None
    # The main session's own turns attributed to the run (the orchestrator of a
    # cycle or audit; the agent doing the work in an instance)
    main_run: SubagentRun = field(
        default_factory=lambda: SubagentRun(agent_type="main-session", description="main session", tool_use_id="")
    )


# ── Parsing ──────────────────────────────────────────────────────────────────


def shorten_path(path: str) -> str:
    """Shorten a file path for readability: relative to its worktree or the cwd."""
    norm = path.replace("\\", "/")
    marker = "/.claude/worktrees/"
    if marker in norm:
        # .../.claude/worktrees/<name>/rest -> rest
        rest = norm.split(marker, 1)[1]
        return rest.split("/", 1)[1] if "/" in rest else rest
    cwd = str(Path.cwd()).replace("\\", "/").rstrip("/") + "/"
    if norm.lower().startswith(cwd.lower()):
        return norm[len(cwd) :]
    for prefix in (os.path.expanduser("~").replace("\\", "/"), "C:/Users/", "/home/"):
        if norm.startswith(prefix):
            return "/".join(norm.split("/")[-4:])
    return path


def summarize_tool_input(name: str, inp: dict) -> str:
    """Create a short summary of a tool call's input."""
    if not isinstance(inp, dict):
        return ""

    if name in ("Read", "Edit", "Write"):
        return shorten_path(inp.get("file_path", "?"))
    elif name == "Grep":
        return f"pattern={inp.get('pattern', '?')[:40]}"
    elif name == "Glob":
        return f"{inp.get('pattern', '?')}"
    elif name == "Bash":
        cmd = inp.get("command", "?")
        # Detect cd-only commands
        if re.match(r"^cd\s+", cmd.strip()):
            return "cd ..."
        return cmd[:80]
    elif name == "mcp__axiom-graph__axiom_graph_read_doc":
        mode = " (outline)" if inp.get("outline") else ""
        if inp.get("section_ids"):
            ids = inp["section_ids"]
            return f"{ids[0]}" + (f" +{len(ids) - 1} more" if len(ids) > 1 else "") + mode
        if inp.get("doc_ids"):
            ids = inp["doc_ids"]
            return f"{ids[0]} +{len(ids) - 1} more docs{mode}"
        doc = inp.get("doc_id", "?")
        sec = inp.get("section", "")
        return f"{doc}" + (f" [{sec}]" if sec else "") + mode
    elif name == "mcp__axiom-graph__axiom_graph_search":
        return inp.get("query", "?")[:50]
    elif name == "mcp__axiom-graph__axiom_graph_source":
        return inp.get("node_id", "?")
    elif name == "mcp__axiom-graph__axiom_graph_graph":
        return inp.get("node_id", "?")
    elif name in ("mcp__axiom-graph__axiom_graph_update_section", "mcp__axiom-graph__axiom_graph_patch_section"):
        return inp.get("section_id", "?")
    elif name == "Agent":
        return inp.get("description", "?")
    elif name == "ToolSearch":
        return inp.get("query", "?")[:40]
    else:
        return str(list(inp.keys()))[:50]


def classify_tool(name: str, inp: dict) -> str:
    """Classify a tool call into a category."""
    if name == "Bash":
        cmd = inp.get("command", "").strip()
        # cd-only is waste
        if re.match(r"^cd\s+", cmd) and "&&" not in cmd:
            return "waste"
        # Test runs
        if any(kw in cmd for kw in ("pytest", "poetry run pytest", "python -m pytest", "npm test")):
            return "test"
        # Git operations
        if cmd.startswith("git "):
            return "infra"
        # ls/find used for exploration
        if any(cmd.startswith(x) for x in ("ls ", "find ", "cat ", "head ")):
            return "exploration"
        # Poetry/pip install
        if any(kw in cmd for kw in ("poetry install", "pip install", "npm install")):
            return "infra"
        return "exploration"

    if name in IMPLEMENTATION_TOOLS:
        return "implementation"
    if name in INFRA_TOOLS:
        return "infra"
    if name in EXPLORATION_TOOLS:
        return "exploration"
    if name == "Agent":
        return "dispatch"
    return "other"


def extract_tool_calls_from_content(content: list) -> list[dict]:
    """Extract tool_use blocks from a message content list."""
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version)[:3])


def _session_version(records: list[dict]) -> str | None:
    """Return the most common Claude Code ``version`` recorded in *records*."""
    counts = Counter(r["version"] for r in records if isinstance(r.get("version"), str))
    return counts.most_common(1)[0][0] if counts else None


def _warn_if_untested(version: str | None, file_path: str) -> None:
    if version and _version_tuple(version) > _version_tuple(TESTED_CLAUDE_CODE_VERSION):
        print(
            f"WARNING: {Path(file_path).name} was written by Claude Code {version}; this script was last "
            f"checked against {TESTED_CLAUDE_CODE_VERSION}. If subagent tool calls come out empty, the "
            "session-log format has changed — see 'Transcript format' in the script docstring.",
            file=sys.stderr,
        )


def _normalize_agent_type(agent_type: str) -> str:
    """``pev:pev-builder`` -> ``pev-builder`` (plugin-namespaced agent types)."""
    return agent_type.split(":", 1)[1] if agent_type.startswith("pev:") else agent_type


def run_kind(run_id: str) -> str:
    """Return ``"audit"``, ``"instance"`` or ``"cycle"`` for a PEV run id."""
    if run_id.startswith("pev-audit-"):
        return "audit"
    if run_id.startswith("pev-instance-"):
        return "instance"
    return "cycle"


def named_cycle(text: str) -> str | None:
    """Return the PEV run id (cycle, audit or instance) *text* names most often, or ``None``.

    A dispatch prompt names its own run several times (manifest path, section
    ids) and a related run once or twice, so the most frequent id is the one
    the call belongs to.  A tie goes to the id named first.

    Args:
        text: A dispatch prompt or a serialised tool input.

    Returns:
        The run id, or ``None`` when *text* names none.
    """
    ids = RUN_ID_PATTERN.findall(text)
    if not ids:
        return None
    counts = Counter(ids)
    best = max(counts.values())
    return next(i for i in ids if counts[i] == best)


def _tool_input_text(inp: object) -> str:
    """Serialise a tool input for cycle-id matching."""
    return inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False)


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


#: A Builder's task manifest section, in either id shape: the dot-path
#: ``inc-N.task-M.manifest`` (cycle directories, and ``builder.inc-N...`` in
#: single-file cycles) or a flat slug ``inc-N-task-M-manifest``.
_MANIFEST_SECTION = re.compile(r"(?:^|[:.])(inc-\d+)[.-](task-\d+)[.-]manifest$")

_SECTION_WRITE_TOOLS = {
    "mcp__axiom-graph__axiom_graph_add_section",
    "mcp__axiom-graph__axiom_graph_update_section",
    "mcp__axiom-graph__axiom_graph_patch_section",
}


def _manifest_writes(name: str, inp: object) -> list[str]:
    """Return the ``inc-N.task-M`` label of each task manifest a section write targets.

    Args:
        name: The tool name.
        inp: The tool input: a single write (``section_id``, plus ``parent_id``
            for ``add_section``) or a batch under ``sections`` / ``edits``.

    Returns:
        One label per manifest section written, in input order.
    """
    if name not in _SECTION_WRITE_TOOLS or not isinstance(inp, dict):
        return []
    items = inp.get("sections") or inp.get("edits") or [inp]
    labels = []
    for item in items:
        if not isinstance(item, dict):
            continue
        section = str(item.get("section_id") or "")
        parent = item.get("parent_id")
        match = _MANIFEST_SECTION.search(f"{parent}.{section}" if parent else section)
        if match:
            labels.append(f"{match.group(1)}.{match.group(2)}")
    return labels


def _context_size(message: dict) -> int | None:
    """Context the model saw on one turn: input plus cache-creation and cache-read tokens."""
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(usage.get(k) or 0) for k in keys)


def _observe_turn(run: SubagentRun, message: dict, blocks: list[dict] | None = None) -> None:
    """Record one assistant message's context size and any task-boundary writes on *run*.

    Claude Code splits one API response over several records that share the
    message id and its usage, so each id is counted as one turn.

    Args:
        run: The run the turn belongs to.
        message: The assistant message (one record's ``message``).
        blocks: The tool calls whose manifest writes to record; ``None`` means
            every tool call in *message*.  The main session passes the one
            call it keeps, so a record holding several calls adds each write
            once.
    """
    context = _context_size(message)
    if context is None:
        return
    msg_id = message.get("id")
    if msg_id not in run.seen_message_ids:
        if msg_id:
            run.seen_message_ids.add(msg_id)
        run.turn_contexts.append(context)
    if blocks is None:
        blocks = extract_tool_calls_from_content(message.get("content", []))
    for block in blocks:
        for label in _manifest_writes(block.get("name", ""), block.get("input", {})):
            run.task_boundaries.append((label, context))


def context_metrics(run: SubagentRun) -> dict:
    """Per-run context metrics, shared by every efficiency report.

    Args:
        run: A parsed subagent run.

    Returns:
        ``turns`` (assistant turns with usage), ``first_context`` and
        ``peak_context`` (tokens, ``None`` when no turn carried usage) and
        ``task_boundaries`` (``(inc-N.task-M, tokens)`` per Builder manifest
        write, in order).
    """
    contexts = run.turn_contexts
    return {
        "turns": len(contexts),
        "first_context": contexts[0] if contexts else None,
        "peak_context": max(contexts) if contexts else None,
        "task_boundaries": list(run.task_boundaries),
    }


def _kilo(tokens: int) -> str:
    return f"{tokens / 1000:.1f}k"


def context_summary_lines(run: SubagentRun) -> tuple[str, str | None]:
    """Format a run's context for the one-screen summary.

    Returns:
        The suffix for the run's line (empty when no turn carried usage) and,
        for a run with task boundaries, a second line listing them.
    """
    m = context_metrics(run)
    if not m["turns"]:
        return "", None
    suffix = f"  ctx {_kilo(m['first_context'])}->{_kilo(m['peak_context'])}, {m['turns']} turns"
    bounds = m["task_boundaries"]
    extra = "task boundaries: " + ", ".join(f"{label} {_kilo(t)}" for label, t in bounds) if bounds else None
    return suffix, extra


def dedupe_runs(analyses: list[SessionAnalysis]) -> list[SessionAnalysis]:
    """Count each subagent once across session logs, the oldest session keeping it.

    A resumed session holds the dispatches and subagent files of the session
    it resumed, so the same agent id turns up in both.  Later copies are
    removed in place; runs without an agent id are kept.

    Args:
        analyses: Parsed sessions, oldest first.

    Returns:
        The analyses that still hold a subagent run, plus audit and instance
        sessions that hold main-session calls.
    """
    seen: set[str] = set()
    for analysis in analyses:
        kept = []
        for run in analysis.subagents:
            if run.agent_id and run.agent_id in seen:
                continue
            if run.agent_id:
                seen.add(run.agent_id)
            kept.append(run)
        analysis.subagents = kept
    # An audit or instance session can be all main-session work.
    return [a for a in analyses if a.subagents or (a.orchestrator_tools and run_kind(a.cycle_id or "") != "cycle")]


def _load_subagent_transcripts(file_path: str, agent_dispatches: dict[str, SubagentRun]) -> None:
    """Fill tool calls from ``<session>/subagents/agent-*.jsonl`` (current layout)."""
    sub_dir = Path(file_path).with_suffix("") / "subagents"
    if not sub_dir.is_dir():
        return
    for meta_path in sorted(sub_dir.glob("agent-*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        run = agent_dispatches.get(meta.get("toolUseId", ""))
        transcript = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
        if run is None or not transcript.exists() or run.tool_calls:
            continue  # unknown dispatch, missing file, or already filled from progress records
        run.agent_id = run.agent_id or meta_path.name[len("agent-") : -len(".meta.json")]
        for rec in _read_jsonl(transcript):
            ts = rec.get("timestamp")
            if ts:
                run.first_timestamp = run.first_timestamp or ts
                run.last_timestamp = ts
            if rec.get("type") != "assistant":
                continue
            _observe_turn(run, rec.get("message", {}))
            for block in extract_tool_calls_from_content(rec.get("message", {}).get("content", [])):
                name = block.get("name", "?")
                inp = block.get("input", {})
                run.tool_calls.append(
                    ToolCall(
                        index=len(run.tool_calls),
                        name=name,
                        input_summary=summarize_tool_input(name, inp),
                        category=classify_tool(name, inp),
                    )
                )


_NOTIFICATION_USAGE = re.compile(
    r"<tool-use-id>(\S+?)</tool-use-id>(?:(?!</task-notification>).)*?"
    r"<subagent_tokens>(\d+)</subagent_tokens>\s*<tool_uses>(\d+)</tool_uses>\s*<duration_ms>(\d+)</duration_ms>",
    re.S,
)


def _apply_notification_usage(records: list[dict], agent_dispatches: dict[str, SubagentRun]) -> None:
    """Read usage totals from ``<task-notification>`` blocks (background agents).

    A resumed agent notifies again; the largest totals seen are kept.
    """
    for rec in records:
        blob = json.dumps(rec, ensure_ascii=False)
        if "<task-notification>" not in blob:
            continue
        for tui, tokens, tools, ms in _NOTIFICATION_USAGE.findall(blob):
            run = agent_dispatches.get(tui)
            if run is None:
                continue
            if run.usage_total_tokens is None or int(tokens) > run.usage_total_tokens:
                run.usage_total_tokens = int(tokens)
                run.usage_tool_count = int(tools)
                run.usage_duration_ms = int(ms)


def parse_session(file_path: str, cycle_filter: str | None = None) -> SessionAnalysis:
    """Parse a JSONL session file and extract PEV-relevant metrics.

    Args:
        file_path: The session's ``.jsonl`` log.
        cycle_filter: Keep only the tool calls and Agent dispatches attributed
            to this cycle id (see "Cycle attribution" in the module
            docstring).  ``None`` keeps the whole session.

    Returns:
        The analysis; ``cycle_id`` is *cycle_filter* when given, otherwise the
        cycle the first cycle-naming dispatch belongs to.
    """
    records = []
    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    analysis = SessionAnalysis(file_path=file_path, total_records=len(records), cycle_id=cycle_filter)
    analysis.claude_code_version = _session_version(records)
    _warn_if_untested(analysis.claude_code_version, file_path)

    # Step 1: Find Agent dispatches and orchestrator tool calls.
    #
    # A session can run several cycles, or mention a sibling cycle in passing,
    # so each call is attributed to the session's *current* cycle: a call
    # that names a cycle id moves it there (see named_cycle), a call that
    # names none (a code-reviewer dispatch, a git command) stays with it.
    # With a filter only the calls attributed to that cycle are kept.
    #
    # An instance is worked in the main session and names its id only once
    # its checkin is cloned, so a ``/pev-instance`` command clears the current
    # run and the calls made before any id is named are held; they go to the
    # first run named after them when that run is an instance.  Until then a
    # call that only mentions another run (pre-flight reading an old cycle's
    # doc) is held with them; a call that works on that run ends the hold.
    # A turn with no tool call goes with the run current at that point.
    agent_dispatches: dict[str, SubagentRun] = {}  # tool_use_id -> SubagentRun
    current_cycle: str | None = None
    first_named: str | None = None
    instance_preflight = False  # after /pev-instance, before its run id is named
    pending: list[tuple[dict | None, dict]] = []  # (tool_use block or None, its message) awaiting a run id

    def keep(block: dict | None, message: dict) -> None:
        if block is None:
            _observe_turn(analysis.main_run, message, [])
            return
        _observe_turn(analysis.main_run, message, [block])
        raw_input = block.get("input", {})
        if block.get("name") == "Agent":
            inp = raw_input if isinstance(raw_input, dict) else {}
            prompt = inp.get("prompt", "")
            agent_type = _normalize_agent_type(inp.get("subagent_type", "general-purpose"))
            description = inp.get("description", "")
            tool_use_id = block.get("id", "")

            if not analysis.cycle_id:
                analysis.cycle_id = named_cycle(prompt)

            # Detect context bundle markers in builder dispatches
            has_bundle = "ARCHITECT PITCH" in prompt and "KEY SOURCE FILES" in prompt

            agent_dispatches[tool_use_id] = SubagentRun(
                agent_type=agent_type,
                description=description,
                tool_use_id=tool_use_id,
                prompt_preview=prompt[:200],
                prompt_length=len(prompt),
                has_context_bundle=has_bundle,
            )
        else:
            # Orchestrator-level tool call
            name = block.get("name", "?")
            tc = ToolCall(
                index=len(analysis.orchestrator_tools),
                name=name,
                input_summary=summarize_tool_input(name, raw_input),
                category=classify_tool(name, raw_input),
            )
            analysis.orchestrator_tools.append(tc)

    for rec in records:
        if rec.get("type") == "user" and _INSTANCE_COMMAND.search(_tool_input_text(rec.get("message", {}))):
            current_cycle, pending, instance_preflight = None, [], True
            continue
        if rec.get("type") != "assistant":
            continue
        message = rec.get("message", {})
        blocks = extract_tool_calls_from_content(message.get("content", []))
        if not blocks:
            if current_cycle is None and cycle_filter is not None:
                pending.append((None, message))
            elif cycle_filter is None or current_cycle == cycle_filter:
                keep(None, message)
            continue
        for block in blocks:
            raw_input = block.get("input", {})
            if block.get("name") == "Skill" and _INSTANCE_SKILL.search(_tool_input_text(raw_input)):
                current_cycle, pending, instance_preflight = None, [], True
            named = named_cycle(_tool_input_text(raw_input))
            if named and instance_preflight and run_kind(named) != "instance" and not _block_works_on_run(block, named):
                named = None  # a mention during the instance's pre-flight: held with the rest
            if named:
                if pending and named == cycle_filter and run_kind(named) == "instance":
                    for held_block, held_message in pending:
                        keep(held_block, held_message)
                pending = []
                current_cycle = named
                instance_preflight = False
                first_named = first_named or named
            elif current_cycle is None and cycle_filter is not None:
                pending.append((block, message))
                continue
            if cycle_filter is not None and current_cycle != cycle_filter:
                continue
            keep(block, message)
    analysis.cycle_id = analysis.cycle_id or first_named

    # Step 2: Extract subagent tool calls from progress records
    for rec in records:
        if rec.get("type") != "progress":
            continue
        parent_id = rec.get("parentToolUseID", "")
        if parent_id not in agent_dispatches:
            continue

        run = agent_dispatches[parent_id]
        data = rec.get("data", {})

        # Track timestamps
        ts = rec.get("timestamp")
        if ts:
            if not run.first_timestamp:
                run.first_timestamp = ts
            run.last_timestamp = ts

        if data.get("type") != "agent_progress":
            continue

        # Extract agent ID
        if not run.agent_id:
            run.agent_id = data.get("agentId")

        # Extract tool calls from the progress message
        msg = data.get("message", {})
        inner_msg = msg.get("message", {})
        if isinstance(inner_msg, dict):
            _observe_turn(run, inner_msg)
        content = inner_msg.get("content", [])
        for block in extract_tool_calls_from_content(content):
            inp = block.get("input", {})
            name = block.get("name", "?")
            tc = ToolCall(
                index=len(run.tool_calls),
                name=name,
                input_summary=summarize_tool_input(name, inp),
                category=classify_tool(name, inp),
            )
            run.tool_calls.append(tc)

    # Step 2b: current layout -- per-subagent transcript files
    _load_subagent_transcripts(file_path, agent_dispatches)

    # Step 3: Extract usage stats from tool_result blocks (fallback when
    # progress records are absent — newer Claude Code session format)
    for rec in records:
        if rec.get("type") != "user":
            continue
        content = rec.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tui = block.get("tool_use_id", "")
            if tui not in agent_dispatches:
                continue
            run = agent_dispatches[tui]
            # Parse <usage> tag from result text blocks
            result_content = block.get("content", [])
            if isinstance(result_content, str):
                result_content = [{"type": "text", "text": result_content}]
            for rb in result_content:
                if not isinstance(rb, dict) or rb.get("type") != "text":
                    continue
                text = rb.get("text", "")
                # Extract agentId
                aid_match = re.search(r"agentId:\s*(\S+)", text)
                if aid_match and not run.agent_id:
                    run.agent_id = aid_match.group(1)
                # Extract <usage> block. The tag's first field has been both
                # "total_tokens" (older sessions) and "subagent_tokens"
                # (current format) — accept either so the usage fallback keeps
                # working across Claude Code session-format changes.
                usage_match = re.search(
                    r"(?:total_tokens|subagent_tokens):\s*(\d+)\s*\n\s*tool_uses:\s*(\d+)\s*\n\s*duration_ms:\s*(\d+)",
                    text,
                )
                if usage_match:
                    run.usage_total_tokens = int(usage_match.group(1))
                    run.usage_tool_count = int(usage_match.group(2))
                    run.usage_duration_ms = int(usage_match.group(3))

    # Step 3b: current layout -- usage totals in <task-notification> blocks
    _apply_notification_usage(records, agent_dispatches)

    analysis.subagents = list(agent_dispatches.values())

    # Reclassify all tool calls from reviewer agents as "review"
    # so they don't inflate builder exploration numbers
    for run in analysis.subagents:
        if run.agent_type in REVIEWER_AGENT_TYPES:
            for tc in run.tool_calls:
                tc.category = "review"

    # Post-process: reclassify exploration reads that support edits
    for run in analysis.subagents:
        reclassify_impl_support(run.tool_calls)

    return analysis


# ── Metrics ──────────────────────────────────────────────────────────────────


def compute_handoff_gap(tools: list[ToolCall]) -> int:
    """Number of tool calls before first implementation action."""
    for i, tc in enumerate(tools):
        if tc.category in ("implementation", "impl_support"):
            return i
    return len(tools)  # Never implemented


def find_redundant_reads(tools: list[ToolCall]) -> list[str]:
    """Find files read via both axiom_graph_source and Read/Bash."""
    source_modules = set()
    file_reads = set()
    for tc in tools:
        if tc.name == "mcp__axiom-graph__axiom_graph_source":
            # Extract the module path hint from node ID
            # e.g., {project_id}::axiom_graph.scanners.module_scanner -> axiom_graph/scanners/module_scanner
            node_id = tc.input_summary
            parts = node_id.split("::")
            if len(parts) >= 2:
                # Take the dotted module path, convert to path-like
                mod = parts[1].split("::")[0]  # top-level module
                source_modules.add(mod)
        elif tc.name == "Read":
            path = tc.input_summary.replace("\\", "/")
            file_reads.add(path)

    redundant = []
    for mod in source_modules:
        mod_path = mod.replace(".", "/")
        for fpath in file_reads:
            if mod_path in fpath:
                redundant.append(f"axiom_graph_source({mod}) + Read({fpath})")
    return redundant


def reclassify_impl_support(tools: list[ToolCall]) -> None:
    """Reclassify exploration reads that support edits as impl_support.

    Rules:
    1. Read of a file that is also an Edit/Write target → impl_support
    2. Exploration call within LOOKBACK calls before an implementation call → impl_support
    """
    LOOKBACK = 5

    # Collect all edited file paths (normalized forward slashes)
    edited_files: set[str] = set()
    for tc in tools:
        if tc.name in ("Edit", "Write"):
            edited_files.add(tc.input_summary.replace("\\", "/"))

    if not edited_files:
        return

    # Find indices of all implementation calls
    impl_indices = {i for i, tc in enumerate(tools) if tc.category == "implementation"}

    for i, tc in enumerate(tools):
        if tc.category != "exploration":
            continue

        # Rule 1: Read of an edited file
        if tc.name == "Read":
            norm = tc.input_summary.replace("\\", "/")
            if norm in edited_files:
                tc.category = "impl_support"
                continue

        # Rule 2: exploration within LOOKBACK of an implementation call
        for j in range(i + 1, min(i + LOOKBACK + 1, len(tools))):
            if j in impl_indices:
                tc.category = "impl_support"
                break


MCP_EXPLORATION_TOOLS = {
    "mcp__axiom-graph__axiom_graph_search",
    "mcp__axiom-graph__axiom_graph_source",
    "mcp__axiom-graph__axiom_graph_read_doc",
    "mcp__axiom-graph__axiom_graph_list",
    "mcp__axiom-graph__axiom_graph_graph",
    "mcp__axiom-graph__axiom_graph_render",
    "mcp__axiom-graph__axiom_graph_report",
    "mcp__axiom-graph__axiom_graph_sql",
    "mcp__axiom-graph__axiom_graph_diff",
    "mcp__axiom-graph__axiom_graph_history",
    "mcp__axiom-graph__axiom_graph_list_undocumented",
    "mcp__axiom-graph__axiom_graph_list_reference_points",
}

RAW_EXPLORATION_TOOLS = {"Grep", "Glob", "Read"}


def compute_mcp_ratio(tools: list[ToolCall]) -> dict:
    """Compute what fraction of exploration uses MCP vs raw file tools.

    Returns dict with mcp_count, raw_count, ratio (0-1), and
    raw_reads list of (index, summary) for files that could have used MCP.
    """
    mcp_count = 0
    raw_count = 0
    raw_reads: list[tuple[int, str]] = []
    bash_exploration = 0

    for tc in tools:
        if tc.name in MCP_EXPLORATION_TOOLS:
            mcp_count += 1
        elif tc.name in RAW_EXPLORATION_TOOLS:
            raw_count += 1
            raw_reads.append((tc.index, f"{tc.name}({tc.input_summary})"))
        elif tc.name == "Bash" and tc.category == "exploration":
            bash_exploration += 1
            raw_reads.append((tc.index, f"Bash({tc.input_summary})"))

    raw_count += bash_exploration
    total = mcp_count + raw_count
    ratio = mcp_count / total if total > 0 else 0.0

    return {
        "mcp_count": mcp_count,
        "raw_count": raw_count,
        "total_exploration": total,
        "ratio": ratio,
        "raw_reads": raw_reads,
    }


def count_cd_waste(tools: list[ToolCall]) -> int:
    """Count standalone cd commands."""
    return sum(1 for tc in tools if tc.category == "waste")


# ── Output ───────────────────────────────────────────────────────────────────


def _format_usage_fallback(run: SubagentRun) -> str:
    """Format usage stats from <usage> tags as a fallback summary line."""
    parts = []
    if run.usage_tool_count is not None:
        parts.append(f"{run.usage_tool_count} tool calls")
    if run.usage_total_tokens is not None:
        parts.append(f"{run.usage_total_tokens:,} tokens")
    if run.usage_duration_ms is not None:
        secs = run.usage_duration_ms / 1000
        if secs >= 60:
            parts.append(f"{secs / 60:.1f} min")
        else:
            parts.append(f"{secs:.0f}s")
    return ", ".join(parts)


def print_subagent_summary(run: SubagentRun, verbose: bool = False) -> None:
    """Print analysis for one subagent run."""
    tools = run.tool_calls
    if not tools:
        fallback = _format_usage_fallback(run)
        if fallback:
            print(f"  (from usage summary: {fallback})")
        else:
            print("  (no tool calls captured)")
        return

    total = len(tools)
    counts = Counter(tc.category for tc in tools)
    tool_counts = Counter(tc.name for tc in tools)
    gap = compute_handoff_gap(tools)
    redundant = find_redundant_reads(tools)
    cd_waste = count_cd_waste(tools)

    # Duration
    duration = ""
    if run.first_timestamp and run.last_timestamp:
        try:
            t0 = datetime.fromisoformat(run.first_timestamp.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(run.last_timestamp.replace("Z", "+00:00"))
            delta = t1 - t0
            mins = delta.total_seconds() / 60
            duration = f" ({mins:.1f} min)"
        except (ValueError, TypeError):
            pass

    print(f"  Total tool calls: {total}{duration}")

    # Context bundle check (for builder agents)
    if run.prompt_length > 0:
        print(f"  Dispatch prompt: {run.prompt_length:,} chars")
    print()

    # Category breakdown
    print("  Category breakdown:")
    for cat in ("exploration", "implementation", "test", "review", "infra", "waste", "other"):
        c = counts.get(cat, 0)
        if c:
            pct = c / total * 100
            bar = "#" * int(pct / 2)
            print(f"    {cat:16s} {c:3d} ({pct:4.1f}%) {bar}")

    print()
    print(f"  Handoff gap: {gap} calls before first edit/write", end="")
    if total > 0:
        print(f" ({gap / total * 100:.0f}% of session)")
    else:
        print()

    if cd_waste:
        print(f"  Wasted cd calls: {cd_waste}")

    if redundant:
        print(f"  Redundant reads ({len(redundant)}):")
        for r in redundant:
            print(f"    - {r}")

    # MCP vs raw exploration ratio
    mcp_info = compute_mcp_ratio(tools)
    if mcp_info["total_exploration"] > 0:
        ratio_pct = mcp_info["ratio"] * 100
        print()
        print(
            f"  MCP tool ratio: {mcp_info['mcp_count']}"
            f"/{mcp_info['total_exploration']} exploration calls"
            f" ({ratio_pct:.0f}% MCP)"
        )
        if mcp_info["raw_count"] > 0:
            print(f"  Raw reads that could use MCP ({mcp_info['raw_count']}):")
            for idx, desc in mcp_info["raw_reads"][:10]:
                print(f"    call #{idx + 1}: {desc}")
            if len(mcp_info["raw_reads"]) > 10:
                print(f"    ... and {len(mcp_info['raw_reads']) - 10} more")

    # Tool frequency
    print()
    print("  Tool frequency:")
    for name, count in tool_counts.most_common():
        print(f"    {count:3d}  {name}")

    # Verbose: full sequence
    if verbose:
        print()
        print("  Full tool sequence:")
        for tc in tools:
            marker = ""
            if tc.category == "waste":
                marker = " [WASTE]"
            elif tc.category == "implementation":
                marker = " [IMPL]"
            elif tc.category == "impl_support":
                marker = " [IMPL_SUP]"
            print(f"    {tc.index + 1:3d}. {tc.name:42s} {tc.input_summary}{marker}")


def print_analysis(analysis: SessionAnalysis, verbose: bool = False) -> None:
    """Print the full analysis report."""
    print("=" * 72)
    print("PEV Session Analysis")
    print("=" * 72)
    print(f"Session: {analysis.file_path}")
    if analysis.cycle_id:
        print(f"Cycle:   {analysis.cycle_id}")
    print(f"Records: {analysis.total_records}")
    print(f"Subagent dispatches: {len(analysis.subagents)}")
    print()

    # Orchestrator summary
    if analysis.orchestrator_tools:
        print(f"--- Orchestrator ({len(analysis.orchestrator_tools)} tool calls) ---")
        orch_counts = Counter(tc.name for tc in analysis.orchestrator_tools)
        for name, count in orch_counts.most_common():
            print(f"  {count:3d}  {name}")
        print()

    # Per-subagent analysis
    for i, run in enumerate(analysis.subagents):
        print(f"--- Subagent {i + 1}: {run.agent_type} ---")
        print(f"  Description: {run.description}")
        if verbose:
            print(f"  Prompt: {run.prompt_preview}...")
        print()
        print_subagent_summary(run, verbose=verbose)
        print()

    # Overall efficiency score
    all_tools = []
    for run in analysis.subagents:
        all_tools.extend(run.tool_calls)

    if all_tools:
        total = len(all_tools)
        cats = Counter(tc.category for tc in all_tools)
        impl = cats.get("implementation", 0)
        explore = cats.get("exploration", 0)
        waste = cats.get("waste", 0)
        test = cats.get("test", 0)

        print("=" * 72)
        print("Overall subagent efficiency")
        print("=" * 72)
        review = cats.get("review", 0)

        print(f"  Total subagent tool calls: {total}")
        impl_sup = cats.get("impl_support", 0)
        print(f"  Implementation:  {impl:3d} ({impl / total * 100:.0f}%)")
        if impl_sup:
            print(f"  Impl support:    {impl_sup:3d} ({impl_sup / total * 100:.0f}%)")
        print(f"  Exploration:     {explore:3d} ({explore / total * 100:.0f}%)")
        print(f"  Testing:         {test:3d} ({test / total * 100:.0f}%)")
        if review:
            print(f"  Review:          {review:3d} ({review / total * 100:.0f}%)")
        print(f"  Waste:           {waste:3d} ({waste / total * 100:.0f}%)")
        if waste > 0:
            print(f"  -> {waste} tool calls could be eliminated with better handoff")
        if explore > impl:
            print("  -> Exploration exceeds implementation — handoff could include more context")
    else:
        # Fallback: aggregate usage-only stats
        usage_runs = [r for r in analysis.subagents if r.usage_tool_count is not None]
        if usage_runs:
            total_calls = sum(r.usage_tool_count for r in usage_runs)
            total_tokens = sum(r.usage_total_tokens or 0 for r in usage_runs)
            total_ms = sum(r.usage_duration_ms or 0 for r in usage_runs)

            print("=" * 72)
            print("Overall subagent efficiency (from usage summaries)")
            print("=" * 72)
            print(f"  Total subagent tool calls: {total_calls}")
            print(f"  Total tokens:              {total_tokens:,}")
            print(f"  Total duration:            {total_ms / 60000:.1f} min")
            print()
            print("  Per-agent breakdown:")
            for run in usage_runs:
                dur = f"{run.usage_duration_ms / 60000:.1f}m" if run.usage_duration_ms else "?"
                tok = f"{run.usage_total_tokens:,}" if run.usage_total_tokens else "?"
                print(
                    f"    {run.agent_type:18s} {run.usage_tool_count:3d} calls  {dur}  {tok} tok  [{run.description}]"
                )


def print_summary(analysis: SessionAnalysis) -> None:
    """Print a compact one-screen summary for human gates."""
    cycle = analysis.cycle_id or "unknown"
    print(f"PEV Efficiency — {cycle}")
    print("-" * 50)

    # Separate implementer agents from reviewers for metrics
    implementer_tools = []
    reviewer_tools = []
    for run in analysis.subagents:
        is_reviewer = run.agent_type in REVIEWER_AGENT_TYPES
        for tc in run.tool_calls:
            if is_reviewer:
                reviewer_tools.append(tc)
            else:
                implementer_tools.append(tc)

    all_tools = implementer_tools + reviewer_tools
    if not all_tools:
        # Check for usage-only data
        usage_runs = [r for r in analysis.subagents if r.usage_tool_count is not None]
        if usage_runs:
            total_calls = sum(r.usage_tool_count for r in usage_runs)
            total_tokens = sum(r.usage_total_tokens or 0 for r in usage_runs)
            total_ms = sum(r.usage_duration_ms or 0 for r in usage_runs)
            print("  (detailed tool calls not available — summary from usage tags)")
            print(f"  Total subagent calls: {total_calls}")
            print(f"  Total tokens: {total_tokens:,}")
            print(f"  Total duration: {total_ms / 60000:.1f} min")
        else:
            print("No subagent tool calls captured.")
        return

    total = len(all_tools)
    cats = Counter(tc.category for tc in all_tools)

    # One-line per category
    for cat in ("exploration", "impl_support", "implementation", "test", "review", "infra", "waste"):
        c = cats.get(cat, 0)
        if c:
            pct = c / total * 100
            bar = "#" * int(pct / 3)
            print(f"  {cat:16s} {c:3d} ({pct:4.1f}%) {bar}")

    print()

    # Main session's own turns, then a one-liner per subagent
    main_ctx, _ = context_summary_lines(analysis.main_run)
    if main_ctx:
        print(f"  {'main-session':18s} {len(analysis.orchestrator_tools):3d} calls{main_ctx}")
    for i, run in enumerate(analysis.subagents):
        tools = run.tool_calls
        if not tools:
            # Fallback: show usage summary if available
            if run.usage_tool_count is not None:
                dur = ""
                if run.usage_duration_ms is not None:
                    dur = f" {run.usage_duration_ms / 60000:.1f}m"
                tokens = ""
                if run.usage_total_tokens is not None:
                    tokens = f"  {run.usage_total_tokens:,} tok"
                print(f"  {run.agent_type:18s} {run.usage_tool_count:3d} calls{dur}{tokens}")
            continue
        n = len(tools)
        is_reviewer = run.agent_type in REVIEWER_AGENT_TYPES

        dur = ""
        if run.first_timestamp and run.last_timestamp:
            try:
                t0 = datetime.fromisoformat(run.first_timestamp.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(run.last_timestamp.replace("Z", "+00:00"))
                dur = f" {(t1 - t0).total_seconds() / 60:.1f}m"
            except (ValueError, TypeError):
                pass

        ctx, boundaries = context_summary_lines(run)
        if is_reviewer:
            # Reviewers are read-only — no gap or redundancy flags
            print(f"  {run.agent_type:18s} {n:3d} calls{dur}{ctx}  (review)")
        else:
            gap = compute_handoff_gap(tools)
            gap_pct = round(gap / n * 100) if n else 0
            redundant = len(find_redundant_reads(tools))
            mcp_info = compute_mcp_ratio(tools)
            mcp_pct = round(mcp_info["ratio"] * 100)

            flags = []
            if gap_pct > 40 and run.agent_type in GAP_FLAGGED_AGENT_TYPES:
                flags.append(f"gap={gap_pct}%")
            if redundant:
                flags.append(f"{redundant} redundant")
            if mcp_info["total_exploration"] > 3 and mcp_pct < 50:
                flags.append(f"mcp={mcp_pct}%")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""

            print(f"  {run.agent_type:18s} {n:3d} calls{dur}{ctx}{flag_str}")
        if boundaries:
            print(f"      {boundaries}")

    # Bottom-line verdict (based on implementer agents only)
    print()
    if not implementer_tools:
        usage_runs = [r for r in analysis.subagents if r.usage_tool_count is not None]
        if usage_runs:
            print("  Verdict: Tool call details unavailable (no progress records in session log)")
        else:
            print("  Verdict: No implementer agents found")
    else:
        impl_total = len(implementer_tools)
        impl_cats = Counter(tc.category for tc in implementer_tools)
        impl_pct = (impl_cats.get("implementation", 0) + impl_cats.get("impl_support", 0)) / impl_total * 100
        waste_pct = impl_cats.get("waste", 0) / impl_total * 100
        if impl_pct >= 30 and waste_pct < 10:
            print("  Verdict: Good efficiency")
        elif impl_pct >= 20:
            print("  Verdict: Acceptable — handoff has room for improvement")
        else:
            print("  Verdict: Low efficiency — handoff needs more context")


# ── DocJSON output ───────────────────────────────────────────────────────────


def _get_duration_min(run: SubagentRun) -> float | None:
    """Extract duration in minutes from timestamps."""
    if run.first_timestamp and run.last_timestamp:
        try:
            t0 = datetime.fromisoformat(run.first_timestamp.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(run.last_timestamp.replace("Z", "+00:00"))
            return round((t1 - t0).total_seconds() / 60, 1)
        except (ValueError, TypeError):
            pass
    return None


def _subagent_metrics(run: SubagentRun) -> dict:
    """Compute metrics dict for a single subagent."""
    tools = run.tool_calls
    total = len(tools)
    if total == 0:
        # Return usage-only metrics if available
        if run.usage_tool_count is not None:
            duration_min = None
            if run.usage_duration_ms is not None:
                duration_min = round(run.usage_duration_ms / 60000, 1)
            return {
                "total": run.usage_tool_count,
                "duration_min": duration_min,
                "total_tokens": run.usage_total_tokens,
                "usage_only": True,
            }
        return {"total": 0}

    cats = Counter(tc.category for tc in tools)
    tool_counts = Counter(tc.name for tc in tools)
    gap = compute_handoff_gap(tools)
    redundant = find_redundant_reads(tools)
    cd_waste = count_cd_waste(tools)

    duration_min = None
    if run.first_timestamp and run.last_timestamp:
        try:
            t0 = datetime.fromisoformat(run.first_timestamp.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(run.last_timestamp.replace("Z", "+00:00"))
            duration_min = round((t1 - t0).total_seconds() / 60, 1)
        except (ValueError, TypeError):
            pass

    mcp_info = compute_mcp_ratio(tools)

    return {
        "total": total,
        "duration_min": duration_min,
        "categories": {
            cat: cats.get(cat, 0)
            for cat in ("exploration", "implementation", "test", "review", "infra", "waste")
            if cats.get(cat, 0) > 0
        },
        "handoff_gap": gap,
        "handoff_gap_pct": round(gap / total * 100),
        "cd_waste": cd_waste,
        "redundant_reads": redundant,
        "mcp_ratio": mcp_info,
        "has_context_bundle": run.has_context_bundle,
        "prompt_length": run.prompt_length,
        "tool_frequency": dict(tool_counts.most_common()),
        "context": context_metrics(run),
    }


def _format_bar(label: str, count: int, total: int) -> str:
    """Format a category line with percentage bar."""
    if total == 0:
        return f"- {label}: 0"
    pct = count / total * 100
    bar = "\u2588" * int(pct / 4)  # block char, ~25 wide max
    return f"- {label}: {count} ({pct:.0f}%) {bar}"


def _subagent_section_content(run: SubagentRun, metrics: dict) -> str:
    """Render markdown content for a subagent section."""
    lines = [f"**{run.description}**"]
    total = metrics["total"]
    if total == 0:
        lines.append("\nNo tool calls captured.")
        return "\n".join(lines)

    dur = f" in {metrics['duration_min']} min" if metrics.get("duration_min") else ""
    lines.append(f"\n{total} tool calls{dur}")

    ctx = metrics.get("context", {})
    if ctx.get("turns"):
        lines.append(
            f"\n**Context:** {ctx['first_context']:,} tokens at the first turn, "
            f"peak {ctx['peak_context']:,}, {ctx['turns']} turns"
        )

    # Context bundle status
    if metrics.get("prompt_length", 0) > 0:
        lines.append(f"\n**Dispatch prompt:** {metrics['prompt_length']:,} chars")

    # Category breakdown
    lines.append("\n**Category breakdown:**")
    for cat, count in metrics.get("categories", {}).items():
        lines.append(_format_bar(cat, count, total))

    # Handoff gap
    if "handoff_gap" in metrics:
        gap = metrics["handoff_gap"]
        lines.append(
            f"\n**Handoff gap:** {gap} calls before first edit/write ({metrics['handoff_gap_pct']}% of session)"
        )

    if metrics.get("cd_waste"):
        lines.append(f"**Wasted cd calls:** {metrics['cd_waste']}")

    if metrics.get("redundant_reads"):
        lines.append(f"\n**Redundant reads** ({len(metrics['redundant_reads'])}):")
        for r in metrics["redundant_reads"]:
            lines.append(f"- {r}")

    # MCP tool ratio
    mcp = metrics.get("mcp_ratio", {})
    if mcp.get("total_exploration", 0) > 0:
        ratio_pct = round(mcp["ratio"] * 100)
        lines.append(
            f"\n**MCP ratio:** {mcp['mcp_count']}/{mcp['total_exploration']} exploration calls ({ratio_pct}% MCP)"
        )
        if mcp["raw_count"] > 0:
            lines.append("Raw reads that could use MCP:")
            for idx, desc in mcp["raw_reads"][:5]:
                lines.append(f"- call #{idx + 1}: {desc}")

    # Tool frequency
    lines.append("\n**Tool frequency:**")
    for name, count in metrics.get("tool_frequency", {}).items():
        lines.append(f"- {count} {name}")

    return "\n".join(lines)


def _render_collapsed_tools(tools: list[ToolCall]) -> list[str]:
    """Render tool calls, collapsing consecutive waste into counts."""
    lines = []
    waste_run = 0
    for tc in tools:
        if tc.category == "waste":
            waste_run += 1
            continue
        if waste_run > 0:
            lines.append(f"*({waste_run} cd calls)*")
            waste_run = 0
        marker = ""
        if tc.category == "implementation":
            marker = " **[IMPL]**"
        elif tc.category == "test":
            marker = " **[TEST]**"
        lines.append(f"{tc.index + 1}. `{tc.name}` {tc.input_summary}{marker}")
    if waste_run > 0:
        lines.append(f"*({waste_run} cd calls)*")
    return lines


def _phased_sequence_content(run: SubagentRun) -> str:
    """Render tool sequence split into orient/work phases, waste collapsed."""
    tools = run.tool_calls
    if not tools:
        return "No tool calls captured."

    gap = compute_handoff_gap(tools)
    orient = tools[:gap] if gap > 0 else []
    work = tools[gap:] if gap < len(tools) else []

    lines = []
    if orient:
        waste_in_orient = sum(1 for tc in orient if tc.category == "waste")
        active = len(orient) - waste_in_orient
        lines.append(f"**Orient** ({len(orient)} calls, {active} substantive):\n")
        lines.extend(_render_collapsed_tools(orient))

    if work:
        if orient:
            lines.append("")
        waste_in_work = sum(1 for tc in work if tc.category == "waste")
        active = len(work) - waste_in_work
        lines.append(f"**Work** ({len(work)} calls, {active} substantive):\n")
        lines.extend(_render_collapsed_tools(work))

    return "\n".join(lines)


def _collapsed_sequence_content(run: SubagentRun) -> str:
    """Render tool sequence with waste collapsed but no phase split."""
    tools = run.tool_calls
    if not tools:
        return "No tool calls captured."
    return "\n".join(_render_collapsed_tools(tools))


def _build_handoff_timeline(analysis: SessionAnalysis) -> str:
    """Build a markdown table showing the dispatch chain."""
    lines = [
        "| # | Role | Description | Calls | Duration | Gap | Issues |",
        "|--:|------|-------------|------:|---------:|----:|--------|",
    ]
    for i, run in enumerate(analysis.subagents):
        tools = run.tool_calls
        n = len(tools)
        role = run.agent_type
        for prefix in ("pev-", "superpowers:code-", "superpowers:"):
            if role.startswith(prefix):
                role = role[len(prefix) :]
                break

        dur_min = _get_duration_min(run)
        dur = f"{dur_min}m" if dur_min is not None else "—"

        is_reviewer = run.agent_type in REVIEWER_AGENT_TYPES
        if is_reviewer or n == 0:
            gap_str = "—"
        else:
            gap = compute_handoff_gap(tools)
            gap_pct = round(gap / n * 100) if n else 0
            gap_str = f"{gap_pct}%"

        issues = []
        if not is_reviewer and n > 0:
            redundant = find_redundant_reads(tools)
            cd_waste = count_cd_waste(tools)
            if redundant:
                issues.append(f"{len(redundant)} redundant")
            if cd_waste > 3:
                issues.append(f"{cd_waste} cd waste")
        issue_str = ", ".join(issues) if issues else "—"

        lines.append(f"| {i + 1} | {role} | {run.description} | {n} | {dur} | {gap_str} | {issue_str} |")

    return "\n".join(lines)


def _build_reviewer_aggregate(reviewer_runs: list[SubagentRun]) -> str:
    """Collapse reviewer dispatches into a summary."""
    total_calls = sum(len(r.tool_calls) for r in reviewer_runs)
    durations = [_get_duration_min(r) for r in reviewer_runs]
    valid_durs = [d for d in durations if d is not None]
    dur_str = f", {sum(valid_durs):.1f} min total" if valid_durs else ""

    lines = [f"**{len(reviewer_runs)} dispatches, {total_calls} tool calls{dur_str}**\n"]
    for r in reviewer_runs:
        n = len(r.tool_calls)
        d = _get_duration_min(r)
        dur = f" ({d}m)" if d else ""
        lines.append(f"- {r.description}: {n} calls{dur}")

    return "\n".join(lines)


def context_table(runs: list[SubagentRun]) -> str | None:
    """Render the per-agent context table and the Builder task-boundary table.

    Args:
        runs: The runs to list, in dispatch order.

    Returns:
        Markdown, or ``None`` when no run carried per-turn usage.
    """
    rows = []
    boundary_rows = []
    for run in runs:
        m = context_metrics(run)
        if not m["turns"]:
            continue
        rows.append(
            f"| {run.agent_type} | {run.description} | {m['turns']} | {m['first_context']:,} | {m['peak_context']:,} |"
        )
        boundary_rows += [f"| {run.description} | {label} | {tokens:,} |" for label, tokens in m["task_boundaries"]]
    if not rows:
        return None
    lines = [
        "Context is the tokens the agent saw on a turn: input plus cache-creation and cache-read tokens.",
        "",
        "| Agent | Run | Turns | First turn | Peak |",
        "|---|---|---|---|---|",
        *rows,
    ]
    if boundary_rows:
        lines += [
            "",
            "**Builder task boundaries** (context on the turn that wrote each task manifest):",
            "",
            "| Run | Task | Context |",
            "|---|---|---|",
            *boundary_rows,
        ]
    return "\n".join(lines)


def build_docjson(analysis: SessionAnalysis) -> dict:
    """Build a DocJSON document from the analysis."""
    cycle_id = analysis.cycle_id or "unknown-cycle"
    sections = []

    # ── Summary ──
    all_tools = []
    for run in analysis.subagents:
        all_tools.extend(run.tool_calls)
    total = len(all_tools)
    cats = Counter(tc.category for tc in all_tools)

    summary_lines = [
        f"Cycle: {cycle_id}",
        f"Session: `{Path(analysis.file_path).name}`",
        f"Records: {analysis.total_records}",
        f"Subagent dispatches: {len(analysis.subagents)}",
    ]
    if total > 0:
        summary_lines.append(f"\n**Overall ({total} tool calls):**")
        for cat in ("exploration", "implementation", "test", "review", "infra", "waste"):
            c = cats.get(cat, 0)
            if c:
                summary_lines.append(_format_bar(cat, c, total))
        if cats.get("waste", 0) > 0:
            summary_lines.append(f"\n{cats['waste']} tool calls could be eliminated with better handoff.")
        if cats.get("exploration", 0) > cats.get("implementation", 0):
            summary_lines.append("Exploration exceeds implementation — handoff could include more context.")

    sections.append(
        {
            "id": "summary",
            "heading": "Summary",
            "content": "\n".join(summary_lines),
        }
    )

    # ── Per-agent context ──
    context = context_table([analysis.main_run, *analysis.subagents])
    if context:
        sections.append({"id": "context", "heading": "Context per Agent", "content": context})

    # ── Handoff Timeline ──
    if analysis.subagents:
        sections.append(
            {
                "id": "handoff-timeline",
                "heading": "Handoff Timeline",
                "content": _build_handoff_timeline(analysis),
            }
        )

    # ── Orchestrator ──
    if analysis.orchestrator_tools:
        orch_counts = Counter(tc.name for tc in analysis.orchestrator_tools)
        orch_lines = [f"{count} {name}" for name, count in orch_counts.most_common()]
        sections.append(
            {
                "id": "orchestrator",
                "heading": "Orchestrator",
                "content": (
                    f"**{len(analysis.orchestrator_tools)} tool calls**\n\n" + "\n".join(f"- {l}" for l in orch_lines)
                ),
            }
        )

    # ── Group agents by role ──
    architects = [r for r in analysis.subagents if r.agent_type in ARCHITECT_AGENT_TYPES]
    builders = [r for r in analysis.subagents if r.agent_type in BUILDER_AGENT_TYPES]
    reviewers = [r for r in analysis.subagents if r.agent_type in REVIEWER_AGENT_TYPES]
    auditors = [r for r in analysis.subagents if r.agent_type in AUDITOR_AGENT_TYPES]
    doc_reviewers = [r for r in analysis.subagents if r.agent_type in DOC_REVIEWER_AGENT_TYPES]
    known = (
        ARCHITECT_AGENT_TYPES
        | BUILDER_AGENT_TYPES
        | REVIEWER_AGENT_TYPES
        | AUDITOR_AGENT_TYPES
        | DOC_REVIEWER_AGENT_TYPES
    )
    others = [r for r in analysis.subagents if r.agent_type not in known]

    # ── Architects (collapsed into one section) ──
    if architects:
        arch_calls = sum(len(r.tool_calls) for r in architects)
        valid_durs = [d for d in (_get_duration_min(r) for r in architects) if d is not None]
        dur_str = f", {sum(valid_durs):.1f} min" if valid_durs else ""

        # Aggregate tool frequency across all architect dispatches
        arch_tool_counts: Counter = Counter()
        for r in architects:
            for tc in r.tool_calls:
                if tc.category != "waste":
                    arch_tool_counts[tc.name] += 1
        freq_lines = [f"- {count} {name}" for name, count in arch_tool_counts.most_common(8)]

        subsections = []
        for j, run in enumerate(architects):
            n = len(run.tool_calls)
            d = _get_duration_min(run)
            dur = f" ({d}m)" if d else ""
            content = _collapsed_sequence_content(run) if run.tool_calls else "No tool calls."
            subsections.append(
                {
                    "id": f"dispatch-{j + 1}",
                    "heading": f"{run.description} — {n} calls{dur}",
                    "content": content,
                }
            )

        sections.append(
            {
                "id": "architect",
                "heading": f"Architect ({len(architects)} dispatches, {arch_calls} calls{dur_str})",
                "content": "\n".join(freq_lines),
                "sections": subsections,
            }
        )

    # ── Builders (one section each with phased sequence) ──
    for j, run in enumerate(builders):
        metrics = _subagent_metrics(run)
        label = f"Builder {j + 1}" if len(builders) > 1 else "Builder"

        subsections = []
        if run.tool_calls:
            subsections.append(
                {
                    "id": "sequence",
                    "heading": f"{label} Tool Sequence",
                    "content": _phased_sequence_content(run),
                }
            )

        sections.append(
            {
                "id": f"builder-{j + 1}",
                "heading": f"{label}: {run.description}",
                "content": _subagent_section_content(run, metrics),
                "sections": subsections,
            }
        )

    # ── Reviewers (collapsed into aggregate) ──
    if reviewers:
        sections.append(
            {
                "id": "reviewers",
                "heading": f"Reviewers ({len(reviewers)} dispatches)",
                "content": _build_reviewer_aggregate(reviewers),
            }
        )

    # ── Auditors, Doc Reviewers, other agents (one section each with phased sequence) ──
    role_groups = [("auditor", "Auditor", auditors), ("doc-reviewer", "Doc Reviewer", doc_reviewers)]
    role_groups += [("other", f"Agent ({r.agent_type})", [r]) for r in others]
    labelled = []
    for slug, title, runs in role_groups:
        for j, run in enumerate(runs):
            labelled.append(
                (
                    f"{slug}-{j + 1}" if slug != "other" else f"other-{len(labelled) + 1}",
                    f"{title} {j + 1}" if len(runs) > 1 else title,
                    run,
                )
            )
    for section_id, label, run in labelled:
        metrics = _subagent_metrics(run)

        subsections = []
        if run.tool_calls:
            subsections.append(
                {
                    "id": "sequence",
                    "heading": f"{label} Tool Sequence",
                    "content": _phased_sequence_content(run),
                }
            )

        sections.append(
            {
                "id": section_id,
                "heading": f"{label}: {run.description}",
                "content": _subagent_section_content(run, metrics),
                "sections": subsections,
            }
        )

    # ── Recommendations ──
    recs = []
    for run in analysis.subagents:
        if run.agent_type in REVIEWER_AGENT_TYPES:
            continue
        metrics = _subagent_metrics(run)
        if metrics.get("handoff_gap_pct", 0) > 40 and run.agent_type in GAP_FLAGGED_AGENT_TYPES:
            recs.append(
                f"- **{run.agent_type}** ({run.description}): "
                f"handoff gap is {metrics['handoff_gap_pct']}% — "
                f"include more context in dispatch prompt"
            )
        if metrics.get("redundant_reads"):
            recs.append(
                f"- **{run.agent_type}** ({run.description}): "
                f"{len(metrics['redundant_reads'])} redundant read(s) — "
                f"pre-fetch axiom_graph_source output into dispatch"
            )
        if metrics.get("cd_waste", 0) > 3:
            recs.append(
                f"- **{run.agent_type}** ({run.description}): "
                f"{metrics['cd_waste']} wasted cd calls — "
                f"set working directory in dispatch setup"
            )
        # Flag low MCP tool ratio
        mcp = metrics.get("mcp_ratio", {})
        if mcp.get("total_exploration", 0) > 3 and mcp.get("ratio", 1.0) < 0.5:
            ratio_pct = round(mcp["ratio"] * 100)
            recs.append(
                f"- **{run.agent_type}** ({run.description}): "
                f"only {ratio_pct}% of exploration uses MCP tools — "
                f"prefer axiom_graph_source/axiom_graph_graph over Read/Grep to reduce context"
            )

    if recs:
        sections.append(
            {
                "id": "recommendations",
                "heading": "Recommendations",
                "content": "\n".join(recs),
            }
        )

    return {
        "title": f"PEV Efficiency: {cycle_id}",
        "tags": ["pev-efficiency"],
        "sections": sections,
    }


#: A run's report lands in the run's own directory (``RUN_DOC_FOLDERS``), e.g.
#: ``pev/cycles/{cycle-id}/efficiency``, beside the run's other docs.  Earlier
#: reports stay where they were written (``docs/pev/cycles/efficiency/``,
#: ``docs/pev-cycles/``): doc ids are path-based, so moving them would orphan
#: their history.

#: The report's doc name inside its run directory (``efficiency-sN`` for
#: session N of a multi-session run).
REPORT_DOC_NAME = "efficiency"


def run_doc_folder(run_id: str) -> str:
    """Return the docs-tree directory of a run, where its report is written.

    Args:
        run_id: A cycle, audit or instance id.

    Returns:
        ``pev/cycles/{id}``, ``pev/audits/{id}`` or ``pev/instances/{id}``, as a
        path slug relative to the docs root.
    """
    return f"{RUN_DOC_FOLDERS[run_kind(run_id)]}/{run_id}"


#: Where ``--docjson`` stages the report when ``--output-dir`` is not given.  The
#: script never writes into the docs tree itself: a file written there by hand is
#: a raw DocJSON edit the index flags and never verifies.  The orchestrator hands
#: the staged file to ``axiom_graph_write_doc(doc_file=...)``, which accepts paths
#: under the system temp directory.
DEFAULT_STAGING_DIR = Path(tempfile.gettempdir()) / "pev-efficiency"


class OutputDirMissing(Exception):
    """The output folder does not exist and creating it was not confirmed."""


def ensure_output_dir(out_dir: Path, assume_yes: bool) -> None:
    """Create *out_dir* if missing — with ``--yes``, or after an interactive prompt.

    Without a terminal (an agent's Bash call) there is no one to answer the
    prompt, so the folder is created only when ``--yes`` was passed.

    Raises:
        OutputDirMissing: When the folder is missing and creation was declined
            or could not be confirmed.
    """
    if out_dir.is_dir():
        return
    if not assume_yes:
        if not sys.stdin.isatty():
            raise OutputDirMissing(
                f"output folder {out_dir} does not exist; pass --yes to create it, or --output-dir to pick another"
            )
        try:
            answer = input(f"Output folder {out_dir} does not exist. Create it? [y/N] ").strip().lower()
        except EOFError:
            # isatty() can report a terminal that has no input behind it (e.g.
            # Git Bash on Windows with stdin redirected); treat it as no answer.
            raise OutputDirMissing(
                f"output folder {out_dir} does not exist and there is no input to confirm; pass --yes to create it"
            ) from None
        if answer not in ("y", "yes"):
            raise OutputDirMissing(f"output folder {out_dir} not created")
    out_dir.mkdir(parents=True, exist_ok=True)


def write_docjson(
    analysis: SessionAnalysis,
    output_dir: str | None = None,
    session_index: int | None = None,
    assume_yes: bool = False,
    doc_folder: str | None = None,
) -> tuple[str, str]:
    """Stage the analysis as a ``write_doc`` payload, return its path and doc slug.

    The payload is the DocJSON document plus an ``id`` path slug, ready for
    ``axiom_graph_write_doc(doc_file=<path>)``, which writes the ``.docjson``
    into the docs tree, indexes it and verifies it.

    Args:
        analysis: The parsed session.
        output_dir: Staging folder; defaults to ``DEFAULT_STAGING_DIR``, which is
            created without asking.
        session_index: Appended as ``-s<N>`` for multi-session cycles.
        assume_yes: Create a missing ``output_dir`` without asking.
        doc_folder: Docs-tree folder for the slug; defaults to the cycle's own
            directory (:func:`run_doc_folder`).

    Raises:
        OutputDirMissing: When an ``output_dir`` is missing and not confirmed.
    """
    doc = build_docjson(analysis)
    cycle_id = analysis.cycle_id or "unknown-cycle"

    # Append session index for multi-session cycles
    suffix = f"-s{session_index}" if session_index is not None else ""
    name = f"{cycle_id}-efficiency{suffix}"  # staged file name, unique across runs
    slug = f"{(doc_folder or run_doc_folder(cycle_id)).strip('/')}/{REPORT_DOC_NAME}{suffix}"

    if output_dir:
        out_dir = Path(output_dir)
        ensure_output_dir(out_dir, assume_yes)
    else:
        out_dir = DEFAULT_STAGING_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.json"
    out_path.write_text(
        json.dumps({"id": slug, **doc}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(out_path), slug


# ── Session finder ───────────────────────────────────────────────────────────


def session_dispatches_cycle(jsonl_file: Path, cycle_id: str, project_root: str | None = None) -> bool:
    """Return whether a session log dispatches a PEV agent for *cycle_id*.

    The whole log is read: an orchestrator session often runs other work
    before ``/pev-cycle`` starts, so the cycle id can first appear deep in the
    file.  Merely mentioning the id (reading the manifest, a sibling cycle's
    prompt citing it) does not count; a ``pev-*`` Agent dispatch whose prompt
    belongs to the cycle (see :func:`named_cycle`) does.

    With *project_root*, the dispatch must also have been made in that project
    (see :func:`_record_in_project`), so another project's session that reused
    the same run id does not match.

    Args:
        jsonl_file: A top-level ``<session-id>.jsonl`` log.
        cycle_id: The PEV cycle id.
        project_root: The project's main checkout, or ``None`` for any project.

    Returns:
        ``True`` when the session orchestrated (part of) the cycle.
    """
    needle = cycle_id.encode("utf-8")
    try:
        with open(jsonl_file, "rb") as f:
            for raw in f:
                if needle not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant":
                    continue
                if project_root is not None and not _record_in_project(rec, jsonl_file, project_root):
                    continue
                for block in extract_tool_calls_from_content(rec.get("message", {}).get("content", [])):
                    if _block_works_on_run(block, cycle_id):
                        return True
    except OSError:
        return False
    return False


def drive_path(path: str) -> str:
    """Return *path* with forward slashes and a Windows drive in place of a Git Bash or WSL mount.

    ``/c/Users/...`` (Git Bash) and ``/mnt/c/Users/...`` (WSL) both become
    ``C:/Users/...``; any other path keeps its form.  No trailing slash.
    """
    norm = path.replace("\\", "/").rstrip("/")
    mount = re.match(r"^/mnt/([A-Za-z])(/|$)", norm) or re.match(r"^/([A-Za-z])(/|$)", norm)
    if mount:
        norm = f"{mount.group(1).upper()}:/{norm[mount.end() :]}".rstrip("/")
    return norm


def normalize_path(path: str) -> str:
    """Return *path* in one comparable form: forward slashes, no trailing slash.

    A Git Bash (``/c/Users/...``) or WSL (``/mnt/c/Users/...``) path becomes a
    drive path, and a drive path is lower-cased because Windows paths compare
    case-insensitively.
    """
    norm = drive_path(path)
    if re.match(r"^[A-Za-z]:", norm):
        norm = norm.lower()
    return norm


def main_checkout(path: str) -> str:
    """Return the main checkout of *path*: a ``.claude/worktrees/<name>`` worktree maps to its project."""
    norm = path.replace("\\", "/")
    marker = "/.claude/worktrees/"
    return path[: norm.index(marker)] if marker in norm else path


def project_slug(path: str) -> str:
    """Return Claude Code's session folder name for *path*: every non-alphanumeric becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", path.replace("\\", "/").rstrip("/"))


def _record_in_project(rec: dict, jsonl_file: Path, project_root: str) -> bool:
    """Whether a session record was written while the session worked in *project_root*.

    The record's ``cwd`` decides: it is the session's directory when the record
    was written, so it must be the project or a path under it (its
    ``.claude/worktrees/`` included).  A record without ``cwd`` falls back to
    the log's folder: the project's own slug, or one of its worktree slugs.
    The slug is built from the root as given and from its drive form, so a
    Git Bash or WSL root (``/c/...``, ``/mnt/c/...``) still matches the
    ``C--Users-...`` folder Claude Code writes on Windows.
    The folder alone is not trusted when ``cwd`` is present, since Claude Code
    moves a log between folders as the session changes directory.
    """
    cwd = rec.get("cwd")
    if isinstance(cwd, str) and cwd:
        here, root = normalize_path(cwd), normalize_path(project_root)
        return here == root or here.startswith(root + "/")
    folder = jsonl_file.parent.name.lower()
    slugs = {project_slug(project_root).lower(), project_slug(drive_path(project_root)).lower()}
    return any(folder == slug or folder.startswith(slug + "--claude-worktrees-") for slug in slugs)


#: Section and doc writes that show a main session working on an audit or an
#: instance itself (an instance has no subagents until its review).
_RUN_WRITE_TOOLS = IMPLEMENTATION_TOOLS | {"mcp__axiom-graph__axiom_graph_clone_doc"}

#: An efficiency report's doc id or file: ``{run-dir}/efficiency[-sN]``, the
#: older ``docs/pev/cycles/efficiency/`` and ``docs/pev/cycles-efficiency/``
#: folders, or a staged ``{run-id}-efficiency[-sN].docjson`` file.
_EFFICIENCY_DOC = re.compile(
    r"[/\\](?:pev-efficiency|cycles-efficiency|efficiency(?:-s\d+)?)(?:[/\\:.]|$)"
    r"|-efficiency(?:-s\d+)?\.(?:docjson|json)\b"
)


def _block_works_on_run(block: dict, run_id: str) -> bool:
    """Whether one tool call works on *run_id*.

    A ``pev-*`` Agent dispatch whose prompt belongs to the run counts for every
    kind.  For an audit or an instance, a doc write naming the run most also
    counts: those runs are worked in the main session.  A read never counts,
    and neither does a write to an efficiency report: writing the report is
    not working the run.
    """
    inp = block.get("input", {})
    if not isinstance(inp, dict):
        return False
    if block.get("name") == "Agent":
        agent_type = _normalize_agent_type(str(inp.get("subagent_type", "")))
        return agent_type.startswith("pev-") and named_cycle(inp.get("prompt", "")) == run_id
    if run_kind(run_id) == "cycle" or block.get("name") not in _RUN_WRITE_TOOLS:
        return False
    if named_cycle(_tool_input_text(inp)) != run_id:
        return False
    return not any(_EFFICIENCY_DOC.search(target) for target in _write_targets(inp))


#: Input fields that name what a write targets (a doc, section, node or file).
_TARGET_FIELDS = ("section_id", "doc_id", "new_id", "node_id", "file_path", "notebook_path", "doc_file")


def _write_targets(inp: dict) -> list[str]:
    """Return the doc, section, node and file ids a write's input targets.

    Only the target fields are read, never the written content, so a write
    whose text mentions an efficiency report is not taken for a write to one.

    Args:
        inp: The tool input: a single write, a batch under ``sections`` /
            ``edits`` / ``links``, ``node_ids``, or ``write_doc``'s ``doc_json``.

    Returns:
        Every target id or path found, in input order.
    """
    items = [inp] + [i for key in ("sections", "edits", "links") for i in (inp.get(key) or []) if isinstance(i, dict)]
    targets = [str(item[k]) for item in items for k in _TARGET_FIELDS if item.get(k)]
    targets += [str(n) for n in inp.get("node_ids") or []]
    doc = inp.get("doc_json")
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except json.JSONDecodeError:
            doc = None
    if isinstance(doc, dict) and doc.get("id"):
        targets.append(str(doc["id"]))
    return targets


def find_sessions_for_run(run_id: str, projects_dir: Path | None = None, project_root: str | None = None) -> list[str]:
    """Find the session logs that worked a PEV cycle, audit or instance, oldest first.

    Same search as :func:`find_sessions_for_cycle`; an audit or instance
    session also matches on a doc write naming the run (old single-file runs
    and run directories alike, since only the id is matched).

    Args:
        run_id: The cycle, audit or instance id.
        projects_dir: Claude Code's projects folder; defaults to
            ``CLAUDE_PROJECTS_DIR``.
        project_root: Keep only sessions that worked the run in this project
            (its main checkout or a worktree under it); ``None`` keeps every
            project's.

    Returns:
        Paths of the matching ``.jsonl`` logs, oldest first.
    """
    return find_sessions_for_cycle(run_id, projects_dir, project_root)


def find_sessions_for_cycle(
    cycle_id: str, projects_dir: Path | None = None, project_root: str | None = None
) -> list[str]:
    """Find the session logs that orchestrated a PEV cycle, oldest first.

    Every project folder is searched, because a session's log follows it into
    and out of worktrees (see "Transcript format").  Subagent transcripts
    (``<session-id>/subagents/*.jsonl``) are not sessions and are skipped.
    Run ids are a date plus a slug, so another project (or an earlier dry run
    in another folder) can reuse one: with *project_root*, only dispatches
    made in that project count (see :func:`_record_in_project`).

    Args:
        cycle_id: The PEV cycle id.
        projects_dir: Claude Code's projects folder; defaults to
            ``CLAUDE_PROJECTS_DIR``.
        project_root: Keep only sessions that worked the cycle in this
            project; ``None`` keeps every project's.

    Returns:
        Paths of the matching ``.jsonl`` logs, ordered by modification time so
        multi-session cycles number their reports chronologically.
    """
    root = projects_dir if projects_dir is not None else CLAUDE_PROJECTS_DIR
    if not root.is_dir():
        return []

    matches = []
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            if session_dispatches_cycle(jsonl_file, cycle_id, project_root):
                matches.append(jsonl_file)

    matches.sort(key=lambda p: (p.stat().st_mtime, str(p)))
    return [str(p) for p in matches]


# ── CLI ──────────────────────────────────────────────────────────────────────


def _write_or_exit(analysis: SessionAnalysis, args: argparse.Namespace, session_index: int | None = None) -> None:
    try:
        path, slug = write_docjson(
            analysis,
            output_dir=args.output_dir,
            session_index=session_index,
            assume_yes=args.yes,
            doc_folder=args.doc_folder,
        )
    except OutputDirMissing as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
    print(f"Staged DocJSON: {path}")
    print(f"  doc slug: {slug}")
    print(f'  write it: axiom_graph_write_doc(project_root, doc_file="{Path(path).as_posix()}")')


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Claude Code session logs for PEV cycle efficiency.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("session_file", nargs="?", help="Path to .jsonl session file")
    parser.add_argument("--cycle", help="Keep only the calls attributed to this PEV cycle ID")
    parser.add_argument(
        "--find-run",
        help="Find and analyze every session that worked this PEV cycle, audit or instance ID",
    )
    parser.add_argument("--find-cycle", help="Same as --find-run, for a cycle ID")
    parser.add_argument(
        "--project-root",
        help="Project whose sessions --find-run / --find-cycle search (default: $CLAUDE_PROJECT_DIR, else the "
        "current directory; a .claude/worktrees/<name> worktree counts as its project)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full tool sequence")
    parser.add_argument("--summary", "-s", action="store_true", help="Compact one-screen summary")
    parser.add_argument(
        "--docjson",
        action="store_true",
        help=f"Stage the report for axiom_graph_write_doc (default staging folder: {DEFAULT_STAGING_DIR})",
    )
    parser.add_argument("--output-dir", help="Staging folder for --docjson")
    parser.add_argument(
        "--doc-folder",
        help="Docs-tree folder of the report's doc slug (default: the run's own directory, "
        "pev/cycles/<id>, pev/audits/<id> or pev/instances/<id>)",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Create a missing --output-dir without asking")
    args = parser.parse_args()

    run_id = args.find_run or args.find_cycle
    if run_id:
        project_root = main_checkout(args.project_root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        print(f"Searching for {run_kind(run_id)}: {run_id} (sessions in {project_root})")
        matches = find_sessions_for_run(run_id, project_root=project_root)
        if not matches:
            print("No sessions found.")
            return
        print(f"Found {len(matches)} session(s):")
        for m in matches:
            print(f"  {m}")
        # Analyze each — use session index for multi-session cycles.  A resumed
        # session repeats the agents of the one it resumed: count each once.
        parsed = [parse_session(m, cycle_filter=run_id) for m in matches]
        results = dedupe_runs(parsed)

        use_index = len(results) > 1
        for i, analysis in enumerate(results):
            print()
            if args.docjson:
                idx = i + 1 if use_index else None
                _write_or_exit(analysis, args, session_index=idx)
            if args.summary:
                print_summary(analysis)
            elif not args.docjson:
                print_analysis(analysis, verbose=args.verbose)
        return

    if not args.session_file:
        parser.error("Either session_file or --find-run / --find-cycle is required")

    if not os.path.exists(args.session_file):
        print(f"File not found: {args.session_file}", file=sys.stderr)
        sys.exit(1)

    analysis = parse_session(args.session_file, cycle_filter=args.cycle)

    if args.docjson:
        _write_or_exit(analysis, args)
    if args.summary:
        print_summary(analysis)
    elif not args.docjson:
        print_analysis(analysis, verbose=args.verbose)


if __name__ == "__main__":
    main()
