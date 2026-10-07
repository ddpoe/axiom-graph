"""Axiom-graph DB: staleness persistence + computed query helpers.

Covers two-column staleness persistence (``persist_staleness``,
``get_all_staleness``) plus the computed staleness queries consumed by
``compute_staleness`` (``get_stale_doc_sections``,
``get_stale_annotated_nodes``,
``get_stale_workflow_envelopes_via_delegates``, ``get_stale_tests``).

Also exposes file mtime lookups (``get_file_mtime``,
``get_all_file_mtimes``) which sit alongside staleness because they
feed the same compute path.

Drift-query helpers (``parse_drift_filter``, ``query_drift_rows``,
``query_drift_counts_*``, ``query_drift_ids_*``) provide filtered,
paginated, and grouped projections over the persisted staleness
columns.  They share the ``parse_drift_filter`` vocab parser with
``axiom_graph_check``.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from pathlib import Path

from axiom_annotations import task

from axiom_graph.db._core import _connect
from axiom_graph.db.history import effective_change_rows_conn
from axiom_graph.index.dependency_set import LazyDependencyGraph, delegates_closure, warm_delegates_closures
from axiom_graph.index.status import (
    BROKEN_LINK,
    CONTENT_UPDATED,
    DESC_UPDATED,
    LINKED_STALE,
    LINK_PROBLEM_STATUSES,
    NOT_FOUND,
    OWN_PROBLEM_STATUSES,
    RENAMED,
    VERIFIED,
)


# ---------------------------------------------------------------------------
# File mtime lookups
# ---------------------------------------------------------------------------


def get_file_mtime(db_path: Path, location: str) -> float | None:
    """Return the stored file_mtime for this location, or None.

    A location can carry a stored mtime on more than one row — a Markdown
    file stamps its file node and every section node.  ``MAX`` is what makes
    this point lookup agree with :func:`get_all_file_mtimes` for those
    locations; an arbitrary row would let the two readers disagree about the
    same file.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(file_mtime) AS mtime FROM nodes WHERE location = ? AND file_mtime IS NOT NULL",
            (location,),
        ).fetchone()
        return row["mtime"] if row else None


def get_all_file_mtimes(db_path: Path) -> dict[str, float]:
    """Return {location: file_mtime} for all nodes with a stored mtime (single query)."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT location, MAX(file_mtime) AS mtime FROM nodes WHERE file_mtime IS NOT NULL GROUP BY location"
        ).fetchall()
        return {r["location"]: r["mtime"] for r in rows}


# ---------------------------------------------------------------------------
# Staleness persistence (two-column)
# ---------------------------------------------------------------------------


def persist_staleness(
    db_path: Path,
    statuses: dict[str, str | tuple[str, str]],
) -> int:
    """Write computed staleness values to the nodes table.

    Accepts either the legacy single-string format or the new two-column
    tuple format ``(own_status, link_status)``.  When a tuple is provided
    both ``own_status`` and ``link_status`` are updated; the legacy
    ``staleness`` column receives the higher-severity value for backward
    compatibility with any readers that still inspect it.

    Returns the number of rows updated.
    """
    if not statuses:
        return 0
    with _connect(db_path) as conn:
        updated = 0
        for node_id, status in statuses.items():
            if isinstance(status, tuple):
                own, link = status
                cur = conn.execute(
                    "UPDATE nodes SET own_status = ?, link_status = ? WHERE id = ?",
                    (own, link, node_id),
                )
            else:
                # Single-string caller: treat as own_status only
                cur = conn.execute(
                    "UPDATE nodes SET own_status = ? WHERE id = ?",
                    (status, node_id),
                )
            updated += cur.rowcount
        return updated


def get_all_staleness(db_path: Path) -> dict[str, tuple[str, str]]:
    """Read the persisted two-column staleness for all nodes.

    Returns a dict mapping ``node_id -> (own_status, link_status)``.
    """
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT id, own_status, link_status FROM nodes").fetchall()
        return {r["id"]: (r["own_status"], r["link_status"]) for r in rows}


def count_status_pairs_conn(conn) -> dict[tuple[str, str], int]:
    """Count the stored ``(own_status, link_status)`` pairs over every node (one aggregate query).

    Args:
        conn: Open connection.

    Returns:
        ``{(own_status, link_status): node count}``.
    """
    rows = conn.execute("SELECT own_status, link_status, COUNT(*) FROM nodes GROUP BY own_status, link_status")
    return {(r[0], r[1]): int(r[2]) for r in rows}


def get_staleness_for_conn(conn, node_ids: Collection[str]) -> dict[str, tuple[str, str]]:
    """Read the stored ``(own_status, link_status)`` of *node_ids* (ids with no row are left out).

    Args:
        conn: Open connection.
        node_ids: The nodes to read.

    Returns:
        ``{node_id: (own_status, link_status)}``.
    """
    out: dict[str, tuple[str, str]] = {}
    for chunk in _id_chunks(node_ids):
        rows = conn.execute(
            f"SELECT id, own_status, link_status FROM nodes WHERE id IN ({','.join('?' * len(chunk))})",
            chunk,
        )
        out.update({r[0]: (r[1], r[2]) for r in rows})
    return out


def get_ordered_staleness_conn(conn, *, problems_only: bool) -> list[tuple[str, str, str]]:
    """Read stored statuses in node-table order: every node, or only those not VERIFIED in a dimension.

    The order is the order ``SELECT * FROM nodes`` lists the nodes in, which
    is what ``check`` prints its rows in.

    Args:
        conn: Open connection.
        problems_only: Keep only nodes whose own or link status is not VERIFIED.

    Returns:
        ``[(node_id, own_status, link_status), ...]``.
    """
    where = " WHERE own_status != ? OR link_status != ?" if problems_only else ""
    params = (VERIFIED, VERIFIED) if problems_only else ()
    return [
        (r[0], r[1], r[2])
        for r in conn.execute(f"SELECT id, own_status, link_status FROM nodes{where} ORDER BY rowid", params)
    ]


BROKEN_LINK_EDGE_TYPES: tuple[str, ...] = ("documents", "validates", "delegates_to")
"""Link types whose dangling target makes the link's charged source BROKEN_LINK (see :func:`charged_source_sql`)."""

DANGLING_LINK_EDGE_TYPES: tuple[str, ...] = (*BROKEN_LINK_EDGE_TYPES, "annotates")
"""Link types whose dangling target can move the charged source's link status: the broken-link types and ``annotates``."""


def charged_source_sql(edge: str = "e", source: str = "src") -> str:
    """Return the SQL expression naming the node a dangling link out of *edge* is charged to.

    A link leaving a ``step`` / ``autostep`` is charged to the ``workflow`` /
    ``task`` envelope that composes the step (the smallest id when several
    do): staleness gives step nodes a blanket VERIFIED, and the envelope is
    the node a maintainer acts on.  A step no envelope composes keeps the
    link, and so does any other source.

    Args:
        edge: Alias of the ``edges`` row of the link.
        source: Alias of the ``nodes`` row of the link's source (may be a
            LEFT JOIN that matched nothing).

    Returns:
        A SQL expression over *edge* and *source*.
    """
    return f"""CASE
                    WHEN {source}.subtype IN ('step', 'autostep') THEN COALESCE(
                        (SELECT c.from_id
                           FROM edges c
                           JOIN nodes env ON env.id = c.from_id
                          WHERE c.edge_type = 'composes'
                            AND c.to_id = {edge}.from_id
                            AND env.subtype IN ('workflow', 'task')
                          ORDER BY c.from_id
                          LIMIT 1),
                        {edge}.from_id
                    )
                    ELSE {edge}.from_id
                END"""


def unflagged_dangling_sources_conn(
    conn, deleted_after: int | None = None, deleted_through: int | None = None
) -> set[str]:
    """Return the charged sources of dependency links whose target has no node row, not yet stored BROKEN_LINK.

    A writer that deletes a node row (a retired doc section) leaves no
    journal row; these are the nodes whose link status that deletion can
    move, so an incremental refresh evaluates them.  The links looked at are
    :data:`DANGLING_LINK_EDGE_TYPES`, and each is charged to a node the way
    the broken-link rule charges it (:func:`charged_source_sql`), so a
    step's dangling delegate link names its envelope.

    With *deleted_after* only the links into the ids the deletion log holds
    past that mark are looked at (the deletions since the last refresh that
    consumed the log), through the ``edges.to_id`` index: a link that
    dangled before the mark had its source evaluated by that refresh.
    ``None`` (no mark stored yet) looks at every link.

    Args:
        conn: Open connection.
        deleted_after: The last ``node_deletion_log.id`` already consumed, or
            ``None`` for every link.
        deleted_through: The newest log id to read (the one the caller
            stores as the new mark); ``None`` for no bound.

    Returns:
        Charged source node ids.
    """
    types = ", ".join("?" * len(DANGLING_LINK_EDGE_TYPES))
    dangling = (
        f"e.edge_type IN ({types}) AND s.link_status != ? AND NOT EXISTS (SELECT 1 FROM nodes t WHERE t.id = e.to_id)"
    )
    charged = f"JOIN nodes src ON src.id = e.from_id CROSS JOIN nodes s ON s.id = ({charged_source_sql()})"
    if deleted_after is None:
        rows = conn.execute(
            f"SELECT DISTINCT s.id FROM edges e {charged} WHERE {dangling}",  # noqa: S608 - fixed text
            (*DANGLING_LINK_EDGE_TYPES, BROKEN_LINK),
        )
        return {r[0] for r in rows}
    bound = "" if deleted_through is None else " AND id <= ?"
    rows = conn.execute(
        "SELECT DISTINCT s.id FROM "
        f"(SELECT DISTINCT node_id FROM node_deletion_log WHERE id > ?{bound}) d "  # noqa: S608 - fixed text
        # CROSS JOIN fixes the order: the few logged ids drive the edges index.
        f"CROSS JOIN edges e ON e.to_id = d.node_id CROSS {charged} WHERE {dangling}",
        (
            deleted_after,
            *(() if deleted_through is None else (deleted_through,)),
            *DANGLING_LINK_EDGE_TYPES,
            BROKEN_LINK,
        ),
    )
    return {r[0] for r in rows}


def count_nodes_conn(conn) -> int:
    """Return how many nodes the index holds.

    Args:
        conn: Open connection.

    Returns:
        The node count.
    """
    return int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])


# ---------------------------------------------------------------------------
# Computed staleness queries
# ---------------------------------------------------------------------------


def _id_chunks(ids, size: int = 500):
    """Yield *ids* (deduplicated, sorted) in chunks small enough for one ``IN (...)`` list."""
    ordered = sorted(set(ids))
    for start in range(0, len(ordered), size):
        yield ordered[start : start + size]


@task(
    purpose="Find doc sections whose linked code node has a change that still counts — Pass 1 candidates for LINKED_STALE, for every section or only the given ones; Pass 2 settles a via by its verified-against pair when the section's verification recorded one, else by the verification clock",
    inputs="db_path, optional section ids (the scope)",
    outputs="List of dicts with section_id, doc_id, heading, code_node_id, code_changed_at, section_updated_at, sorted by section and code node",
)
def get_stale_doc_sections(
    db_path: Path,
    realigned_now: frozenset[str] | set[str] = frozenset(),
    section_ids: Collection[str] | None = None,
) -> list[dict]:
    """Return doc sections linked to code that has drifted since the last verification.

    LINKED_STALE is sticky. Pass 1 of ``_get_linked_stale_ids`` calls this
    helper to collect every section whose linked code node has at least
    one ``CONTENT_ONLY`` / ``CONTENT_AND_DESC`` / ``BECAME_CONTENT_UPDATED``
    history row. The query intentionally does NOT compare ``nh.scanned_at``
    against ``doc_sections.updated_at``: editing a section is not the same
    as verifying that its prose still matches the linked code, so an edit
    must not auto-clear LINKED_STALE.

    The only mechanism that clears LINKED_STALE is Pass 2 of
    ``_get_linked_stale_ids``, which settles each via against the section's
    verification and drops the section once no via remains.  A via the
    verification recorded a pair for (the hash of the target it was
    checked against) is settled by hash equality: dropped while the
    recorded hash equals the target's live hash, kept while they differ,
    whatever the change times say.  Only a via with no recorded pair falls
    back to the clock: dropped when its change (``code_changed_at`` below)
    is not newer than the section's ``node_verification.verified_at``.
    A recorded pair that no longer matches flags the section through
    Pass P even when this query reports no change.  See ADR-018.

    The linked node's change time is its latest code change that still
    counts (:func:`axiom_graph.db.history.effective_change_rows_conn`): a
    change that ended back at its baseline is not a change.

    Args:
        db_path: Path to the axiom-graph DB.
        realigned_now: Nodes the caller has just observed back at their
            baseline, ahead of the history row that records it.
        section_ids: Only these sections (the scoped refresh's evaluation
            set); ``None`` means every section.  The rule is the same.

    Each dict has: section_id, doc_id, heading, code_node_id,
    code_changed_at, section_updated_at.  Rows are sorted by section, then
    code node, so a scoped call lists a section's rows in the same order
    as the full one.
    """
    from axiom_graph.db.docs import _section_filter_sql, split_section_id  # noqa: PLC0415

    sql = f"""
            SELECT
                s.id          AS section_id,
                s.level_1     AS heading,
                e.to_id       AS code_node_id,
                s.updated_at  AS section_updated_at
            FROM nodes s
            JOIN edges e          ON e.from_id = s.id AND e.edge_type = 'documents'
            JOIN nodes code_n     ON code_n.id = e.to_id AND NOT (code_n.node_type = 'atomic_process' AND COALESCE(code_n.subtype, '') IN ('docjson', 'docjson_section'))
            WHERE {_section_filter_sql("s.")}
            """
    with _connect(db_path) as conn:
        if section_ids is None:
            rows = conn.execute(sql).fetchall()
        else:
            rows = []
            for chunk in _id_chunks(section_ids):
                rows.extend(conn.execute(f"{sql} AND s.id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
        changes = effective_change_rows_conn(conn, [r["code_node_id"] for r in rows], realigned_now=realigned_now)
    out: list[dict] = []
    for r in rows:
        change = changes.get(r["code_node_id"])
        if change is None:
            continue
        d = dict(r)
        d["code_changed_at"] = change[1]
        d["doc_id"] = split_section_id(d["section_id"])[0]
        out.append(d)
    out.sort(key=lambda d: (d["section_id"], d["code_node_id"]))
    return out


@task(
    purpose="Find annotation-envelope nodes whose annotated target drifted (code OR docstring) after the envelope was last updated — Pass A candidates for LINKED_STALE, for every envelope or only the given ones; Pass 2 settles a via by its verified-against pair when one is recorded, else by the verification clock",
    inputs="db_path, optional envelope ids (the scope)",
    outputs="List of dicts with envelope_id, target_id, change_at, envelope_updated_at, sorted by envelope and target",
)
def get_stale_annotated_nodes(
    db_path: Path,
    realigned_now: frozenset[str] | set[str] = frozenset(),
    envelope_ids: Collection[str] | None = None,
) -> list[dict]:
    """Return envelope nodes whose annotated target changed after the envelope was last updated.

    Joins ``nodes`` (envelope) -> ``edges`` (``edge_type = 'annotates'``) ->
    ``node_history`` of the target, widened to include ``DESC_ONLY`` so
    pure-docstring drift on the target still flips the envelope.

    The target's change time is its latest change that still counts
    (:func:`axiom_graph.db.history.effective_change_rows_conn`, widened).

    This is the Pass A query behind ``annotates`` staleness.  It only
    proposes candidates: Pass 2 of ``_get_linked_stale_ids`` settles a via
    the envelope's verification recorded a pair for by hash equality (code
    and docstring hash), and falls back to the verification clock only for
    a via with no recorded pair.  Each dict has keys: ``envelope_id``,
    ``target_id``, ``change_at``, ``envelope_updated_at``.

    Args:
        db_path: Path to the axiom-graph DB.
        realigned_now: Nodes the caller has just observed back at their
            baseline, ahead of the history row that records it.
        envelope_ids: Only these sources (the scoped refresh's evaluation
            set); ``None`` means every one.  The rule is the same.
    """
    sql = """
            SELECT
                e.from_id     AS envelope_id,
                e.to_id       AS target_id,
                n.updated_at  AS envelope_updated_at
            FROM edges e
            JOIN nodes n          ON n.id = e.from_id
            WHERE e.edge_type = 'annotates'
            """
    with _connect(db_path) as conn:
        if envelope_ids is None:
            rows = conn.execute(sql).fetchall()
        else:
            rows = []
            for chunk in _id_chunks(envelope_ids):
                rows.extend(conn.execute(f"{sql} AND e.from_id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
        changes = effective_change_rows_conn(
            conn, [r["target_id"] for r in rows], include_desc=True, realigned_now=realigned_now
        )
    out: list[dict] = []
    for r in rows:
        change = changes.get(r["target_id"])
        if change is None or not change[1] > (r["envelope_updated_at"] or ""):
            continue
        d = dict(r)
        d["change_at"] = change[1]
        out.append(d)
    out.sort(key=lambda d: (d["envelope_id"], d["target_id"]))
    return out


@task(
    purpose="Find workflow/task envelopes whose delegates_to chain transitively reaches a CONTENT-changed task — Pass B candidates for LINKED_STALE, for every envelope or only the given ones, cycle-guarded, folding history only for the tasks the closures reach; Pass 2 settles a via by its verified-against pair when one is recorded, else by the verification clock",
    inputs="db_path, optional envelope ids (the scope)",
    outputs="List of dicts with envelope_id, via_task_id, envelope_updated_at, change_at, sorted by envelope and task",
)
def get_stale_workflow_envelopes_via_delegates(
    db_path: Path,
    realigned_now: frozenset[str] | set[str] = frozenset(),
    envelope_ids: Collection[str] | None = None,
) -> list[dict]:
    """Return envelopes whose delegates_to closure includes a CONTENT-changed task.

    Walks the transitive closure of ``composes`` → ``autostep`` →
    ``delegates_to`` → ``annotates`` (inbound) for every envelope, with a
    per-envelope visited-task set for cycle safety.  Only CODE changes
    propagate: ``DESC_ONLY`` is excluded (Pass A catches it on the task's
    own envelope).

    The closures are walked first and the change history is folded only
    for the tasks they reach, so the cost follows the closures, never the
    size of the history.  A task's change time is its latest code change
    that still counts (:func:`axiom_graph.db.history.effective_change_rows_conn`).

    The rows are candidates only: Pass 2 of ``_get_linked_stale_ids``
    settles a via the envelope's verification recorded a pair for by hash
    equality, and falls back to the verification clock only for a via with
    no recorded pair.  The closure is the shared
    :func:`~axiom_graph.index.dependency_set.delegates_closure` walk, so the
    tasks reported here are the ones a verification records pairs for.

    Args:
        db_path: Path to the axiom-graph DB.
        realigned_now: Nodes the caller has just observed back at their
            baseline, ahead of the history row that records it.
        envelope_ids: Only these envelopes (the scoped refresh's evaluation
            set; ids that are not workflow / task envelopes are ignored);
            ``None`` means every one.  The rule is the same.

    Each dict has: ``envelope_id``, ``via_task_id``, ``envelope_updated_at``,
    ``change_at``.
    """
    closures: dict[str, list[str]] = {}
    updated: dict[str, str] = {}
    with _connect(db_path) as conn:
        if envelope_ids is None:
            composes_out: dict[str, list[str]] = {}
            for r in conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'composes'"):
                composes_out.setdefault(r["from_id"], []).append(r["to_id"])
            delegates_out: dict[str, str] = {}
            for r in conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'delegates_to'"):
                # AutoStep has at most one delegates_to edge by construction.
                delegates_out[r["from_id"]] = r["to_id"]
            annotates_rev: dict[str, list[str]] = {}
            for r in conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'annotates'"):
                annotates_rev.setdefault(r["to_id"], []).append(r["from_id"])
            node_subtype = {r["id"]: r["subtype"] for r in conn.execute("SELECT id, subtype FROM nodes")}
            for r in conn.execute(
                "SELECT id, updated_at FROM nodes WHERE node_type = 'composite_process' AND subtype IN ('workflow', 'task')"
            ):
                updated[r["id"]] = r["updated_at"]
                closures[r["id"]] = delegates_closure(
                    r["id"], composes_out, delegates_out, annotates_rev, node_subtype.get
                )
        else:
            graph = LazyDependencyGraph(conn)
            for chunk in _id_chunks(envelope_ids):
                for r in conn.execute(
                    "SELECT id, updated_at FROM nodes WHERE node_type = 'composite_process' "
                    f"AND subtype IN ('workflow', 'task') AND id IN ({','.join('?' * len(chunk))})",
                    chunk,
                ):
                    updated[r["id"]] = r["updated_at"]
            warm_delegates_closures(graph, sorted(updated))
            for env_id in sorted(updated):
                closures[env_id] = delegates_closure(
                    env_id, graph.composes_out, graph.delegates_out, graph.annotates_rev, graph.subtype_of
                )
        tasks = {t for ts in closures.values() for t in ts}
        # Latest CODE change that still counts, for the closure tasks only.
        effective = effective_change_rows_conn(conn, sorted(tasks), realigned_now=realigned_now) if tasks else {}

    latest_code_change: dict[str, str] = {nid: at for nid, (_hid, at) in effective.items()}
    results: list[dict] = []
    for env_id in sorted(closures):
        env_updated = updated[env_id]
        for task_id in closures[env_id]:
            change_at = latest_code_change.get(task_id)
            if change_at and change_at > env_updated:
                results.append(
                    {
                        "envelope_id": env_id,
                        "via_task_id": task_id,
                        "envelope_updated_at": env_updated,
                        "change_at": change_at,
                    }
                )
    results.sort(key=lambda d: (d["envelope_id"], d["via_task_id"]))
    return results


@task(
    purpose="Find test nodes linked via 'validates' to code with a change that still counts — Pass 1 candidates for LINKED_STALE, for every test or only the given ones; Pass 2 settles a via by its verified-against pair when the test's verification recorded one, else by the verification clock",
    inputs="db_path, optional test ids (the scope)",
    outputs="List of dicts with test_node_id, code_node_id, code_changed_at, test_updated_at, sorted by test and code node",
)
def get_stale_tests(
    db_path: Path,
    realigned_now: frozenset[str] | set[str] = frozenset(),
    test_ids: Collection[str] | None = None,
) -> list[dict]:
    """Return test nodes linked to code that has drifted since the last verification.

    LINKED_STALE is sticky. Pass 1 of ``_get_linked_stale_ids`` calls this
    helper to collect every test whose ``validates`` target has at least
    one ``CONTENT_ONLY`` / ``CONTENT_AND_DESC`` / ``BECAME_CONTENT_UPDATED``
    history row. The query intentionally does NOT compare ``nh.scanned_at``
    against ``t.updated_at``: editing a test file is not the same as
    re-running the test against the new code, so an edit must not
    auto-clear LINKED_STALE.

    The only mechanism that clears LINKED_STALE is Pass 2 of
    ``_get_linked_stale_ids``, which settles each via against the test's
    verification and drops the test once no via remains.  A via the
    verification recorded a pair for (the hash of the target it was
    checked against) is settled by hash equality: dropped while the
    recorded hash equals the target's live hash, kept while they differ,
    whatever the change times say.  Only a via with no recorded pair falls
    back to the clock: dropped when its change (``code_changed_at`` below)
    is not newer than the test's ``node_verification.verified_at``.  A
    recorded pair that no longer matches flags the test through Pass P even
    when this query reports no change.  See ADR-018.

    The validated node's change time is its latest code change that still
    counts (:func:`axiom_graph.db.history.effective_change_rows_conn`): a
    change that ended back at its baseline is not a change.

    Args:
        db_path: Path to the axiom-graph DB.
        realigned_now: Nodes the caller has just observed back at their
            baseline, ahead of the history row that records it.
        test_ids: Only these tests (the scoped refresh's evaluation set);
            ``None`` means every test.  The rule is the same.

    Each dict has: test_node_id, code_node_id, code_changed_at, test_updated_at.
    """
    sql = """
            SELECT
                t.id          AS test_node_id,
                e.to_id       AS code_node_id,
                t.updated_at  AS test_updated_at
            FROM nodes t
            JOIN edges e         ON e.from_id = t.id AND e.edge_type = 'validates'
            JOIN nodes code_n    ON code_n.id = e.to_id AND NOT (code_n.node_type = 'atomic_process' AND COALESCE(code_n.subtype, '') IN ('docjson', 'docjson_section'))
            WHERE t.node_type = 'atomic_process'
              AND t.subtype   = 'test'
            """
    with _connect(db_path) as conn:
        if test_ids is None:
            rows = conn.execute(sql).fetchall()
        else:
            rows = []
            for chunk in _id_chunks(test_ids):
                rows.extend(conn.execute(f"{sql} AND t.id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
        changes = effective_change_rows_conn(conn, [r["code_node_id"] for r in rows], realigned_now=realigned_now)
    out: list[dict] = []
    for r in rows:
        change = changes.get(r["code_node_id"])
        if change is None:
            continue
        d = dict(r)
        d["code_changed_at"] = change[1]
        out.append(d)
    out.sort(key=lambda d: (d["test_node_id"], d["code_node_id"]))
    return out


# ---------------------------------------------------------------------------
# Drift-query helpers (filtered/grouped/paginated projections over
# persisted own_status / link_status columns).
# ---------------------------------------------------------------------------


# DOC_SECTION_LONG advisory token (referenced by filter vocab; the
# underlying data lives in section nodes' level_2 content via
# get_long_sections).
DOC_SECTION_LONG = "DOC_SECTION_LONG"


def parse_drift_filter(filter_str: str | None) -> tuple[set[str], set[str], bool]:
    """Parse a ``check`` / ``drift_query`` filter string into selection sets.

    Returns ``(show_own, show_link, show_doc_quality)``:

    - ``show_own``: own_status values that count as "shown" by this filter.
    - ``show_link``: link_status values that count as "shown" by this filter.
    - ``show_doc_quality``: include DOC_SECTION_LONG advisories.

    The vocab matches the historical ``axiom_graph_check`` filter param
    so callers can be migrated mechanically.

    Valid values:
        - ``None`` -- all problem statuses (own + link), no doc quality
          (default behaviour matching the legacy ``check`` default for
          verbose problem-table filtering).
        - ``"staleness"`` -- own problem statuses + LINKED_STALE only
          (excludes BROKEN_LINK).
        - ``"links"`` -- LINKED_STALE + BROKEN_LINK.
        - ``"doc_quality"`` / ``"DOC_SECTION_LONG"`` -- doc-quality only.
        - ``"all"`` -- everything (own problem + link problem + doc quality).
        - Individual status name (e.g. ``"CONTENT_UPDATED"``,
          ``"LINKED_STALE"``).

    Raises:
        ValueError: when ``filter_str`` is non-empty but unrecognised.
    """
    if filter_str is None:
        return (set(OWN_PROBLEM_STATUSES), set(LINK_PROBLEM_STATUSES), False)
    if filter_str == "staleness":
        return (set(OWN_PROBLEM_STATUSES), {LINKED_STALE}, False)
    if filter_str == "links":
        return (set(), {BROKEN_LINK, LINKED_STALE}, False)
    if filter_str in ("doc_quality", DOC_SECTION_LONG):
        return (set(), set(), True)
    if filter_str == "all":
        return (set(OWN_PROBLEM_STATUSES), set(LINK_PROBLEM_STATUSES), True)
    # Individual status name.
    if filter_str in (LINKED_STALE, BROKEN_LINK):
        return (set(), {filter_str}, False)
    if filter_str in (CONTENT_UPDATED, DESC_UPDATED, RENAMED, NOT_FOUND):
        return ({filter_str}, set(), False)
    if filter_str == VERIFIED:
        raise ValueError(
            "filter='VERIFIED' is not a drift filter: VERIFIED is the absence of drift. "
            "Use axiom_graph_check for the VERIFIED count."
        )
    raise ValueError(
        f"Unknown filter value: {filter_str!r}. Valid: None, 'staleness', 'links', "
        f"'doc_quality', 'all', or an individual status name "
        f"(CONTENT_UPDATED, DESC_UPDATED, RENAMED, NOT_FOUND, LINKED_STALE, BROKEN_LINK, DOC_SECTION_LONG)."
    )


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a path glob into a compiled, fully-anchored regex.

    Path-segment aware, unlike SQL ``LIKE``:

    - ``*`` matches any run of characters within one path segment.
    - ``**`` matches across directories; ``**/`` also matches zero
      directories (``a/**/b.py`` matches ``a/b.py``).
    - ``?`` matches one character within a segment.
    - ``[abc]`` / ``[!abc]`` match one character in / not in the set.
    - ``{a,b}`` matches any one alternative (alternatives may contain
      the other wildcards; braces do not nest).

    Args:
        glob: The glob pattern.

    Returns:
        Compiled regex to ``fullmatch`` against a ``/``-separated path.

    Raises:
        ValueError: On an unclosed ``[`` or ``{``, or nested braces --
            a malformed glob errors rather than silently matching nothing.
    """
    out: list[str] = []
    i = 0
    n = len(glob)
    in_brace = False
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                if i + 2 < n and glob[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = glob.find("]", i + 2 if i + 1 < n and glob[i + 1] in "!^" else i + 1)
            if end == -1:
                raise ValueError(f"Invalid location_glob {glob!r}: unclosed '['")
            body = glob[i + 1 : end]
            negate = body[:1] in ("!", "^")
            if negate:
                body = body[1:]
            body = body.replace("\\", "\\\\")
            out.append(f"[^/{body}]" if negate else f"[{body}]")
            i = end + 1
            continue
        elif c == "{":
            if in_brace:
                raise ValueError(f"Invalid location_glob {glob!r}: nested '{{' is not supported")
            in_brace = True
            out.append("(?:")
        elif c == "}" and in_brace:
            in_brace = False
            out.append(")")
        elif c == "," and in_brace:
            out.append("|")
        else:
            out.append(re.escape(c))
        i += 1
    if in_brace:
        raise ValueError(f"Invalid location_glob {glob!r}: unclosed '{{'")
    return re.compile("".join(out))


def _location_matcher(glob: str):
    """Build the SQL-callable predicate for ``location_glob``.

    A location matches when either the whole stored location or its path
    part (before any ``#Lx-Ly`` line fragment) fully matches the glob, so
    ``tests/*.py`` selects function and test nodes as well as modules.
    Backslashes are normalised to ``/``.

    Raises:
        ValueError: Propagated from :func:`_glob_to_regex`.
    """
    pattern = _glob_to_regex(glob)

    def _match(location: str | None) -> int:
        if not location:
            return 0
        loc = location.replace("\\", "/")
        if pattern.fullmatch(loc):
            return 1
        path = loc.split("#", 1)[0]
        return 1 if path != loc and pattern.fullmatch(path) else 0

    return _match


def _long_section_select(show_doc_quality: bool) -> tuple[str, list]:
    """SELECT expression (+ params) flagging DOC_SECTION_LONG advisory rows.

    Constant ``0`` when the filter does not request doc-quality rows, so
    status-only filters never label a row as an advisory.
    """
    if not show_doc_quality:
        return "0", []
    from axiom_graph.db.docs import DOC_SECTION_LONG_THRESHOLD  # noqa: PLC0415

    return "(subtype = 'docjson_section' AND LENGTH(level_2) > ?)", [DOC_SECTION_LONG_THRESHOLD]


def _apply_location_glob(conn, location_glob: str, clauses: list[str]) -> None:
    """Register the glob predicate on *conn* and append its WHERE clause."""
    conn.create_function("drift_location_match", 1, _location_matcher(location_glob), deterministic=True)
    clauses.append("drift_location_match(COALESCE(level_3_location, location)) = 1")


def _row_in_filter(
    own: str,
    link: str,
    show_own: set[str],
    show_link: set[str],
) -> bool:
    """Predicate: should this (own_status, link_status) row be shown?"""
    return own in show_own or link in show_link


def _via_for_node(db_path: Path, node_id: str) -> list[str]:
    """Return the source node ids that likely contributed LINKED_STALE
    for this node, by inspecting inbound annotations / documents / validates
    edges to nodes with own_status != VERIFIED.

    This is a cheap projection -- it does NOT recompute staleness; it
    simply surfaces who the most likely upstream offenders are based on
    the persisted own_status column.  Returns an empty list when
    nothing relevant is found.

    Note: callers iterating over many rows should prefer
    ``_via_for_nodes_batch`` -- a single SELECT for all node_ids -- to
    avoid one fresh ``_connect`` per row.
    """
    return _via_for_nodes_batch(db_path, [node_id]).get(node_id, [])


def _via_for_nodes_batch(
    db_path: Path,
    node_ids: list[str],
) -> dict[str, list[str]]:
    """Batched version of ``_via_for_node`` for an entire page.

    Given a list of node IDs, returns ``{node_id -> [via_id, ...]}`` in a
    single SQL round-trip.  Nodes with no qualifying
    inbound edge are absent from the dict (callers default to ``[]``).

    The per-row "via" string content matches the unbatched version
    semantically: inbound ``annotates`` / ``documents`` / ``validates`` /
    ``delegates_to`` edges to nodes with ``own_status != 'VERIFIED'``,
    capped at 3 entries per source node.

    Args:
        db_path: Path to the axiom-graph DB.
        node_ids: List of node IDs to look up vias for.  Empty list returns
            an empty dict without opening a connection.

    Returns:
        Mapping from node_id to its via_id strings.  Order within
        each list reflects insertion order from the SQL scan.
    """
    if not node_ids:
        return {}

    # Deduplicate while preserving stable iteration -- the SQL IN clause
    # doesn't care about duplicates, but a smaller param list is faster
    # for very wide pages.
    unique_ids = list(dict.fromkeys(node_ids))

    placeholders = ",".join("?" * len(unique_ids))
    sql = (
        "SELECT e.from_id AS src_id, n.id AS via_id "
        "FROM edges e "
        "JOIN nodes n ON n.id = e.to_id "
        f"WHERE e.from_id IN ({placeholders}) "
        "AND e.edge_type IN ('annotates', 'documents', 'validates', 'delegates_to') "
        "AND n.own_status != 'VERIFIED'"
    )

    out: dict[str, list[str]] = {}
    with _connect(db_path) as conn:
        rows = conn.execute(sql, unique_ids).fetchall()
    for r in rows:
        bucket = out.setdefault(r["src_id"], [])
        if r["via_id"] not in bucket:
            bucket.append(r["via_id"])
    return out


def query_drift_rows(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
    page: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Return drift rows matching filter + location glob, ordered by id.

    Args:
        db_path: Path to the axiom-graph DB.
        filter: One of the values accepted by ``parse_drift_filter``.
        location_glob: fnmatch-style glob (with ``**`` for recursive)
            applied to ``nodes.level_3_location`` (falling back to
            ``nodes.location``).
        page: Zero-indexed page number.  ``None`` (with ``limit=None``)
            returns the whole matching slice unpaginated.
        limit: Page size.  ``None`` (with ``page=None``) returns the
            whole matching slice unpaginated.

    Returns:
        List of dicts ``{id, own_status, link_status, location, via}``.
        ``via`` is a list of upstream offender node IDs (may be empty).
    """
    show_own, show_link, show_doc_quality = parse_drift_filter(filter)
    paginate = page is not None and limit is not None

    with _connect(db_path) as conn:
        # Build status filter clause.
        clauses = []
        params: list = []
        # Doc-quality clause (DOC_SECTION_LONG advisory:
        # subtype='docjson_section' node rows whose level_2 -- the
        # canonical section content -- exceeds
        # DOC_SECTION_LONG_THRESHOLD).  OR-unions with the
        # status clauses when filter='all'.
        doc_quality_clause: str | None = None
        if show_doc_quality:
            from axiom_graph.db.docs import DOC_SECTION_LONG_THRESHOLD  # noqa: PLC0415

            doc_quality_clause = "(subtype = 'docjson_section' AND LENGTH(level_2) > ?)"
        # Own/link union.
        if show_own and show_link:
            status_clause = "(own_status IN ({}) OR link_status IN ({}))".format(
                ",".join("?" * len(show_own)),
                ",".join("?" * len(show_link)),
            )
            params.extend(sorted(show_own))
            params.extend(sorted(show_link))
            if doc_quality_clause:
                clauses.append(f"({status_clause} OR {doc_quality_clause})")
                params.append(DOC_SECTION_LONG_THRESHOLD)
            else:
                clauses.append(status_clause)
        elif show_own:
            status_clause = "own_status IN ({})".format(",".join("?" * len(show_own)))
            params.extend(sorted(show_own))
            if doc_quality_clause:
                clauses.append(f"({status_clause} OR {doc_quality_clause})")
                params.append(DOC_SECTION_LONG_THRESHOLD)
            else:
                clauses.append(status_clause)
        elif show_link:
            status_clause = "link_status IN ({})".format(",".join("?" * len(show_link)))
            params.extend(sorted(show_link))
            if doc_quality_clause:
                clauses.append(f"({status_clause} OR {doc_quality_clause})")
                params.append(DOC_SECTION_LONG_THRESHOLD)
            else:
                clauses.append(status_clause)
        elif doc_quality_clause:
            # filter='doc_quality' / 'DOC_SECTION_LONG' alone.
            clauses.append(doc_quality_clause)
            params.append(DOC_SECTION_LONG_THRESHOLD)
        else:
            # Nothing to project.
            return []

        if location_glob is not None:
            _apply_location_glob(conn, location_glob, clauses)

        where = " AND ".join(clauses)
        long_select, long_params = _long_section_select(show_doc_quality)
        sql = (
            "SELECT id, own_status, link_status, "
            "       COALESCE(level_3_location, location) AS location, "
            f"       {long_select} AS long_section "
            f"FROM nodes WHERE {where} "
            "ORDER BY id"
        )
        params = [*long_params, *params]
        if paginate:
            offset = max(0, page) * max(1, limit)
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()

    # Batch via lookup once for the whole page -- one SQL round-trip
    # instead of one fresh _connect() per LINKED_STALE row.
    linked_stale_ids = [r["id"] for r in rows if r["link_status"] == LINKED_STALE]
    via_map = _via_for_nodes_batch(db_path, linked_stale_ids)

    out: list[dict] = []
    for r in rows:
        node_id = r["id"]
        link_status = r["link_status"]
        via = via_map.get(node_id, []) if link_status == LINKED_STALE else []
        out.append(
            {
                "id": node_id,
                "own_status": r["own_status"],
                "link_status": link_status,
                "location": r["location"],
                "via": via,
                "long_section": bool(r["long_section"]),
            }
        )
    return out


def _location_prefix(location: str | None, depth: int = 2) -> str:
    """Return the first ``depth`` path components of ``location``.

    Treats both ``/`` and ``\\`` as separators.  A ``#...`` line-range
    suffix (``tests/test_x.py#L10-L20``) is dropped first, so every node in
    one file shares the file's group.  Returns ``"(no-location)"`` when
    ``location`` is empty/None.
    """
    if not location:
        return "(no-location)"
    parts = location.split("#", 1)[0].replace("\\", "/").split("/")
    parts = [p for p in parts if p]
    if not parts:
        return "(no-location)"
    return "/".join(parts[:depth])


def _filtered_rows_for_grouping(
    db_path: Path,
    filter: str | None,
    location_glob: str | None,
) -> list[dict]:
    """Return the unpaginated set of nodes matching filter + glob.

    Used by the grouped helpers (counts / IDs).  Returns dicts with
    ``id``, ``own_status``, ``link_status``, ``location``, ``subtype``,
    ``long_section``.
    """
    show_own, show_link, show_doc_quality = parse_drift_filter(filter)
    with _connect(db_path) as conn:
        clauses = []
        params: list = []
        doc_quality_clause: str | None = None
        if show_doc_quality:
            from axiom_graph.db.docs import DOC_SECTION_LONG_THRESHOLD  # noqa: PLC0415

            doc_quality_clause = "(subtype = 'docjson_section' AND LENGTH(level_2) > ?)"
        if show_own and show_link:
            status_clause = "(own_status IN ({}) OR link_status IN ({}))".format(
                ",".join("?" * len(show_own)),
                ",".join("?" * len(show_link)),
            )
            params.extend(sorted(show_own))
            params.extend(sorted(show_link))
            if doc_quality_clause:
                clauses.append(f"({status_clause} OR {doc_quality_clause})")
                params.append(DOC_SECTION_LONG_THRESHOLD)
            else:
                clauses.append(status_clause)
        elif show_own:
            status_clause = "own_status IN ({})".format(",".join("?" * len(show_own)))
            params.extend(sorted(show_own))
            if doc_quality_clause:
                clauses.append(f"({status_clause} OR {doc_quality_clause})")
                params.append(DOC_SECTION_LONG_THRESHOLD)
            else:
                clauses.append(status_clause)
        elif show_link:
            status_clause = "link_status IN ({})".format(",".join("?" * len(show_link)))
            params.extend(sorted(show_link))
            if doc_quality_clause:
                clauses.append(f"({status_clause} OR {doc_quality_clause})")
                params.append(DOC_SECTION_LONG_THRESHOLD)
            else:
                clauses.append(status_clause)
        elif doc_quality_clause:
            clauses.append(doc_quality_clause)
            params.append(DOC_SECTION_LONG_THRESHOLD)
        else:
            return []

        if location_glob is not None:
            _apply_location_glob(conn, location_glob, clauses)

        long_select, long_params = _long_section_select(show_doc_quality)
        sql = (
            "SELECT id, own_status, link_status, subtype, "
            "       COALESCE(level_3_location, location) AS location, "
            f"       {long_select} AS long_section "
            "FROM nodes WHERE " + " AND ".join(clauses) + " ORDER BY id"
        )
        rows = conn.execute(sql, [*long_params, *params]).fetchall()
    return [{**dict(r), "long_section": bool(r["long_section"])} for r in rows]


def query_drift_counts_by_status(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
) -> list[dict]:
    """Return ``[{group: '<own>/<link>', count: N}, ...]`` grouped by status.

    Each row is uniquely identified by the ``(own_status, link_status)``
    pair.  Rows are sorted alphabetically by group label.
    """
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    counts: dict[str, int] = {}
    for r in rows:
        key = f"{r['own_status']}/{r['link_status']}"
        counts[key] = counts.get(key, 0) + 1
    return [{"group": k, "count": v} for k, v in sorted(counts.items())]


def query_drift_counts_by_location_prefix(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
) -> list[dict]:
    """Return ``[{group: 'pkg/sub', count: N}, ...]`` by 2-component path prefix."""
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    counts: dict[str, int] = {}
    for r in rows:
        key = _location_prefix(r["location"])
        counts[key] = counts.get(key, 0) + 1
    return [{"group": k, "count": v} for k, v in sorted(counts.items())]


def query_drift_ids_by_status(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
) -> list[dict]:
    """Return ``[{group: '<own>/<link>', ids: [...]}, ...]``."""
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    buckets: dict[str, list[str]] = {}
    for r in rows:
        key = f"{r['own_status']}/{r['link_status']}"
        buckets.setdefault(key, []).append(r["id"])
    return [{"group": k, "ids": sorted(v)} for k, v in sorted(buckets.items())]


def query_drift_ids_by_location_prefix(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
) -> list[dict]:
    """Return ``[{group: 'pkg/sub', ids: [...]}, ...]``."""
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    buckets: dict[str, list[str]] = {}
    for r in rows:
        key = _location_prefix(r["location"])
        buckets.setdefault(key, []).append(r["id"])
    return [{"group": k, "ids": sorted(v)} for k, v in sorted(buckets.items())]


def _build_feature_index(db_path: Path) -> dict[str, str]:
    """Return ``{node_id -> feature_label}`` for every node with an inbound
    ``documents`` edge.

    Walks the doc-tree to find the nearest ``docs/features/{X}`` ancestor.
    The "feature label" is the X token (e.g. ``viz``, ``mcp-server``).

    Tie-breaker rules (when a node has multiple inbound ``documents``
    edges from different feature subtrees):
        1. Pick the section whose feature ancestor is **closest** in the
           doc-tree (smallest hop count from section to ``docs/features/{X}``).
        2. Ties break alphabetically on the feature label.
        3. The final label is recorded; the node lives in exactly one
           bucket.

    Nodes without any inbound ``documents`` edge are NOT in the returned
    dict; the caller assigns them to the ``(undocumented)`` sentinel
    bucket.
    """
    with _connect(db_path) as conn:
        # Inbound documents edges: section -> code node.
        # In our graph: edges with edge_type='documents' have from_id=section_id, to_id=code_node_id.
        edge_rows = conn.execute(
            "SELECT from_id AS section_id, to_id AS code_id FROM edges WHERE edge_type = 'documents'"
        ).fetchall()
        # All sections (for the doc_id lookup + hop-count walk).
        from axiom_graph.db.docs import _SECTION_FILTER_SQL, split_section_id  # noqa: PLC0415

        sec_rows = conn.execute(f"SELECT id AS section_id FROM nodes WHERE {_SECTION_FILTER_SQL}").fetchall()
        # All docs (for the id-suffix walk to docs/features/X).
        doc_rows = conn.execute("SELECT id FROM docs").fetchall()

    # Map section_id -> doc_id (derived from the section ID's dot-path).
    sec_to_doc: dict[str, str] = {r["section_id"]: split_section_id(r["section_id"])[0] for r in sec_rows}

    # For each doc, walk its node-id (which is project_id::dotted.path)
    # backwards looking for the 'features' segment, and pick the X
    # immediately after.  Doc id format example:
    #   axiom_graph::docs/features/indexer/sub_features/scanning/design
    # We want X='indexer' (the topmost feature token after 'features').
    from axiom_graph.index.doc_ids import DOC_ID_PATH_SEP  # noqa: PLC0415

    def _doc_to_feature(doc_id: str) -> str | None:
        # Strip project_id prefix.
        if "::" in doc_id:
            tail = doc_id.split("::", 1)[1]
        else:
            tail = doc_id
        parts = tail.split(DOC_ID_PATH_SEP)
        # Find first 'features' segment.
        try:
            idx = parts.index("features")
        except ValueError:
            return None
        if idx + 1 < len(parts):
            return parts[idx + 1]
        return None

    doc_to_feature: dict[str, str | None] = {}
    for r in doc_rows:
        doc_to_feature[r["id"]] = _doc_to_feature(r["id"])

    # Hop-count proxy: depth of the section's parent path within the doc.
    # We don't have an explicit hop count from section to docs/features/X,
    # but the section ID's dot-path encodes its nesting depth.  For
    # tie-breaking we use this depth as a coarse proxy: deeper section
    # implies the feature ancestor is closer to the section in the doc
    # tree.
    sec_to_depth: dict[str, int] = {sid: sid.rsplit("::", 1)[-1].count(".") for sid in sec_to_doc}

    # For each code node, collect all (feature_label, depth) candidates
    # from inbound documents edges, then pick the winner.
    candidates: dict[str, list[tuple[str, int]]] = {}
    for r in edge_rows:
        sec_id = r["section_id"]
        code_id = r["code_id"]
        doc_id = sec_to_doc.get(sec_id)
        if doc_id is None:
            continue
        feature = doc_to_feature.get(doc_id)
        if feature is None:
            continue
        depth = sec_to_depth.get(sec_id, 0)
        candidates.setdefault(code_id, []).append((feature, depth))

    out: dict[str, str] = {}
    for code_id, options in candidates.items():
        # Pick: max depth (deepest section -> closest to feature ancestor),
        # then alphabetical on feature label.
        best = sorted(options, key=lambda t: (-t[1], t[0]))[0]
        out[code_id] = best[0]
    return out


def query_drift_counts_by_feature(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
) -> list[dict]:
    """Return ``[{group: feature, count: N}, ...]`` grouped by inbound
    ``documents``-edge feature ancestor.

    Nodes without any inbound ``documents`` edge bucket as
    ``(undocumented)``.  Tie-breaker rules are documented in
    ``_build_feature_index``.
    """
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    feat_index = _build_feature_index(db_path)
    counts: dict[str, int] = {}
    for r in rows:
        key = feat_index.get(r["id"], "(undocumented)")
        counts[key] = counts.get(key, 0) + 1
    return [{"group": k, "count": v} for k, v in sorted(counts.items())]


def query_drift_ids_by_feature(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
) -> list[dict]:
    """Return ``[{group: feature, ids: [...]}, ...]`` by feature ancestor."""
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    feat_index = _build_feature_index(db_path)
    buckets: dict[str, list[str]] = {}
    for r in rows:
        key = feat_index.get(r["id"], "(undocumented)")
        buckets.setdefault(key, []).append(r["id"])
    return [{"group": k, "ids": sorted(v)} for k, v in sorted(buckets.items())]


def query_drift_full_by_status(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
) -> list[dict]:
    """Return ``[{group: '<own>/<link>', rows: [...]}, ...]`` (full rows)."""
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    linked_stale_ids = [r["id"] for r in rows if r["link_status"] == LINKED_STALE]
    via_map = _via_for_nodes_batch(db_path, linked_stale_ids)
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        key = f"{r['own_status']}/{r['link_status']}"
        via = via_map.get(r["id"], []) if r["link_status"] == LINKED_STALE else []
        buckets.setdefault(key, []).append(
            {
                "id": r["id"],
                "own_status": r["own_status"],
                "link_status": r["link_status"],
                "location": r["location"],
                "via": via,
                "long_section": bool(r["long_section"]),
            }
        )
    return [{"group": k, "rows": sorted(v, key=lambda x: x["id"])} for k, v in sorted(buckets.items())]


def query_drift_full_by_location_prefix(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
) -> list[dict]:
    """Return ``[{group: 'pkg/sub', rows: [...]}, ...]`` (full rows)."""
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    linked_stale_ids = [r["id"] for r in rows if r["link_status"] == LINKED_STALE]
    via_map = _via_for_nodes_batch(db_path, linked_stale_ids)
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        key = _location_prefix(r["location"])
        via = via_map.get(r["id"], []) if r["link_status"] == LINKED_STALE else []
        buckets.setdefault(key, []).append(
            {
                "id": r["id"],
                "own_status": r["own_status"],
                "link_status": r["link_status"],
                "location": r["location"],
                "via": via,
                "long_section": bool(r["long_section"]),
            }
        )
    return [{"group": k, "rows": sorted(v, key=lambda x: x["id"])} for k, v in sorted(buckets.items())]


def query_drift_full_by_feature(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
) -> list[dict]:
    """Return ``[{group: feature, rows: [...]}, ...]`` (full rows)."""
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    feat_index = _build_feature_index(db_path)
    linked_stale_ids = [r["id"] for r in rows if r["link_status"] == LINKED_STALE]
    via_map = _via_for_nodes_batch(db_path, linked_stale_ids)
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        key = feat_index.get(r["id"], "(undocumented)")
        via = via_map.get(r["id"], []) if r["link_status"] == LINKED_STALE else []
        buckets.setdefault(key, []).append(
            {
                "id": r["id"],
                "own_status": r["own_status"],
                "link_status": r["link_status"],
                "location": r["location"],
                "via": via,
                "long_section": bool(r["long_section"]),
            }
        )
    return [{"group": k, "rows": sorted(v, key=lambda x: x["id"])} for k, v in sorted(buckets.items())]


_DOC_SUBTYPES = frozenset({"docjson", "docjson_doc", "docjson_section"})


def _node_kind(row: dict, test_paths: Collection[str]) -> str:
    """Classify a drift row as ``"doc"``, ``"test"`` or ``"code"``.

    A docjson subtype is ``doc``.  A row whose location (``#...`` suffix
    dropped, ``\\`` read as ``/``) starts with one of ``test_paths``, or whose
    subtype is ``test``, is ``test``.  Everything else is ``code``.

    Args:
        row: A row from :func:`_filtered_rows_for_grouping`.
        test_paths: The configured ``scan.test_paths`` prefixes.

    Returns:
        The kind label.
    """
    if row.get("subtype") in _DOC_SUBTYPES:
        return "doc"
    if row.get("subtype") == "test":
        return "test"
    location = (row.get("location") or "").split("#", 1)[0].replace("\\", "/")
    if location and any(location.startswith(tp.replace("\\", "/")) for tp in test_paths if tp):
        return "test"
    return "code"


def query_drift_counts_by_node_kind(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
    test_paths: Collection[str] = (),
) -> list[dict]:
    """Return ``[{group: 'code'|'doc'|'test', count: N}, ...]``.

    Args:
        db_path: Path to the index database.
        filter: Drift filter vocabulary (see :func:`parse_drift_filter`).
        location_glob: Optional location glob.
        test_paths: The configured ``scan.test_paths`` prefixes; see
            :func:`_node_kind`.

    Returns:
        One entry per non-empty kind, sorted by kind.
    """
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    counts: dict[str, int] = {}
    for r in rows:
        key = _node_kind(r, test_paths)
        counts[key] = counts.get(key, 0) + 1
    return [{"group": k, "count": v} for k, v in sorted(counts.items())]


def query_drift_ids_by_node_kind(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
    test_paths: Collection[str] = (),
) -> list[dict]:
    """Return ``[{group: 'code'|'doc'|'test', ids: [...]}, ...]``.

    Args:
        db_path: Path to the index database.
        filter: Drift filter vocabulary (see :func:`parse_drift_filter`).
        location_glob: Optional location glob.
        test_paths: The configured ``scan.test_paths`` prefixes.

    Returns:
        One entry per non-empty kind, sorted by kind, ids sorted.
    """
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    buckets: dict[str, list[str]] = {}
    for r in rows:
        buckets.setdefault(_node_kind(r, test_paths), []).append(r["id"])
    return [{"group": k, "ids": sorted(v)} for k, v in sorted(buckets.items())]


def query_drift_full_by_node_kind(
    db_path: Path,
    filter: str | None = None,
    location_glob: str | None = None,
    test_paths: Collection[str] = (),
) -> list[dict]:
    """Return ``[{group: 'code'|'doc'|'test', rows: [...]}, ...]`` (full rows).

    Args:
        db_path: Path to the index database.
        filter: Drift filter vocabulary (see :func:`parse_drift_filter`).
        location_glob: Optional location glob.
        test_paths: The configured ``scan.test_paths`` prefixes.

    Returns:
        One entry per non-empty kind, sorted by kind, rows sorted by id.
    """
    rows = _filtered_rows_for_grouping(db_path, filter, location_glob)
    linked_stale_ids = [r["id"] for r in rows if r["link_status"] == LINKED_STALE]
    via_map = _via_for_nodes_batch(db_path, linked_stale_ids)
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        via = via_map.get(r["id"], []) if r["link_status"] == LINKED_STALE else []
        buckets.setdefault(_node_kind(r, test_paths), []).append(
            {
                "id": r["id"],
                "own_status": r["own_status"],
                "link_status": r["link_status"],
                "location": r["location"],
                "via": via,
                "long_section": bool(r["long_section"]),
            }
        )
    return [{"group": k, "rows": sorted(v, key=lambda x: x["id"])} for k, v in sorted(buckets.items())]


__all__ = [
    # File mtime
    "get_file_mtime",
    "get_all_file_mtimes",
    # Staleness persistence
    "persist_staleness",
    "get_all_staleness",
    "count_status_pairs_conn",
    "get_staleness_for_conn",
    "get_ordered_staleness_conn",
    "count_nodes_conn",
    "BROKEN_LINK_EDGE_TYPES",
    "DANGLING_LINK_EDGE_TYPES",
    "charged_source_sql",
    "unflagged_dangling_sources_conn",
    # Computed staleness
    "get_stale_doc_sections",
    "get_stale_annotated_nodes",
    "get_stale_workflow_envelopes_via_delegates",
    "get_stale_tests",
    # Drift-query
    "parse_drift_filter",
    "query_drift_rows",
    "query_drift_counts_by_status",
    "query_drift_counts_by_location_prefix",
    "query_drift_counts_by_feature",
    "query_drift_ids_by_status",
    "query_drift_ids_by_location_prefix",
    "query_drift_ids_by_feature",
    "query_drift_full_by_status",
    "query_drift_full_by_location_prefix",
    "query_drift_full_by_feature",
    "query_drift_counts_by_node_kind",
    "query_drift_ids_by_node_kind",
    "query_drift_full_by_node_kind",
]
