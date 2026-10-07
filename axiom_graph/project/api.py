"""Public Python API for the project bounded context.

Reads the facts an agent needs before it builds doc ids or paths for one
project: the id, the resolved root, the docs roots and extensions, the
staleness tags, the index's schema and size, what a build scans and skips,
and the project's agent policy.  The read never writes: it loads
``axiom-graph.toml`` and, when the index file exists, opens one read-only
connection to it.  It never creates the index, migrates it or refreshes
staleness.

The agent policy is either/or: the doc carrying the doc-level
:data:`AGENT_POLICY_TAG` tag, or, with none, axiom-graph's shipped default
(:data:`DEFAULT_POLICY_SECTIONS`).

Public surface:
    ``ProjectFacts``          -- typed result of the read
    ``AgentPolicy``           -- the policy the read resolved
    ``project_facts``         -- read one project's facts and policy
    ``default_policy_doc``    -- the shipped default as a DocJSON dict
    ``find_policy_doc``       -- the doc carrying the policy tag, and its file
    ``seed_policy``           -- write the default as a doc when there is none (init)
    ``restore_policy``        -- overwrite the policy doc with the default (init --policy)
    ``PolicyWrite``           -- what a seed or restore did
    ``AGENT_POLICY_TAG``      -- the doc-level tag that marks a policy doc

Layering invariants (per the architecture policy):
    Allowed imports: ``axiom_graph`` (version), ``axiom_graph.config``,
    ``axiom_graph.db``, ``axiom_graph.index.*``, the doc write path in
    ``axiom_graph.docjson.api`` and stdlib.  Never ``axiom_graph.mcp.*``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from axiom_annotations import AutoStep, Step, task

import axiom_graph
from axiom_graph.config import AxiomGraphConfig, config_scope, db_path_for
from axiom_graph.db import open_connection
from axiom_graph.db.migrations import CURRENT_SCHEMA_VERSION
from axiom_graph.index.builder import base_skip_dirs, resolve_project_id
from axiom_graph.index.carry_forward import index_identity_conn
from axiom_graph.index.doc_ids import (
    derive_doc_id,
    docjson_filename,
    existing_docjson_file,
    strip_docjson_extension,
)

logger = logging.getLogger(__name__)

AGENT_POLICY_TAG = "agent-policy"
"""Doc-level tag that marks a doc as the project's agent policy."""

DEFAULT_POLICY_SLUG = "agent-policy"
"""File stem the shipped default is written under, in the primary docs root."""

DEFAULT_POLICY_TITLE = "Agent policy"

DEFAULT_POLICY_SECTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "start-here",
        "Start here",
        "Call `axiom_graph_guide` and `axiom_graph_info` first. Take the project id, doc paths and "
        "extensions from `axiom_graph_info`; never hard-code them.",
    ),
    (
        "edit-docs-through-the-tools",
        "Edit docs through the tools",
        "Edit DocJSON only through the axiom-graph doc tools or the `axiom-graph` CLI. If a raw edit "
        "already landed, keep it with `axiom_graph_accept_doc_edits` or re-apply it through a tool.",
    ),
    (
        "build-before-doc-writes",
        "Build before doc writes",
        "After code edits, run `axiom_graph_build` before you write or link docs.",
    ),
    (
        "make-it-yours",
        "Make it yours",
        "This doc is the project's agent policy: edit it to change the rules. "
        "`axiom-graph init --policy` restores the shipped default.",
    ),
)
"""The shipped default policy: ``(section id, heading, content)`` per section."""

POLICY_RENDER_LIMIT = 4000
"""Most characters of a policy doc's headings and content :func:`project_facts` returns."""


def default_policy_doc() -> dict:
    """Return the shipped default policy as a DocJSON dict for the doc write path.

    Returns:
        A new dict with ``id`` (the file stem), ``title``, ``tags`` and
        ``sections``.
    """
    return {
        "id": DEFAULT_POLICY_SLUG,
        "title": DEFAULT_POLICY_TITLE,
        "tags": [AGENT_POLICY_TAG],
        "sections": [
            {"id": sid, "heading": heading, "content": content} for sid, heading, content in DEFAULT_POLICY_SECTIONS
        ],
    }


@dataclass(frozen=True)
class PolicySection:
    """One section of a rendered policy.

    Attributes:
        heading: The section heading.
        content: The section's markdown content.
        level: Heading depth; 2 for a top-level section.
    """

    heading: str
    content: str
    level: int = 2


@dataclass(frozen=True)
class AgentPolicy:
    """The agent policy :func:`project_facts` resolved.

    Attributes:
        doc_id: The policy doc's id; ``None`` for the shipped default.
        title: The policy's title.
        sections: Its sections, in document order, cut at
            :data:`POLICY_RENDER_LIMIT` characters.
        truncated: True when the sections were cut at the limit.
        others: Other docs carrying the tag, not shown.
        looked_up: False when there was no index to look a policy doc up in.
    """

    doc_id: str | None
    title: str
    sections: tuple[PolicySection, ...]
    truncated: bool = False
    others: tuple[str, ...] = ()
    looked_up: bool = True

    @property
    def is_default(self) -> bool:
        """True when this is the shipped default rather than a project doc."""
        return self.doc_id is None


def _default_policy(*, looked_up: bool) -> AgentPolicy:
    """Return the shipped default as an :class:`AgentPolicy`.

    Args:
        looked_up: Whether an index was searched for a policy doc first.

    Returns:
        The default policy.
    """
    return AgentPolicy(
        doc_id=None,
        title=DEFAULT_POLICY_TITLE,
        sections=tuple(PolicySection(heading, content) for _sid, heading, content in DEFAULT_POLICY_SECTIONS),
        looked_up=looked_up,
    )


def _policy_doc_ids(conn: sqlite3.Connection) -> list[str]:
    """Return the ids of docs carrying the doc-level policy tag, lowest first.

    Args:
        conn: Read-only connection to the index.

    Returns:
        The matching doc ids, sorted.
    """
    try:
        rows = conn.execute(
            "SELECT id, tags FROM docs WHERE tags LIKE ? ORDER BY id", (f'%"{AGENT_POLICY_TAG}"%',)
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.debug("agent policy lookup skipped: the index has no readable docs table (%s)", exc)
        return []
    found: list[str] = []
    for doc_id, tags in rows:
        try:
            parsed = json.loads(tags or "[]")
        except (TypeError, ValueError) as exc:
            logger.warning("agent policy lookup: %s has unreadable tags %r (%s); skipped", doc_id, tags, exc)
            continue
        if isinstance(parsed, list) and AGENT_POLICY_TAG in parsed:
            found.append(doc_id)
    return found


def _read_policy(conn: sqlite3.Connection) -> AgentPolicy:
    """Resolve the policy on an open index: the tagged doc, else the default.

    Args:
        conn: Read-only connection to the index.

    Returns:
        The lowest-id tagged doc's sections (headings and content only), or
        the shipped default when no doc carries the tag.
    """
    doc_ids = _policy_doc_ids(conn)
    if not doc_ids:
        return _default_policy(looked_up=True)
    doc_id = doc_ids[0]
    title_row = conn.execute("SELECT title FROM docs WHERE id = ?", (doc_id,)).fetchone()
    # Every section id starts with "{doc_id}::"; the half-open range ("::" up
    # to, not including, ":;") selects exactly those ids.
    rows = conn.execute(
        "SELECT title, level_2, doc_level FROM nodes "
        "WHERE subtype = 'docjson_section' AND id >= ? AND id < ? ORDER BY doc_position, id",
        (f"{doc_id}::", f"{doc_id}:;"),
    ).fetchall()
    sections: list[PolicySection] = []
    budget = POLICY_RENDER_LIMIT
    truncated = False
    for heading, content, level in rows:
        heading, content = heading or "", content or ""
        size = len(heading) + len(content)
        if size > budget:
            truncated = True
            if not sections:
                sections.append(PolicySection(heading, content[: max(budget - len(heading), 0)], int(level or 2)))
            break
        sections.append(PolicySection(heading, content, int(level or 2)))
        budget -= size
    return AgentPolicy(
        doc_id=doc_id,
        title=title_row[0] if title_row else doc_id,
        sections=tuple(sections),
        truncated=truncated,
        others=tuple(doc_ids[1:]),
    )


@dataclass(frozen=True)
class ProjectFacts:
    """One project's facts, as :func:`project_facts` read them.

    Attributes:
        version: The installed axiom-graph version.
        project_root: The project root, resolved.
        project_id: The index's stored id; without an index (or an index
            that stores none), the id a build would use.
        indexed: True when the index file exists.
        id_from_index: True when ``project_id`` is the index's stored id.
        toml_project_id: ``axiom-graph.toml``'s ``project_id``, or ``None``.
        schema_version: The index's schema version; ``None`` without an index.
        package_schema_version: The schema version this package writes.
        db_path: The index file's path (it may not exist).
        node_count: Nodes in the index; ``None`` without an index.
        doc_count: Docs in the index; ``None`` without an index.
        docs_dirs: Configured docs roots, relative to the root.
        docs_extensions: DocJSON extensions; the first is the new-doc one.
        config_dirs: Configured agent/config dirs, relative to the root.
        js_paths: Configured JS/TS globs.
        frozen_tags: Doc tags that opt out of LINKED_STALE.
        transitive_tags: Doc tags that opt in to transitive LINKED_STALE.
        skip_dirs: Directory names every build skips, built in.
        exclude_dirs: Directory names the toml adds to the skip set.
        policy: The project's agent policy, or the shipped default.
    """

    version: str
    project_root: Path
    project_id: str
    indexed: bool
    id_from_index: bool
    toml_project_id: str | None
    schema_version: int | None
    package_schema_version: int
    db_path: Path
    node_count: int | None
    doc_count: int | None
    docs_dirs: tuple[str, ...]
    docs_extensions: tuple[str, ...]
    config_dirs: tuple[str, ...]
    js_paths: tuple[str, ...]
    frozen_tags: tuple[str, ...]
    transitive_tags: tuple[str, ...]
    skip_dirs: tuple[str, ...]
    exclude_dirs: tuple[str, ...]

    policy: AgentPolicy

    @property
    def toml_id_disagrees(self) -> bool:
        """True when the toml names an id other than the index's stored one."""
        return self.id_from_index and bool(self.toml_project_id) and self.toml_project_id != self.project_id

    @property
    def schema_differs(self) -> bool:
        """True when the index's schema is not the one this package writes."""
        return self.schema_version is not None and self.schema_version != self.package_schema_version


@task(
    purpose="Read one project's facts and agent policy from its config and, when it exists, its index (read-only)",
    inputs="project_root: the project directory",
    outputs="ProjectFacts: id, root, docs roots and extensions, tags, schema, counts, scan and skip sets, policy",
    critical="Never creates, migrates or refreshes the index: one read-only connection, only when the file exists",
)
def project_facts(project_root: Path | str) -> ProjectFacts:
    """Read one project's facts and its agent policy.

    Args:
        project_root: The project directory.

    Returns:
        The project's facts.  Without an index file the id is the one a
        first build would use, the schema and counts are ``None`` and the
        policy is the shipped default (no policy doc is looked up).
    """
    root = Path(project_root).resolve()

    口 = Step(step_num=1, name="Load config", purpose="Read axiom-graph.toml once and locate the index file")
    with config_scope():
        config = AxiomGraphConfig.load(root)
        db_path = db_path_for(root)

    口 = Step(
        step_num=2,
        name="Read the index identity",
        purpose="Schema version, stored id, counts and the policy doc on one read-only connection, if the file exists",
    )
    schema_version: int | None = None
    stored_id: str | None = None
    node_count: int | None = None
    doc_count: int | None = None
    policy = _default_policy(looked_up=False)
    indexed = db_path.is_file()
    if indexed:
        conn = open_connection(db_path, read_only=True)
        try:
            schema_version, stored_id = index_identity_conn(conn)
            node_count = _count(conn, "nodes")
            doc_count = _count(conn, "docs")
            policy = _read_policy(conn)
        finally:
            conn.close()

    口 = Step(
        step_num=3,
        name="Settle the project id",
        purpose="The stored id when the index has one, otherwise the toml id then the folder name",
    )
    if stored_id:
        project_id = stored_id
    elif indexed:
        # resolve_project_id would open the index a second time; its rule
        # past the stored id is the toml id, then the folder name.
        project_id = config.project_id or root.name
    else:
        project_id = resolve_project_id(root, db_path=db_path, config=config)

    scan = config.scan
    return ProjectFacts(
        version=axiom_graph.__version__,
        project_root=root,
        project_id=project_id,
        indexed=indexed,
        id_from_index=bool(stored_id),
        toml_project_id=config.project_id or None,
        schema_version=schema_version,
        package_schema_version=CURRENT_SCHEMA_VERSION,
        db_path=db_path,
        node_count=node_count,
        doc_count=doc_count,
        docs_dirs=tuple(scan.docs_dirs),
        docs_extensions=tuple(scan.docs_extensions),
        config_dirs=tuple(scan.config_dirs),
        js_paths=tuple(scan.js_paths),
        frozen_tags=tuple(config.staleness.frozen_tags),
        transitive_tags=tuple(config.staleness.transitive_tags),
        skip_dirs=tuple(sorted(base_skip_dirs())),
        exclude_dirs=tuple(scan.exclude_dirs),
        policy=policy,
    )


def _count(conn: sqlite3.Connection, table: str) -> int | None:
    """Return a table's row count, or ``None`` when the index lacks the table.

    Args:
        conn: Read-only connection to the index.
        table: ``"nodes"`` or ``"docs"``.

    Returns:
        The row count, or ``None``.
    """
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.OperationalError as exc:
        logger.debug("project facts: no %s count, the index has no readable %s table (%s)", table, table, exc)
        return None


# ---------------------------------------------------------------------------
# Writing the shipped default as a doc: init's seed and init --policy's restore
# ---------------------------------------------------------------------------


class PolicyWriteError(Exception):
    """The doc write path refused the policy doc; the message is the write's error."""


@dataclass(frozen=True)
class PolicyDoc:
    """A doc carrying the policy tag, as the index records it.

    Attributes:
        doc_id: The doc's id.
        file: The doc's file, absolute.
    """

    doc_id: str
    file: Path


@dataclass(frozen=True)
class PolicyWrite:
    """What :func:`seed_policy` or :func:`restore_policy` did.

    Attributes:
        written: True when the shipped default was written.
        doc_id: The policy doc's id: the one written, or the existing one
            the seed left alone; ``None`` when nothing was written and no
            policy doc exists.
        file: That doc's file, absolute; ``None`` with ``doc_id``.
        note: Why nothing was written, as one sentence; ``None`` otherwise.
    """

    written: bool
    doc_id: str | None = None
    file: Path | None = None
    note: str | None = None


def _root_dir(root: Path, entry: str) -> Path:
    """Return a configured docs root's directory, absolute."""
    path = Path(entry)
    return path if path.is_absolute() else root / path


def relative_to_root(root: Path, path: Path) -> str:
    """Return *path* relative to *root* in POSIX form, or absolute when it lies outside.

    Args:
        root: The project root, resolved.
        path: A file path.

    Returns:
        The path to show a user.
    """
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def find_policy_doc(project_root: Path | str) -> PolicyDoc | None:
    """Return the project's policy doc: the lowest-id doc carrying the policy tag.

    Args:
        project_root: The project directory.

    Returns:
        The doc and its file, or ``None`` when the index is absent or no doc
        carries :data:`AGENT_POLICY_TAG`.
    """
    root = Path(project_root).resolve()
    with config_scope():
        db_path = db_path_for(root)
    if not db_path.is_file():
        return None
    conn = open_connection(db_path, read_only=True)
    try:
        doc_ids = _policy_doc_ids(conn)
        if not doc_ids:
            return None
        row = conn.execute("SELECT file_path FROM docs WHERE id = ?", (doc_ids[0],)).fetchone()
    finally:
        conn.close()
    file = Path(row[0]) if row and row[0] else Path()
    return PolicyDoc(doc_id=doc_ids[0], file=file if file.is_absolute() else root / file)


@task(
    purpose="Write the shipped default policy through the doc write path under a given docs root and slug",
    inputs="root: the project root; docs_root: a configured docs root entry; slug: the doc's path in it",
    outputs="PolicyWrite: the written doc's id and file",
    critical="The one write path: the doc is indexed, stamped and verified, never hand-written",
)
def _write_default(root: Path, docs_root: str, slug: str) -> PolicyWrite:
    """Write the shipped default through the doc write path as ``slug`` under ``docs_root``.

    An existing file for the slug is overwritten in place, keeping its id.

    Args:
        root: The project root, resolved.
        docs_root: The configured docs root entry to write under.
        slug: The doc's path within that root, without an extension.

    Returns:
        The written doc's id and file.  Both come from the target the write
        was given: the file the slug resolves to under ``docs_root``, and the
        id the scanner's own derivation gives that file.

    Raises:
        PolicyWriteError: The write path refused the doc, or reported
            success yet left no file for the slug.
    """
    from axiom_graph.docjson.api import axiom_graph_write_doc  # noqa: PLC0415

    doc = default_policy_doc()
    doc["id"] = slug
    result = axiom_graph_write_doc(str(root), doc, docs_root=docs_root)
    if result.startswith("ERROR"):
        raise PolicyWriteError(result)
    with config_scope():
        config = AxiomGraphConfig.load(root)
        db_path = db_path_for(root)
    docs_dir = _root_dir(root, docs_root)
    file = existing_docjson_file(docs_dir, slug, config.scan.docs_extensions)
    if file is None:
        raise PolicyWriteError(f"the doc write reported success but {docs_root}/{slug} has no file: {result}")
    project_id = resolve_project_id(root, db_path=db_path, config=config)
    doc_id = derive_doc_id(project_id, docs_root, file.relative_to(docs_dir).as_posix())
    return PolicyWrite(written=True, doc_id=doc_id, file=file)


@task(
    purpose="Write the shipped default policy as an editable doc, only when the project has none",
    inputs="project_root: an indexed project",
    outputs="PolicyWrite: the doc written, or why nothing was (a policy doc exists, the target file exists "
    "untagged, or no docs root is configured)",
    critical="Never overwrites a file: an existing, possibly edited, policy doc survives every re-init",
)
def seed_policy(project_root: Path | str) -> PolicyWrite:
    """Write the shipped default policy into the primary docs root, unless the project has one.

    The doc is ``{primary docs root}/agent-policy`` with the first configured
    extension, written through the doc write path so it is indexed, stamped
    and verified.

    Args:
        project_root: The project directory; its index must exist.

    Returns:
        What was done.  Nothing is written when a doc already carries
        :data:`AGENT_POLICY_TAG` (its id and file are returned), when the
        target file exists without the tag, or when ``docs_dirs`` is empty.

    Raises:
        PolicyWriteError: The write path refused the doc.
    """
    root = Path(project_root).resolve()

    口 = Step(step_num=1, name="Find the policy doc", purpose="A doc already carrying the tag is kept as it is")
    existing = find_policy_doc(root)
    if existing is not None:
        return PolicyWrite(written=False, doc_id=existing.doc_id, file=existing.file)

    口 = Step(
        step_num=2,
        name="Resolve the target file",
        purpose="The primary docs root and the default slug; skip when no root is configured or the file exists",
    )
    with config_scope():
        config = AxiomGraphConfig.load(root)
    if not config.scan.docs_dirs:
        return PolicyWrite(written=False, note="axiom-graph.toml configures no docs_dirs to write it in.")
    docs_root = config.scan.docs_dirs[0]
    present = existing_docjson_file(_root_dir(root, docs_root), DEFAULT_POLICY_SLUG, config.scan.docs_extensions)
    if present is not None:
        return PolicyWrite(
            written=False,
            note=f"{relative_to_root(root, present)} exists and is not tagged {AGENT_POLICY_TAG}.",
        )

    口 = AutoStep(step_num=3, name="Write the default")
    written = _write_default(root, docs_root, DEFAULT_POLICY_SLUG)
    return written


@dataclass(frozen=True)
class PolicyResetPlan:
    """What a full reset (``init --all``) will do to the policy, under the default settings.

    Attributes:
        existing: The policy doc in the current index, or ``None``.
        in_place: True when ``existing``'s file lies in a default docs root,
            so the rebuilt index still holds it and it is overwritten in place.
        target: Where the shipped default goes when it is not overwritten in
            place: the default primary docs root's policy file; ``None`` when
            the defaults configure no docs root.
        target_exists: True when a file is already at ``target``.
    """

    existing: PolicyDoc | None
    in_place: bool
    target: Path | None
    target_exists: bool


def plan_policy_reset(project_root: Path | str) -> PolicyResetPlan:
    """Say what a full reset will do to the agent policy, before anything changes.

    A full reset rewrites ``axiom-graph.toml`` to the defaults before it
    rebuilds, so the policy doc is looked for under the default docs roots.

    Args:
        project_root: The project directory.

    Returns:
        The plan; nothing is read beyond the index and the target file.
    """
    root = Path(project_root).resolve()
    existing = find_policy_doc(root)
    defaults = AxiomGraphConfig().scan
    target: Path | None = None
    target_exists = False
    if defaults.docs_dirs:
        docs_dir = _root_dir(root, defaults.docs_dirs[0])
        present = existing_docjson_file(docs_dir, DEFAULT_POLICY_SLUG, defaults.docs_extensions)
        target = present or docs_dir / docjson_filename(DEFAULT_POLICY_SLUG, defaults.docs_extensions)
        target_exists = present is not None
    in_place = False
    if existing is not None and existing.file.suffix.lower() in {e.lower() for e in defaults.docs_extensions}:
        for entry in defaults.docs_dirs:
            try:
                existing.file.resolve().relative_to(_root_dir(root, entry).resolve())
            except ValueError:
                continue
            in_place = True
    return PolicyResetPlan(existing=existing, in_place=in_place, target=target, target_exists=target_exists)


@task(
    purpose="Overwrite the project's policy doc with the shipped default, keeping its id; write it when absent",
    inputs="project_root: an indexed project",
    outputs="PolicyWrite: the doc written, or why nothing was (as seed_policy, when no policy doc exists)",
    critical="Overwrites only the doc carrying the policy tag; touches nothing else in the index",
)
def restore_policy(project_root: Path | str) -> PolicyWrite:
    """Write the shipped default over the project's policy doc, or seed it when there is none.

    Args:
        project_root: The project directory; its index must exist.

    Returns:
        What was done.  With a policy doc, its file is rewritten with the
        shipped default under the same id.  Without one, as
        :func:`seed_policy`.

    Raises:
        PolicyWriteError: The write path refused the doc, or the policy
            doc's file lies under no configured docs root.
    """
    root = Path(project_root).resolve()

    口 = Step(step_num=1, name="Find the policy doc", purpose="The lowest-id doc carrying the tag")
    existing = find_policy_doc(root)
    if existing is None:
        口 = AutoStep(step_num=2, name="Seed the default")
        seeded = seed_policy(root)
        return seeded

    口 = Step(
        step_num=3,
        name="Locate it in its docs root",
        purpose="The deepest configured root holding the file, and the file's slug within it",
    )
    with config_scope():
        config = AxiomGraphConfig.load(root)
    target: tuple[str, str] | None = None
    depth = -1
    for entry in config.scan.docs_dirs:
        base = _root_dir(root, entry).resolve()
        try:
            rel = existing.file.resolve().relative_to(base)
        except ValueError:
            continue
        if len(base.parts) > depth:
            depth = len(base.parts)
            target = (entry, strip_docjson_extension(rel.as_posix()))
    if target is None:
        raise PolicyWriteError(f"{existing.doc_id}'s file {existing.file} lies under no configured docs root")

    口 = AutoStep(step_num=4, name="Overwrite it with the default")
    written = _write_default(root, target[0], target[1])
    return written
