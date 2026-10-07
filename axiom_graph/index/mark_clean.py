"""Shared mark_clean logic -- compute current hashes and reset baselines.

All three mark_clean entry points (MCP, CLI, viz server) delegate to
``mark_node_clean`` so the hashing, verification snapshot, and baseline
update logic is in one place.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from axiom_annotations import task

from axiom_graph.index import db
from axiom_graph.index.dependency_set import (
    Hashes,
    LazyDependencyGraph,
    LiveView,
    dependency_set,
    dependency_targets,
    pair_hashes,
)
from axiom_graph.scanners import node_hashing
from axiom_graph.scanners.node_hashing import current_node_hash

if TYPE_CHECKING:
    from axiom_graph.models import AxiomNode

logger = logging.getLogger(__name__)

# Provenance recorded on every verification history row: which operation
# wrote it.  Rides the existing ``node_history.meta`` JSON payload (no new
# column, no migration).  ``db/history.py`` reads the same key/value pair
# with inline SQL literals -- keep the two in step.
VERIFICATION_OP_META_KEY = "verification_op"
VERIFICATION_OP_MARK_CLEAN = "mark_clean"
VERIFICATION_OP_REVERIFY = "reverify"
#: A test function's first scan: the build baselines it (see
#: :func:`axiom_graph.db.nodes.write_baseline_verifications_conn`).
VERIFICATION_OP_SCAN_BASELINE = "scan_baseline"
#: A DocJSON section that arrived carrying a valid tool-write stamp whose
#: ``verified_against`` hashes still match the code it documents.
VERIFICATION_OP_TOOL_STAMP = "tool_stamp"
#: A raw DocJSON edit accepted through ``axiom-graph stamps accept`` /
#: ``axiom_graph_accept_doc_edits``.
VERIFICATION_OP_ACCEPT_RAW_DOCJSON_EDIT = "accept_raw_docjson_edit"
#: A doc tool's write: it verifies the text it wrote (see :func:`verify_text_conn`).
VERIFICATION_OP_DOC_EDIT = "doc_edit"
#: A verification made in another checkout's index (a worktree) and copied into
#: this one after a merge, with the pairs it recorded there (see
#: :func:`carry_verification_conn`).
VERIFICATION_OP_CARRY_FORWARD = "carry_forward"
#: Meta key of a carried verification's provenance: the branch, the SHA, and
#: the worktree's latest verification of the node (verifier, time, op).
CARRIED_FROM_META_KEY = "carried_from"

#: Meta key of a partial verification row, and its values: a text-only
#: verification (a doc edit, or a partial carry with the text), a partial
#: carry of link receipts only, and a link-only verification (a reverify
#: cascade member whose own change nobody reviewed).  Readers that take a
#: verification row as evidence about all of a node's links, or about its
#: content, skip every row carrying the key; ``db/history.py`` matches it as
#: ``json.dumps`` writes it -- keep the two in step.
VERIFIES_META_KEY = "verifies"
VERIFIES_TEXT = "text"
VERIFIES_RECEIPTS = "receipts"
VERIFIES_LINKS = "links"
#: Meta key listing the receipt targets a partial carry recorded.
RECEIPTS_META_KEY = "receipts"
#: Meta key listing the offenders a doc write named in ``addresses=``.
ADDRESSES_META_KEY = "addresses"

#: ``verified_by`` values of the build-written verifications.  The post-build
#: re-stamp in :func:`axiom_graph.lifecycle.api.build_index` is limited to
#: rows still carrying one of these, so a real verification that lands in
#: between is never rewritten.
VERIFIED_BY_SCAN_BASELINE = "agent:scan-baseline"
VERIFIED_BY_TOOL_STAMP = "agent:tool-stamp"


def compute_current_hashes(
    node: "AxiomNode",
    project_root: Path,
) -> tuple[str | None, str | None]:
    """Compute current (code_hash, desc_hash) for a node from the file on disk.

    Thin wrapper around
    :func:`axiom_graph.scanners.node_hashing.current_node_hash` -- the
    consolidated primitive used by both ``mark_clean`` and
    ``compute_staleness`` step 2.  Centralising the dispatch table
    eliminates the qualified-name vs short-name disagreement that
    caused chronic ``CONTENT_UPDATED`` churn for sibling-class tests
    and ``@workflow`` / ``@task`` envelopes.  See
    :mod:`axiom_graph.scanners.node_hashing` for the dispatch ladder
    and rationale.

    Falls back to stored DB hashes if the file cannot be parsed or the
    node is not found in the file.

    Args:
        node: The AxiomNode to compute hashes for.
        project_root: Absolute path to the project root.

    Returns:
        Tuple of (code_hash, desc_hash) representing the current file
        state.  Either element may be ``None`` (envelopes always return
        ``desc_hash=None``; functions without docstrings have no
        ``desc_hash``).
    """
    return current_node_hash(node, project_root)


_PAIR_EDGE_TYPES = ("documents", "validates", "annotates")


class PairRecorder:
    """The pairs a verification records: one per dependency target, at that target's live hash.

    One recorder per write call, shared by every node the call verifies, so
    the links each node reads are loaded once and each file is read and
    parsed once (:func:`~axiom_graph.scanners.node_hashing.current_node_hashes_for_file`,
    the primitive the staleness engine compares with), for the verified
    nodes' own hashes and their targets' alike.  Only the links and nodes the
    call's verifications read are loaded, never the whole graph.  Targets
    are read from disk, so a verification absorbs an unbuilt change to a
    dependency it was checked against; with ``from_index=True`` they are
    read from the index instead (stored live hashes, else baselines), which
    a reverify cascade uses so it never absorbs an edit nobody has built.

    Every read goes through the connection the caller passes, so a writer
    that has just upserted nodes and edges in its own transaction sees them.

    Args:
        project_root: Absolute path to the project root.
        from_index: Read target hashes from the index rather than the disk.

    Attributes:
        files_parsed: How many file reads and parses the recorder made (a
            work count: one per distinct file, plus one per node a file's
            parse could not locate).
    """

    def __init__(self, project_root: Path, *, from_index: bool = False) -> None:
        self._root = project_root
        self._from_index = from_index
        self._conn = None
        self._ready: bool | None = None
        self._graph: LazyDependencyGraph | None = None
        self._view: LiveView | None = None
        self._live: dict[str, Hashes | None] = {}
        self._locations: dict[str, str | None] = {}
        self._file_hashes: dict[str, dict[str, Hashes]] = {}
        self._index_rows: dict[str, dict | None] = {}
        self.files_parsed = 0

    # -- connection and links ------------------------------------------------

    def _bind(self, conn) -> bool:
        """Read through *conn* from now on; return whether the index stores pairs."""
        self._conn = conn
        if self._graph is not None:
            self._graph.rebind(conn)
        if self._ready is None:
            self._ready = db.pairs_ready(conn)
        return bool(self._ready)

    def _graph_for(self, conn) -> LazyDependencyGraph:
        if self._graph is None:
            self._graph = LazyDependencyGraph(conn)
            if self._from_index:
                self._view = LiveView(hashes=self._index_hashes, missing=self._index_missing)
            else:
                self._view = LiveView(hashes=self._disk_hashes, missing=self._disk_missing)
        return self._graph

    def prepare(self, conn, nodes: list["AxiomNode"]) -> None:
        """Load, before any write, everything the call's verifications read.

        The links of *nodes*, the identities of their dependency targets
        (digest members included), each target's location (disk) or stored
        live hashes (index), and one parse of each file the nodes and the
        disk targets live in.  Reading the index targets now keeps the
        call's own baseline resets out of the pairs it records.

        Args:
            conn: Open connection (the call's transaction).
            nodes: The nodes the call verifies.
        """
        ready = self._bind(conn)
        self._load_files({n.location for n in nodes if n.location})
        if not ready or not nodes:
            return
        graph = self._graph_for(conn)
        ids = [n.id for n in nodes]
        graph.prefetch(ids)
        self._prefetch_targets(dependency_targets(graph, ids))

    def _prefetch_targets(self, targets: set[str]) -> None:
        graph = self._graph
        assert graph is not None
        graph.kinds.prefetch(targets)  # type: ignore[attr-defined]
        if self._from_index:
            need = [t for t in targets if t not in self._index_rows]
            if need:
                rows = db.get_live_rows_conn(self._conn, need)
                for t in need:
                    self._index_rows[t] = rows.get(t)
        else:
            self._load_locations(targets)
            self._load_files({loc for t in targets if (loc := self._locations.get(t))})

    def pairs_for(self, conn, node_id: str) -> dict[str, tuple[str, str | None]] | None:
        """Return the pairs a verification of *node_id* records now.

        Args:
            conn: Open connection to the index (the caller's transaction).
            node_id: The node being verified.

        Returns:
            Target id -> ``(code_hash, desc_hash)``; ``None`` on an index below
            schema v5, which stores no pairs.
        """
        if not self._bind(conn):
            return None
        graph = self._graph_for(conn)
        deps = dependency_set(graph, node_id)
        if not deps:
            return {}
        self._prefetch_targets(dependency_targets(graph, [node_id]))
        assert self._view is not None
        pairs: dict[str, tuple[str, str | None]] = {}
        for target, kinds in deps.items():
            if target not in self._live:
                self._live[target] = self._view.value(graph, target)
            live = self._live[target]
            if live is None or not live[0]:
                continue
            code, desc = pair_hashes(kinds, live)
            pairs[target] = (code, desc)
        return pairs

    def dependency_ids(self, conn, node_id: str) -> set[str]:
        """Return the ids of every dependency target of *node_id* (its links a verification settles).

        Args:
            conn: Open connection to the index (the caller's transaction).
            node_id: The node being verified.

        Returns:
            The target ids; empty on an index below schema v5.
        """
        if not self._bind(conn):
            return set()
        return set(dependency_set(self._graph_for(conn), node_id))

    # -- own hashes --------------------------------------------------------

    def current_hashes(self, conn, node: "AxiomNode") -> tuple[str | None, str | None]:
        """Return *node*'s current ``(code_hash, desc_hash)`` from the disk, from its file's one parse.

        The same values as :func:`compute_current_hashes`, which it falls back
        to for a node its file's parse could not locate (stored hashes, as
        that function returns).

        Args:
            conn: Open connection (the call's transaction).
            node: The node being verified.

        Returns:
            ``(code_hash, desc_hash)``.
        """
        self._conn = conn
        if node.location:
            self._load_files({node.location})
            hit = self._file_hashes.get(node.location, {}).get(node.id)
            if hit is not None:
                return hit
        self.files_parsed += 1
        return compute_current_hashes(node, self._root)

    # -- loaders (batched) ---------------------------------------------------

    def _load_locations(self, node_ids) -> None:
        need = sorted({n for n in node_ids if n not in self._locations})
        for start in range(0, len(need), 500):
            chunk = need[start : start + 500]
            rows = self._conn.execute(
                f"SELECT id, location FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk
            )
            found = {r["id"]: r["location"] for r in rows}
            for n in chunk:
                self._locations[n] = found.get(n) or None

    def _load_files(self, locations) -> None:
        """Parse each of *locations* not parsed yet, once, over every node the index holds there."""
        need = sorted({loc for loc in locations if loc and loc not in self._file_hashes})
        if not need:
            return
        on_disk = [loc for loc in need if (self._root / loc).exists()]
        for loc in need:
            self._file_hashes[loc] = {}
        by_location: dict[str, list] = {}
        for start in range(0, len(on_disk), 500):
            chunk = on_disk[start : start + 500]
            for r in self._conn.execute(f"SELECT * FROM nodes WHERE location IN ({','.join('?' * len(chunk))})", chunk):
                by_location.setdefault(r["location"], []).append(db._row_to_node(r))
        for loc in on_disk:
            self.files_parsed += 1
            self._file_hashes[loc] = node_hashing.current_node_hashes_for_file(
                self._root / loc, by_location.get(loc, []), self._root
            )

    # -- live views ----------------------------------------------------------

    def _disk_hashes(self, node_id: str) -> Hashes | None:
        if node_id not in self._locations:
            self._load_locations([node_id])
        location = self._locations.get(node_id)
        if location is None:
            return None
        self._load_files({location})
        return self._file_hashes[location].get(node_id)

    def _disk_missing(self, node_id: str) -> bool:
        return self._disk_hashes(node_id) is None

    def _index_row(self, node_id: str) -> dict | None:
        if node_id not in self._index_rows:
            self._index_rows[node_id] = db.get_live_rows_conn(self._conn, [node_id]).get(node_id)
        return self._index_rows[node_id]

    def _index_hashes(self, node_id: str) -> Hashes | None:
        """The DB-only live value (:func:`axiom_graph.db.load_live_view_conn`'s rule), per node."""
        row = self._index_row(node_id)
        if row is None:
            return None
        live = row["live_code_hash"]
        if live and live != db.MISSING_LIVE_HASH:
            return live, row["live_desc_hash"]
        return (row["code_hash"], row["desc_hash"]) if row["code_hash"] else None

    def _index_missing(self, node_id: str) -> bool:
        row = self._index_row(node_id)
        return row is not None and row["own_status"] == "NOT_FOUND"


@task(
    purpose="Record verification history, compute current hashes, and write the verification snapshot, its "
    "dependency pairs and the reset baseline in one transaction; the history row is the journal row the next "
    "refresh reads (mark_clean_nodes refreshes right after, unless its caller defers it)",
    inputs="db_path, project_root, AxiomNode, reason string, verified_by identifier, verification_op provenance marker",
    outputs="None — side effects: history row, verification snapshot, and baseline reset written to DB",
)
def mark_node_clean(
    db_path: Path,
    project_root: Path,
    node: "AxiomNode",
    reason: str,
    verified_by: str,
    *,
    verification_op: str = VERIFICATION_OP_MARK_CLEAN,
    pairs: PairRecorder | None = None,
    conn=None,
) -> None:
    """Record verification and reset baseline hashes for one node.

    This is the shared logic for all mark_clean entry points. It:
    1. Inserts a history row (AGENT_VERIFIED or MANUAL_VERIFIED).
    2. Computes current hashes from the file on disk.
    3. Writes a verification snapshot with those hashes.
    4. Records one pair per dependency target -- the target's current
       hash -- with the snapshot, in the same transaction (schema v5).
    5. Resets the baseline code_hash/desc_hash on the nodes table.

    The pairs record only the node's own outbound dependencies, so
    verifying a node never settles the nodes that depend on it.

    It deliberately does NOT advance ``file_mtime``.  That column is the
    builder's scan-skip cache; advancing it here would make the next build
    skip the file and freeze the node's scan-derived summary (``level_1`` /
    ``level_2``).  See :func:`axiom_graph.db.nodes.update_node_baseline`.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        project_root: Absolute path to the project root.
        node: The AxiomNode to mark clean.
        reason: Brief explanation for the verification.
        verified_by: Identifier (e.g. ``'human'``, ``'agent:model'``).
        verification_op: Which operation wrote this verification --
            :data:`VERIFICATION_OP_MARK_CLEAN` (the default),
            :data:`VERIFICATION_OP_REVERIFY`, or one of the build/write
            provenance constants above.  Recorded in the history
            row's ``meta`` payload under
            :data:`VERIFICATION_OP_META_KEY`.  The payload is written
            whether or not a *reason* was supplied, so a blank-reason
            verification still carries its provenance.
        pairs: The call's :class:`PairRecorder`, shared across a batch so
            each file is read and parsed once.  ``None`` uses a fresh one
            that reads the disk.
        conn: The call's open connection; every read and write of this node
            goes through it, in the caller's transaction.  ``None`` opens one
            connection for this node.
    """
    recorder = pairs if pairs is not None else PairRecorder(project_root)
    if conn is None:
        with db._connect(db_path) as own:
            _mark_node_clean_conn(own, project_root, node, reason, verified_by, verification_op, recorder)
        return
    _mark_node_clean_conn(conn, project_root, node, reason, verified_by, verification_op, recorder)


def _mark_node_clean_conn(
    conn,
    project_root: Path,
    node: "AxiomNode",
    reason: str,
    verified_by: str,
    verification_op: str,
    recorder: PairRecorder,
) -> None:
    """:func:`mark_node_clean` on an open connection."""
    cur_code, cur_desc = recorder.current_hashes(conn, node)
    _write_verification_conn(
        conn,
        node.id,
        cur_code,
        cur_desc,
        pairs=lambda: recorder.pairs_for(conn, node.id),
        reason=reason,
        verified_by=verified_by,
        meta={"reason": reason, VERIFICATION_OP_META_KEY: verification_op},
    )


def _write_verification_conn(
    conn,
    node_id: str,
    cur_code: str | None,
    cur_desc: str | None,
    *,
    pairs,
    reason: str,
    verified_by: str,
    meta: dict,
) -> None:
    """Write one full verification of *node_id* at the given hashes: history row, record, pairs, baseline reset.

    The one writer :func:`mark_node_clean` and :func:`carry_verification_conn`
    share.

    Args:
        conn: The call's open connection (its transaction).
        node_id: The verified node.
        cur_code: The node's code hash the verification is for.
        cur_desc: The node's desc hash the verification is for.
        pairs: Callable returning the pairs to record (target id ->
            ``(code_hash, desc_hash)``), or ``None`` on an index that stores
            none.  Called after the history row is written, as the recorder
            has always been.
        reason: Free-form reason stored on the verification record.
        verified_by: Verifier identifier; also picks the history row type.
        meta: The history row's meta payload.
    """
    # Write-time evidence for the effective-change rule: a reset that finds
    # the hashes already back at the stored baseline while the node is still
    # persisted stale records the realignment first, so the open change is
    # cancelled rather than committed by this reset (see
    # db.history.effective_change_rows_conn).
    if db._get_node_hashes_conn(conn, node_id) == (cur_code, cur_desc):
        db.record_realigned_if_stale_conn(conn, node_id)

    change_type = "AGENT_VERIFIED" if verified_by.startswith("agent") else "MANUAL_VERIFIED"
    db.insert_history_row_conn(conn, node_id=node_id, change_type=change_type, meta=json.dumps(meta), preserved=True)

    node_pairs = pairs()
    db.upsert_verification_conn(
        conn,
        node_id=node_id,
        verified_by=verified_by,
        code_hash_at=cur_code,
        desc_hash_at=cur_desc,
        reason=reason or None,
    )
    if node_pairs is not None:
        db.replace_verification_targets_conn(conn, node_id, node_pairs)
    # Reset baseline hashes so the next compute_staleness re-parses the
    # file, finds baseline == current, and resolves to VERIFIED.
    # file_mtime is intentionally left untouched -- see
    # update_node_baseline.
    db.update_node_baseline_conn(conn, node_id, cur_code, cur_desc)


@task(
    purpose="Write a verification copied from another checkout's index: one history row carrying the carry_forward "
    "op and its provenance, the verification record at the node's hashes here, the pairs it recorded there, and the "
    "baseline reset, on the caller's connection",
    inputs="open connection, node id, its hashes here, the worktree record's verifier, reason, the recorded pairs, "
    "provenance",
    outputs="None (side effects: history row, verification record, pairs, baseline reset)",
)
def carry_verification_conn(
    conn,
    node_id: str,
    code_hash: str,
    desc_hash: str | None,
    *,
    verified_by: str,
    reason: str,
    pairs: Mapping[str, tuple[str, str | None]],
    carried_from: Mapping[str, str | None],
) -> None:
    """Write a verification another checkout made, with the pairs it recorded there.

    The verification is written as :func:`mark_node_clean` writes one, at the
    node's hashes in this index (the caller has checked they equal the
    verified ones) and on this index's clock, but its pairs are the recorded
    ones, copied, rather than computed here.  The history row's meta carries
    :data:`VERIFICATION_OP_CARRY_FORWARD` and, under
    :data:`CARRIED_FROM_META_KEY`, where the verification came from: the
    worktree's latest verification of the node, which may be a doc tool's
    text-only write made after the verification record.

    Args:
        conn: The call's open connection (its transaction).
        node_id: The node.
        code_hash: Its code hash here.
        desc_hash: Its desc hash here.
        verified_by: The worktree verification record's verifier, kept on the record.
        reason: The reason to store (names the source and the worktree's
            latest verifier).
        pairs: The recorded pairs to copy.
        carried_from: Provenance: ``branch``, ``sha``, ``verified_by``,
            ``verified_at``, ``verification_op``.
    """
    copied = dict(pairs)
    ready = db.pairs_ready(conn)
    _write_verification_conn(
        conn,
        node_id,
        code_hash,
        desc_hash,
        pairs=lambda: copied if ready else None,
        reason=reason,
        verified_by=verified_by,
        meta={
            "reason": reason,
            VERIFICATION_OP_META_KEY: VERIFICATION_OP_CARRY_FORWARD,
            CARRIED_FROM_META_KEY: dict(carried_from),
        },
    )


@task(
    purpose="Verify a node's text only, on the caller's connection: a history row marked as a text verification, the "
    "baseline reset and the snapshot hashes; receipts, verified_at and every link status are left as they were, "
    "except the named receipts an addresses= call refreshes",
    inputs="open connection, PairRecorder, AxiomNode, reason, verified_by, verification op, open offenders to pin, "
    "named receipts",
    outputs="None (side effects: history row, snapshot hashes or a new verification row with pins, named receipts, "
    "baseline reset)",
    critical="Never replaces the node's receipts or moves verified_at, so a doc edit clears no LINKED_STALE; a row it "
    "has to create pins every open offender that holds no receipt",
)
def verify_text_conn(
    conn,
    recorder: PairRecorder,
    node: "AxiomNode",
    *,
    reason: str,
    verified_by: str,
    verification_op: str = VERIFICATION_OP_DOC_EDIT,
    open_targets: Iterable[str] = (),
    receipts: Mapping[str, tuple[str, str | None]] | None = None,
    addresses: Collection[str] = (),
) -> None:
    """Record a text-only verification of *node*: its own status, never its links.

    What a doc tool's write verifies.  It writes:

    1. one ``AGENT_VERIFIED`` / ``MANUAL_VERIFIED`` history row whose meta
       carries *verification_op*, ``verifies: "text"`` and, when names were
       given, ``addresses``;
    2. the node's snapshot hashes (an existing row keeps ``verified_at``,
       ``verified_by``, ``reason`` and its receipts);
    3. a new verification row when the node has none, with an open receipt
       (:data:`axiom_graph.db.OPEN_RECEIPT_HASH`) for every open offender in
       *open_targets* that is not given a receipt, so the new row's time
       settles none of them;
    4. the named *receipts* (``addresses=``), replacing those targets' pairs
       only;
    5. the baseline reset, as :func:`mark_node_clean` does.

    Args:
        conn: The call's open connection (its transaction).
        recorder: The call's :class:`PairRecorder` (own hashes: one parse per file).
        node: The node whose text was written.
        reason: Brief explanation.
        verified_by: Identifier recorded on a new row and choosing the history type.
        verification_op: Provenance recorded in the history row's meta.
        open_targets: The node's open offenders that a receipt could hold.
        receipts: Target id -> the pair to record (an ``addresses=`` refresh).
        addresses: The names the call gave, recorded in the history row.
    """
    cur_code, cur_desc = recorder.current_hashes(conn, node)
    meta: dict = {"reason": reason, VERIFICATION_OP_META_KEY: verification_op, VERIFIES_META_KEY: VERIFIES_TEXT}
    if addresses:
        meta[ADDRESSES_META_KEY] = sorted(set(addresses))
    _write_partial_verification_conn(
        conn,
        node.id,
        cur_code,
        cur_desc,
        text=True,
        meta=meta,
        verified_by=verified_by,
        reason=reason,
        open_targets=open_targets,
        receipts=receipts,
        has_row=None,
    )


def _write_partial_verification_conn(
    conn,
    node_id: str,
    cur_code: str | None,
    cur_desc: str | None,
    *,
    text: bool,
    meta: dict,
    verified_by: str,
    reason: str,
    open_targets: Iterable[str],
    receipts: Mapping[str, tuple[str, str | None]] | None,
    has_row: bool | None,
    links: bool = False,
) -> None:
    """Write a verification of a node's text and/or named receipts, or of all its links, never both.

    The one writer :func:`verify_text_conn`,
    :func:`carry_partial_verification_conn` and :func:`verify_links_conn`
    share.  An existing verification row keeps ``verified_at``,
    ``verified_by``, ``reason`` and every receipt not named; a row this has
    to create pins every target in *open_targets* that is not given a
    receipt, so its time settles none of them.

    With *links* the write is the other way round: *receipts* are every
    pair the node's links record now and replace its whole pair set, and
    ``verified_at`` / ``verified_by`` / ``reason`` move to now (the time
    settles the clock-ruled links); the snapshot hashes and the baseline do
    not move, so the node's own status stays what it was.  A row it has to
    create takes *cur_code* / *cur_desc* as its snapshot: the caller passes
    the stored baseline (the last reviewed content), never the disk.

    Args:
        conn: The call's open connection (its transaction).
        node_id: The node.
        cur_code: Its code hash the verification is for.
        cur_desc: Its desc hash the verification is for.
        text: Verify the node's text: update the snapshot hashes and reset the
            baseline.  ``False`` writes the receipts only.
        meta: The history row's meta payload (carries ``verifies``).
        verified_by: Identifier recorded on a new row and choosing the history type.
        reason: Reason recorded on a new row.
        open_targets: Targets to pin open on a new row unless given a receipt.
        receipts: Target id -> the pair to record, replacing those targets' pairs only.
        has_row: Whether the node has a verification row, when the caller
            already knows; ``None`` lets a text write learn it from the
            snapshot update.  A receipts-only write needs it.
        links: Verify every link (see above); *text* must be ``False``.
    """
    if text and links:
        raise ValueError("a verification is of the text or of the links, not both")
    if text and db._get_node_hashes_conn(conn, node_id) == (cur_code, cur_desc):
        db.record_realigned_if_stale_conn(conn, node_id)

    change_type = "AGENT_VERIFIED" if verified_by.startswith("agent") else "MANUAL_VERIFIED"
    db.insert_history_row_conn(conn, node_id=node_id, change_type=change_type, meta=json.dumps(meta), preserved=True)

    named = dict(receipts or {})
    ready = db.pairs_ready(conn)
    if text:
        has_row = db.update_verification_snapshot_conn(conn, node_id, cur_code, cur_desc)
    elif links:
        has_row = db.touch_verification_conn(conn, node_id, verified_by, reason or None)
    elif has_row is None:
        raise ValueError("a receipts-only verification needs has_row")
    if not has_row:
        db.upsert_verification_conn(
            conn,
            node_id=node_id,
            verified_by=verified_by,
            code_hash_at=cur_code,
            desc_hash_at=cur_desc,
            reason=reason or None,
        )
        if ready and not links:
            db.pin_verification_targets_conn(conn, node_id, set(open_targets) - set(named))
    if links and receipts is not None and ready:
        db.replace_verification_targets_conn(conn, node_id, named)
    elif named and ready:
        db.refresh_verification_targets_conn(conn, node_id, named)
    if text:
        db.update_node_baseline_conn(conn, node_id, cur_code, cur_desc)


@task(
    purpose="Verify a node's links only, on the caller's connection: a history row marked as a link verification, "
    "every dependency pair the recorder reads now and verified_at moved to now; the snapshot hashes and the baseline "
    "are left as they were, so an own change nobody reviewed stays flagged",
    inputs="open connection, PairRecorder, AxiomNode, reason, verified_by, verification op",
    outputs="None (side effects: history row, verification time and pairs, or a new verification row whose snapshot "
    "is the stored baseline)",
    critical="Never moves the snapshot hashes or the baseline: a CONTENT_UPDATED / DESC_UPDATED node stays so",
)
def verify_links_conn(
    conn,
    recorder: PairRecorder,
    node: "AxiomNode",
    *,
    reason: str,
    verified_by: str,
    verification_op: str = VERIFICATION_OP_MARK_CLEAN,
) -> None:
    """Record a link-only verification of *node*: its links, never its own content.

    What a reverify cascade writes for a dependent whose own content changed
    and was not reviewed: the source's change does not invalidate the
    dependent, which is a claim about its links only.  It writes:

    1. one ``AGENT_VERIFIED`` / ``MANUAL_VERIFIED`` history row whose meta
       carries *verification_op* and ``verifies: "links"``;
    2. ``verified_at`` / ``verified_by`` / ``reason`` moved to now on the
       node's verification row, or a new row whose snapshot is the stored
       baseline (the last reviewed content) when it has none;
    3. every dependency pair *recorder* reads now, replacing the old set.

    The snapshot hashes and the baseline do not move, so verification
    promotion never turns the node's own change into VERIFIED.

    Args:
        conn: The call's open connection (its transaction).
        recorder: The call's :class:`PairRecorder` (a reverify cascade reads
            the index's hashes).
        node: The node whose links were verified.
        reason: Brief explanation (the cascade's ``[reverify:<source>]`` reason).
        verified_by: Identifier recorded on the row and choosing the history type.
        verification_op: Provenance recorded in the history row's meta.
    """
    meta = {"reason": reason, VERIFICATION_OP_META_KEY: verification_op, VERIFIES_META_KEY: VERIFIES_LINKS}
    base_code, base_desc = db._get_node_hashes_conn(conn, node.id)
    _write_partial_verification_conn(
        conn,
        node.id,
        base_code,
        base_desc,
        text=False,
        links=True,
        meta=meta,
        verified_by=verified_by,
        reason=reason,
        open_targets=(),
        receipts=recorder.pairs_for(conn, node.id),
        has_row=None,
    )


@task(
    purpose="Write the part of a verification another checkout made that carries here, on the caller's connection: "
    "one history row carrying the carry_forward op, what it verifies and its provenance, the text verification "
    "(snapshot and baseline reset) when the text carries, and the carried receipts; verified_at and every other "
    "receipt are left as they were",
    inputs="open connection, node id, its hashes here, text flag, receipts to record, targets to pin on a new row, "
    "whether the node has a verification row, the worktree record's verifier, reason, provenance",
    outputs="None (side effects: history row, snapshot hashes or a new verification row with pins, the carried "
    "receipts, baseline reset when the text carries)",
    critical="Never moves verified_at or replaces a receipt it was not given, so it clears no LINKED_STALE the "
    "worktree's receipts do not settle at this version; a row it has to create pins every open offender it gives no "
    "receipt",
)
def carry_partial_verification_conn(
    conn,
    node_id: str,
    code_hash: str,
    desc_hash: str | None,
    *,
    text: bool,
    receipts: Mapping[str, tuple[str, str | None]],
    open_targets: Iterable[str],
    has_row: bool,
    verified_by: str,
    reason: str,
    carried_from: Mapping[str, str | None],
) -> None:
    """Write a partial carry: a node's own-text verification and/or some of its link receipts.

    The history row's meta carries :data:`VERIFICATION_OP_CARRY_FORWARD`,
    ``verifies`` (``"text"`` when the text carries, else ``"receipts"``), the
    carried receipt targets under ``receipts`` and, under
    :data:`CARRIED_FROM_META_KEY`, where the verification came from.  Readers
    that take a verification row as evidence about all of a node's links skip
    it, as they skip a doc edit's.

    Args:
        conn: The call's open connection (its transaction).
        node_id: The node.
        code_hash: Its code hash here (equal to the worktree's).
        desc_hash: Its desc hash here (equal to the worktree's).
        text: Carry the own-text verification.
        receipts: Target id -> the worktree's receipt to record.
        open_targets: The open offenders given no receipt: pinned open on a
            row this has to create.
        has_row: The node already has a verification row here (the plan read it).
        verified_by: The worktree verification record's verifier, recorded on
            a row this creates.
        reason: The reason (names the source and the worktree's latest verifier).
        carried_from: Provenance: ``branch``, ``sha``, ``verified_by``,
            ``verified_at``, ``verification_op``.
    """
    meta = {
        "reason": reason,
        VERIFICATION_OP_META_KEY: VERIFICATION_OP_CARRY_FORWARD,
        VERIFIES_META_KEY: VERIFIES_TEXT if text else VERIFIES_RECEIPTS,
        RECEIPTS_META_KEY: sorted(receipts),
        CARRIED_FROM_META_KEY: dict(carried_from),
    }
    _write_partial_verification_conn(
        conn,
        node_id,
        code_hash,
        desc_hash,
        text=text,
        meta=meta,
        verified_by=verified_by,
        reason=reason,
        open_targets=open_targets,
        receipts=receipts,
        has_row=has_row,
    )
