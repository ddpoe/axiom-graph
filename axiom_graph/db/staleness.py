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

from pathlib import Path

from axiom_annotations import task

from axiom_graph.db._core import _connect
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
    """Return stored file_mtime from the module/doc node for this location, or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT file_mtime FROM nodes WHERE location = ? AND file_mtime IS NOT NULL LIMIT 1",
            (location,),
        ).fetchone()
        return row["file_mtime"] if row else None


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


# ---------------------------------------------------------------------------
# Computed staleness queries
# ---------------------------------------------------------------------------


@task(
    purpose="Find doc sections whose linked code node has ever drifted — produces a sticky LINKED_STALE signal cleared only by mark_clean",
    inputs="db_path",
    outputs="List of dicts with section_id, doc_id, heading, code_node_id, code_changed_at, section_updated_at",
)
def get_stale_doc_sections(db_path: Path) -> list[dict]:
    """Return doc sections linked to code that has drifted since the last verification.

    LINKED_STALE is sticky. Pass 1 of ``_get_linked_stale_ids`` calls this
    helper to collect every section whose linked code node has at least
    one ``CONTENT_ONLY`` / ``CONTENT_AND_DESC`` / ``BECAME_CONTENT_UPDATED``
    history row. The query intentionally does NOT compare ``nh.scanned_at``
    against ``doc_sections.updated_at``: editing a section is not the same
    as verifying that its prose still matches the linked code, so an edit
    must not auto-clear LINKED_STALE.

    The only mechanism that clears LINKED_STALE is Pass 2 of
    ``_get_linked_stale_ids``, which removes nodes whose
    ``node_verification.verified_at`` (written by ``mark_clean``) is newer
    than every via node's latest code change. See ADR-017.

    Each dict has: section_id, doc_id, heading, code_node_id,
    code_changed_at, section_updated_at.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                s.id          AS section_id,
                s.doc_id,
                s.heading,
                e.to_id       AS code_node_id,
                nh.scanned_at AS code_changed_at,
                s.updated_at  AS section_updated_at
            FROM doc_sections s
            JOIN edges e          ON e.from_id = s.id AND e.edge_type = 'documents'
            JOIN nodes code_n     ON code_n.id = e.to_id AND NOT (code_n.node_type = 'atomic_process' AND COALESCE(code_n.subtype, '') = 'docjson')
            JOIN node_history nh  ON nh.node_id = e.to_id
                                 AND nh.change_type IN ('CONTENT_ONLY', 'CONTENT_AND_DESC', 'BECAME_CONTENT_UPDATED')
                                 AND nh.id = (
                                       SELECT MAX(h2.id) FROM node_history h2
                                       WHERE h2.node_id = e.to_id
                                         AND h2.change_type IN ('CONTENT_ONLY', 'CONTENT_AND_DESC', 'BECAME_CONTENT_UPDATED')
                                     )
            """
        ).fetchall()
        return [dict(r) for r in rows]


@task(
    purpose="Find annotation-envelope nodes whose annotated target drifted (code OR docstring) after the envelope was last updated — produces LINKED_STALE via Pass A",
    inputs="db_path",
    outputs="List of dicts with envelope_id, target_id, change_at, envelope_updated_at",
)
def get_stale_annotated_nodes(db_path: Path) -> list[dict]:
    """Return envelope nodes whose annotated target changed after the envelope was last updated.

    Joins ``nodes`` (envelope) -> ``edges`` (``edge_type = 'annotates'``) ->
    ``node_history`` of the target, widened to include ``DESC_ONLY`` so
    pure-docstring drift on the target still flips the envelope.

    This is the Pass A query behind ``annotates`` staleness.  Each dict has
    keys: ``envelope_id``, ``target_id``, ``change_at``, ``envelope_updated_at``.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                e.from_id     AS envelope_id,
                e.to_id       AS target_id,
                nh.scanned_at AS change_at,
                n.updated_at  AS envelope_updated_at
            FROM edges e
            JOIN nodes n          ON n.id = e.from_id
            JOIN node_history nh  ON nh.node_id = e.to_id
                                 AND nh.change_type IN ('CONTENT_ONLY', 'DESC_ONLY', 'CONTENT_AND_DESC', 'BECAME_CONTENT_UPDATED')
                                 AND nh.id = (
                                       SELECT MAX(h2.id) FROM node_history h2
                                       WHERE h2.node_id = e.to_id
                                         AND h2.change_type IN ('CONTENT_ONLY', 'DESC_ONLY', 'CONTENT_AND_DESC', 'BECAME_CONTENT_UPDATED')
                                     )
            WHERE e.edge_type = 'annotates'
              AND nh.scanned_at > n.updated_at
            """
        ).fetchall()
        return [dict(r) for r in rows]


@task(
    purpose="Find workflow/task envelopes whose delegates_to chain transitively reaches a CONTENT-changed task — produces LINKED_STALE via Pass B with cycle guard",
    inputs="db_path",
    outputs="List of dicts with envelope_id, via_task_id, envelope_updated_at, change_at",
)
def get_stale_workflow_envelopes_via_delegates(db_path: Path) -> list[dict]:
    """Return envelopes whose delegates_to closure includes a CONTENT-changed task.

    Walks the transitive closure of ``composes`` → ``autostep`` →
    ``delegates_to`` → ``annotates`` (inbound) for every envelope, with a
    per-envelope visited-task set for cycle safety.  Only CODE changes
    propagate: ``DESC_ONLY`` is excluded (Pass A catches it on the task's
    own envelope).

    Each dict has: ``envelope_id``, ``via_task_id``, ``envelope_updated_at``,
    ``change_at``.
    """
    with _connect(db_path) as conn:
        # Load edges once.
        composes_rows = conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'composes'").fetchall()
        delegates_rows = conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'delegates_to'").fetchall()
        annotates_rows = conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'annotates'").fetchall()
        envelope_rows = conn.execute(
            "SELECT id, updated_at FROM nodes WHERE node_type = 'composite_process' AND subtype IN ('workflow', 'task')"
        ).fetchall()
        subtype_rows = conn.execute("SELECT id, subtype FROM nodes").fetchall()
        # Latest CODE-change history per node (for the stale filter).
        latest_rows = conn.execute(
            """
            SELECT nh.node_id AS node_id, nh.scanned_at AS scanned_at
            FROM node_history nh
            WHERE nh.change_type IN ('CONTENT_ONLY', 'CONTENT_AND_DESC', 'BECAME_CONTENT_UPDATED')
              AND nh.id = (
                  SELECT MAX(h2.id) FROM node_history h2
                  WHERE h2.node_id = nh.node_id
                    AND h2.change_type IN ('CONTENT_ONLY', 'CONTENT_AND_DESC', 'BECAME_CONTENT_UPDATED')
              )
            """
        ).fetchall()

    latest_code_change: dict[str, str] = {r["node_id"]: r["scanned_at"] for r in latest_rows}
    node_subtype: dict[str, str | None] = {r["id"]: r["subtype"] for r in subtype_rows}

    # Forward adjacency.
    composes_out: dict[str, list[str]] = {}
    for r in composes_rows:
        composes_out.setdefault(r["from_id"], []).append(r["to_id"])
    delegates_out: dict[str, str] = {}
    for r in delegates_rows:
        # AutoStep has at most one delegates_to edge by construction.
        delegates_out[r["from_id"]] = r["to_id"]
    # Reverse: function → envelope(s) that annotate it.
    annotates_rev: dict[str, list[str]] = {}
    for r in annotates_rows:
        annotates_rev.setdefault(r["to_id"], []).append(r["from_id"])

    def _reachable_tasks(envelope_id: str) -> list[str]:
        """BFS over (composes → autostep → delegates_to → annotates_rev)."""
        visited_tasks: set[str] = set()
        # Each frontier item is an envelope we expand.
        env_frontier: list[str] = [envelope_id]
        visited_envs: set[str] = {envelope_id}
        depth = 0
        while env_frontier and depth < 32:
            next_envs: list[str] = []
            for env in env_frontier:
                for step_id in composes_out.get(env, []):
                    if node_subtype.get(step_id) != "autostep":
                        continue
                    target_task = delegates_out.get(step_id)
                    if not target_task:
                        continue
                    if target_task in visited_tasks:
                        continue
                    visited_tasks.add(target_task)
                    # Follow up through target's envelope, if any.
                    for tgt_env in annotates_rev.get(target_task, []):
                        if tgt_env in visited_envs:
                            continue
                        visited_envs.add(tgt_env)
                        next_envs.append(tgt_env)
            env_frontier = next_envs
            depth += 1
        return list(visited_tasks)

    results: list[dict] = []
    for env_row in envelope_rows:
        env_id = env_row["id"]
        env_updated = env_row["updated_at"]
        for task_id in _reachable_tasks(env_id):
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
    return results


@task(
    purpose="Find test nodes linked via 'validates' to code that has ever drifted — produces a sticky LINKED_STALE signal cleared only by mark_clean",
    inputs="db_path",
    outputs="List of dicts with test_node_id, code_node_id, code_changed_at, test_updated_at",
)
def get_stale_tests(db_path: Path) -> list[dict]:
    """Return test nodes linked to code that has drifted since the last verification.

    LINKED_STALE is sticky. Pass 1 of ``_get_linked_stale_ids`` calls this
    helper to collect every test whose ``validates`` target has at least
    one ``CONTENT_ONLY`` / ``CONTENT_AND_DESC`` / ``BECAME_CONTENT_UPDATED``
    history row. The query intentionally does NOT compare ``nh.scanned_at``
    against ``t.updated_at``: editing a test file is not the same as
    re-running the test against the new code, so an edit must not
    auto-clear LINKED_STALE.

    The only mechanism that clears LINKED_STALE is Pass 2 of
    ``_get_linked_stale_ids``, which removes nodes whose
    ``node_verification.verified_at`` (written by ``mark_clean``) is newer
    than every via node's latest code change. See ADR-017.

    Each dict has: test_node_id, code_node_id, code_changed_at, test_updated_at.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                t.id          AS test_node_id,
                e.to_id       AS code_node_id,
                nh.scanned_at AS code_changed_at,
                t.updated_at  AS test_updated_at
            FROM nodes t
            JOIN edges e         ON e.from_id = t.id AND e.edge_type = 'validates'
            JOIN nodes code_n    ON code_n.id = e.to_id AND NOT (code_n.node_type = 'atomic_process' AND COALESCE(code_n.subtype, '') = 'docjson')
            JOIN node_history nh ON nh.node_id = e.to_id
                                AND nh.change_type IN ('CONTENT_ONLY', 'CONTENT_AND_DESC', 'BECAME_CONTENT_UPDATED')
                                AND nh.id = (
                                      SELECT MAX(h2.id) FROM node_history h2
                                      WHERE h2.node_id = e.to_id
                                        AND h2.change_type IN ('CONTENT_ONLY', 'CONTENT_AND_DESC', 'BECAME_CONTENT_UPDATED')
                                    )
            WHERE t.node_type = 'atomic_process'
              AND t.subtype   = 'test'
            """
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Drift-query helpers (filtered/grouped/paginated projections over
# persisted own_status / link_status columns).
# ---------------------------------------------------------------------------


# DOC_SECTION_LONG advisory token (referenced by filter vocab; the
# underlying data lives in doc_sections.content via get_long_sections).
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
    if filter_str in (CONTENT_UPDATED, DESC_UPDATED, RENAMED, NOT_FOUND, VERIFIED):
        return ({filter_str}, set(), False)
    raise ValueError(
        f"Unknown filter value: {filter_str!r}. Valid: None, 'staleness', 'links', "
        f"'doc_quality', 'all', or an individual status name "
        f"(CONTENT_UPDATED, DESC_UPDATED, RENAMED, NOT_FOUND, LINKED_STALE, BROKEN_LINK, DOC_SECTION_LONG)."
    )


def _glob_to_like(glob: str) -> str:
    """Translate an fnmatch-style glob into a SQL LIKE pattern.

    Supports ``*`` (matches any sequence) and ``?`` (matches any single
    character).  Escapes existing SQL wildcards (``%``, ``_``) so they
    are treated literally in the source path.

    Note: ``**`` is collapsed to ``%`` (recursive directory match).
    """
    out = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if c == "*":
            # Coalesce ** to %
            if i + 1 < len(glob) and glob[i + 1] == "*":
                out.append("%")
                i += 2
                continue
            out.append("%")
        elif c == "?":
            out.append("_")
        elif c == "%":
            out.append(r"\%")
        elif c == "_":
            out.append(r"\_")
        elif c == "\\":
            out.append(r"\\")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _row_in_filter(
    own: str,
    link: str,
    show_own: set[str],
    show_link: set[str],
) -> bool:
    """Predicate: should this (own_status, link_status) row be shown?"""
    return own in show_own or link in show_link


def _via_for_node(db_path: Path, node_id: str) -> list[str]:
    """Return the (up to 3) source node ids that contributed LINKED_STALE
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

    Given a list of node IDs, returns ``{node_id -> [via_id, ...]}`` (up
    to 3 vias per node) in a single SQL round-trip.  Nodes with no qualifying
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
        Mapping from node_id to up to three via_id strings.  Order within
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
        if len(bucket) < 3:
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
        # Doc-quality clause (DOC_SECTION_LONG advisory: subtype='docjson'
        # rows whose level_2 -- the shadow of doc_sections.content --
        # exceeds DOC_SECTION_LONG_THRESHOLD).  OR-unions with the
        # status clauses when filter='all'.
        doc_quality_clause: str | None = None
        if show_doc_quality:
            from axiom_graph.db.docs import DOC_SECTION_LONG_THRESHOLD  # noqa: PLC0415

            doc_quality_clause = "(subtype = 'docjson' AND LENGTH(level_2) > ?)"
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
            like = _glob_to_like(location_glob)
            clauses.append("(COALESCE(level_3_location, location) LIKE ? ESCAPE '\\')")
            params.append(like)

        where = " AND ".join(clauses)
        sql = (
            "SELECT id, own_status, link_status, "
            "       COALESCE(level_3_location, location) AS location "
            f"FROM nodes WHERE {where} "
            "ORDER BY id"
        )
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
            }
        )
    return out


def _location_prefix(location: str | None, depth: int = 2) -> str:
    """Return the first ``depth`` path components of ``location``.

    Treats both ``/`` and ``\\`` as separators.  Returns ``"(no-location)"``
    when ``location`` is empty/None.
    """
    if not location:
        return "(no-location)"
    parts = location.replace("\\", "/").split("/")
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
    ``id``, ``own_status``, ``link_status``, ``location``.
    """
    show_own, show_link, show_doc_quality = parse_drift_filter(filter)
    with _connect(db_path) as conn:
        clauses = []
        params: list = []
        doc_quality_clause: str | None = None
        if show_doc_quality:
            from axiom_graph.db.docs import DOC_SECTION_LONG_THRESHOLD  # noqa: PLC0415

            doc_quality_clause = "(subtype = 'docjson' AND LENGTH(level_2) > ?)"
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
            like = _glob_to_like(location_glob)
            clauses.append("(COALESCE(level_3_location, location) LIKE ? ESCAPE '\\')")
            params.append(like)

        sql = (
            "SELECT id, own_status, link_status, "
            "       COALESCE(level_3_location, location) AS location "
            "FROM nodes WHERE " + " AND ".join(clauses) + " ORDER BY id"
        )
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


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

    Walks the doc-tree to find the nearest ``docs.features.{X}`` ancestor.
    The "feature label" is the X token (e.g. ``viz``, ``mcp-server``).

    Tie-breaker rules (when a node has multiple inbound ``documents``
    edges from different feature subtrees):
        1. Pick the section whose feature ancestor is **closest** in the
           doc-tree (smallest hop count from section to ``docs.features.{X}``).
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
        sec_rows = conn.execute("SELECT id AS section_id, doc_id FROM doc_sections").fetchall()
        # All docs (for the id-suffix walk to docs.features.X).
        doc_rows = conn.execute("SELECT id FROM docs").fetchall()

    # Map section_id -> doc_id.
    sec_to_doc: dict[str, str] = {r["section_id"]: r["doc_id"] for r in sec_rows}

    # For each doc, walk its node-id (which is project_id::dotted.path)
    # backwards looking for the 'features' segment, and pick the X
    # immediately after.  Doc id format example:
    #   axiom_graph::docs.features.indexer.sub_features.scanning.design
    # We want X='indexer' (the topmost feature token after 'features').
    def _doc_to_feature(doc_id: str) -> str | None:
        # Strip project_id prefix.
        if "::" in doc_id:
            tail = doc_id.split("::", 1)[1]
        else:
            tail = doc_id
        parts = tail.split(".")
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
    # We don't have an explicit hop count from section to docs.features.X,
    # but doc_sections.depth gives the section's nesting depth.  For
    # tie-breaking we use this depth as a coarse proxy: deeper section
    # implies the feature ancestor is closer to the section in the doc
    # tree.  Pull section depths.
    with _connect(db_path) as conn:
        depth_rows = conn.execute("SELECT id, depth FROM doc_sections").fetchall()
    sec_to_depth: dict[str, int] = {r["id"]: (r["depth"] or 0) for r in depth_rows}

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
]
