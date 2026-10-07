"""Whole-read oracles for the build's scoped reads.

Each function answers a question the build now answers by reading only what
it asks about, the way it was answered by reading every row.  Tests compare
the scoped answer against these.
"""

from __future__ import annotations

from collections.abc import Collection, Container
from pathlib import Path

from axiom_graph.index import builder, db
from axiom_graph.index.status import NOT_FOUND
from axiom_graph.models import make_edge
from axiom_graph.scanners import module_scanner

_VIRTUAL_LOCATIONS = frozenset({"", "external"})


def _normalized_location(location: str | None) -> str:
    return (location or "").replace("\\", "/")


def live_node_types(
    project_root: Path,
    all_nodes: list,
    index_rows: list[tuple],
    *,
    walked: Collection[str] = (),
) -> dict[str, str]:
    """Return ``{node_id: node_type}`` for every node a link may target, from every index row.

    Args:
        project_root: Absolute project root, for the file-existence check.
        all_nodes: Every node the build's scanners produced.
        index_rows: ``(id, node_type, location, own_status)`` for every node
            the index held before the build's upserts.
        walked: Files known to exist.

    Returns:
        The live node ids mapped to their node types.
    """
    rescanned = {_normalized_location(node.location) for node in all_nodes} - _VIRTUAL_LOCATIONS
    file_exists: dict[str, bool] = dict.fromkeys(walked, True)
    live: dict[str, str] = {}
    for node_id, node_type, raw_location, own_status in index_rows:
        if own_status == NOT_FOUND:
            continue
        location = _normalized_location(raw_location)
        if location not in _VIRTUAL_LOCATIONS:
            if location in rescanned:
                continue
            if location not in file_exists:
                file_exists[location] = (project_root / location).exists()
            if not file_exists[location]:
                continue
        live[node_id] = node_type
    for node in all_nodes:
        live[node.id] = node.node_type
    return live


def read_reexport_relation(conn) -> tuple[dict[str, list[str]], dict[str, list[tuple[str, dict[str, str]]]]]:
    """Read the whole re-export relation from every ``depends_on`` row.

    Args:
        conn: Open connection to the axiom-graph database.

    Returns:
        ``(star, named)`` as :func:`builder.reexport_relation_from_rows` builds them.
    """
    return builder.reexport_relation_from_rows(
        (row[0], row[1], row[2])
        for row in conn.execute("SELECT from_id, to_id, meta FROM edges WHERE edge_type = 'depends_on'")
    )


def resolve_delegate_targets(
    db_path: Path,
    all_edges: list,
    warnings: list[str],
    live_ids: Container[str] | None = None,
    rescanned_ids: Container[str] | None = None,
) -> dict[str, int]:
    """Resolve link targets as the build did with the whole relation read up front.

    Same arguments and return as ``builder._resolve_delegate_targets``.
    """
    resolved = {"delegates_to": 0, "validates": 0}
    validates_written = 0
    if not any(edge.edge_type in resolved for edge in all_edges):
        return {**resolved, "validates_written": validates_written}
    rescanned = rescanned_ids if rescanned_ids is not None else ()
    dropped: set[int] = set()
    with db._connect(db_path) as conn:
        if live_ids is None:

            def node_exists(node_id: str) -> bool:
                return conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone() is not None

        else:

            def node_exists(node_id: str) -> bool:
                return node_id in live_ids

        star, named = read_reexport_relation(conn)
        reexport_sources = builder.reexport_hops(star, named)

        def star_sources(module_id: str, symbol: str) -> list[tuple[int, str, str]]:
            return [(builder._STAR_HOP, source, symbol) for source in star.get(module_id, ())]

        unresolved = {"delegates_to": 0, "validates": 0}
        for index, edge in enumerate(all_edges):
            if edge.edge_type not in resolved or node_exists(edge.to_id):
                continue
            chained = bool(edge.meta and edge.meta.get(module_scanner.UNSPELLED_CHAIN_KEY))
            module_id, _, symbol = edge.to_id.rpartition("::")
            new_target = None
            if module_id and symbol and not (chained and edge.edge_type == "validates"):
                new_target = builder.resolve_symbol_through_reexports(
                    module_id, symbol, node_exists, star_sources if chained else reexport_sources
                )
            if new_target is None or new_target == edge.to_id:
                if edge.edge_type == "validates":
                    dropped.add(index)
                if not chained and module_id not in rescanned:
                    unresolved[edge.edge_type] += 1
                continue
            new_edge = make_edge(edge.edge_type, edge.from_id, new_target, meta=edge.meta)
            all_edges[index] = new_edge
            added = db.upsert_edge_conn(conn, new_edge)
            resolved[edge.edge_type] += 1
            if added and edge.edge_type == "validates":
                validates_written += 1

        markers_outside_rescans = any(module not in rescanned for module in named)
        if (unresolved["delegates_to"] or unresolved["validates"]) and not markers_outside_rescans:
            warnings.append(
                f"{unresolved['delegates_to']} delegate link(s) and {unresolved['validates']} validates "
                "link(s) name no live node, and no module outside this build's rescans records named "
                "re-export markers, so link resolution could not follow the names packages re-export. "
                "Markers are written when a "
                "file is scanned: an index built before they existed lacks them, and the mtime fast-pass "
                "keeps skipping the files that would supply them. Remedy (non-destructive; not init, "
                "which deletes the index): clear the stored file mtimes (UPDATE nodes SET file_mtime = "
                "NULL), then run axiom-graph build to rescan every file."
            )
    if dropped:
        all_edges[:] = [edge for index, edge in enumerate(all_edges) if index not in dropped]
    return {**resolved, "validates_written": validates_written}
