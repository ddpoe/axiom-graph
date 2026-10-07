"""MCP wire surface for the project bounded context.

Formats :func:`axiom_graph.project.api.project_facts` as short plain-text
lines, one fact per line, followed by the project's agent policy (its doc,
or the shipped default).  ``axiom_graph.mcp.server`` registers
:func:`axiom_graph_info` as a thin ``_timed_tool`` wrapper.
"""

from __future__ import annotations

from pathlib import Path

from axiom_graph.config import ConfigError
from axiom_graph.project.api import AGENT_POLICY_TAG, AgentPolicy, ProjectFacts, project_facts

_LABEL_WIDTH = 17

FROZEN_GLOSS = "-> write-once docs: their sections never receive LINKED_STALE; BROKEN_LINK still shows"
"""What a frozen tag does, shown under ``frozen_tags``."""

TRANSITIVE_GLOSS = "-> docs whose sections turn LINKED_STALE when a doc section they link to goes stale"
"""What a transitive tag does, shown under ``transitive_tags``."""


def _row(label: str, value: str) -> str:
    """Return one ``label  value`` line, the value in a fixed column."""
    return f"{label.ljust(_LABEL_WIDTH)}{value}"


def _gloss(text: str) -> str:
    """Return a gloss line indented to the value column."""
    return " " * _LABEL_WIDTH + text


def _listed(values: tuple[str, ...], empty: str = "none") -> str:
    """Return *values* comma-separated, or *empty* when there are none."""
    return ", ".join(values) if values else empty


def _header(facts: ProjectFacts) -> str:
    """Return the version line, with the index schema when there is an index."""
    line = f"axiom-graph {facts.version}"
    if facts.schema_version is None:
        return line
    line += f"   index schema {facts.schema_version}"
    if facts.schema_differs:
        expected = facts.package_schema_version
        if facts.schema_version < expected:
            line += f"   (package expects {expected}; the next build migrates the index)"
        else:
            line += f"   (package expects {expected}; the index is newer: upgrade axiom-graph)"
    return line


def _project_id(facts: ProjectFacts) -> str:
    """Return the project id value, with a note when it is not plainly the stored id."""
    if not facts.indexed:
        return f"{facts.project_id}   (no index yet; run build)"
    if not facts.id_from_index:
        return f"{facts.project_id}   (the index stores no id; this is the toml id or the folder name)"
    if facts.toml_id_disagrees:
        return (
            f'{facts.project_id}   (axiom-graph.toml says "{facts.toml_project_id}"; build refuses until the two match)'
        )
    return facts.project_id


def _docs_extensions(facts: ProjectFacts) -> str:
    """Return the extensions, naming the one new docs are written with."""
    if not facts.docs_extensions:
        return "none"
    return f"{', '.join(facts.docs_extensions)}   (new docs: {facts.docs_extensions[0]})"


def _db_path(facts: ProjectFacts) -> str:
    """Return the db path, with its node and doc counts when it exists."""
    if not facts.indexed:
        return f"{facts.db_path}   (not created yet)"
    nodes = "?" if facts.node_count is None else facts.node_count
    docs = "?" if facts.doc_count is None else facts.doc_count
    return f"{facts.db_path}   ({nodes} nodes, {docs} docs)"


def _scanned(facts: ProjectFacts) -> str:
    """Return the roots and globs a build walks."""
    return "; ".join(
        [
            "Python under the project root",
            f"js_paths {_listed(facts.js_paths)}",
            f"docs_dirs {_listed(facts.docs_dirs)}",
            f"config_dirs {_listed(facts.config_dirs)}",
        ]
    )


def _excluded(facts: ProjectFacts) -> str:
    """Return the skipped directory names: built in, then the toml's."""
    text = f"{', '.join(facts.skip_dirs)} (built in)"
    if facts.exclude_dirs:
        text += f"; {', '.join(facts.exclude_dirs)} (exclude_dirs)"
    return text


def format_facts(facts: ProjectFacts) -> str:
    """Return the facts block: a version line, then one ``label  value`` line per fact.

    Args:
        facts: The project's facts.

    Returns:
        Plain-text lines, no table.
    """
    lines = [
        _header(facts),
        _row("project id", _project_id(facts)),
        _row("project root", str(facts.project_root)),
        _row("docs_dirs", _listed(facts.docs_dirs)),
        _row("docs_extensions", _docs_extensions(facts)),
        _row("config_dirs", _listed(facts.config_dirs)),
        _row("frozen_tags", _listed(facts.frozen_tags)),
        _gloss(FROZEN_GLOSS),
        _row("transitive_tags", _listed(facts.transitive_tags)),
        _gloss(TRANSITIVE_GLOSS),
        _row("db path", _db_path(facts)),
        _row("scanned", _scanned(facts)),
        _row("excluded", _excluded(facts)),
    ]
    return "\n".join(lines)


def _policy_source(policy: AgentPolicy) -> str:
    """Return the policy block's header line: where the policy comes from."""
    if policy.doc_id is not None:
        return _row("agent policy", policy.doc_id)
    if not policy.looked_up:
        return _row(
            "agent policy",
            f"axiom-graph's shipped default (no index yet: a doc tagged {AGENT_POLICY_TAG} "
            "is picked up after the first build)",
        )
    return _row(
        "agent policy",
        f"no {AGENT_POLICY_TAG} doc in this project; showing axiom-graph's shipped default. "
        "Run `axiom-graph init --policy` to add an editable copy.",
    )


def format_policy(policy: AgentPolicy) -> str:
    """Return the policy block: a source line, then the policy's headings and content.

    Args:
        policy: The resolved policy.

    Returns:
        Plain text: no linked-node footers, no staleness badges.
    """
    lines = [_policy_source(policy)]
    if policy.others:
        lines.append(_row("", f"also tagged {AGENT_POLICY_TAG}, not shown: {', '.join(policy.others)}"))
    lines.append("")
    lines.append(f"# {policy.title}")
    for section in policy.sections:
        lines.append(f"{'#' * max(section.level, 2)} {section.heading}")
        if section.content.strip():
            lines.append(section.content.strip())
    if policy.truncated:
        lines.append(f'... (cut; read the rest with read_doc(doc_id="{policy.doc_id}"))')
    return "\n".join(lines)


def axiom_graph_info(project_root: str) -> str:
    """Project facts (id, root, docs roots, extensions): call before building ids.

    One fact per line: axiom-graph and index schema versions, the project id
    the index stores, the resolved root, ``docs_dirs``, ``docs_extensions``
    (and the one new docs are written with), ``config_dirs``, ``frozen_tags``
    and ``transitive_tags`` with what each does, the db path with its node and
    doc counts, and what a build scans and skips.  Then the project's agent
    policy: the doc tagged ``agent-policy`` or, with none, axiom-graph's
    shipped default, labelled as such.  Read-only: before the first build it
    answers from ``axiom-graph.toml`` and creates no index.

    Args:
        project_root: Absolute path to the project.

    Returns:
        Plain-text lines, one fact per line, or ``ERROR: ...``.
    """
    root = Path(project_root)
    if not root.is_dir():
        return f"ERROR: project_root is not a directory: {project_root}"
    try:
        facts = project_facts(root)
    except ConfigError as exc:
        return f"ERROR: axiom-graph.toml: {exc}"
    return "\n\n".join([format_facts(facts), format_policy(facts.policy)])
