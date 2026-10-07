"""Carry a worktree's verifications into this checkout's index after a merge: the per-node verdict.

A worktree verifies its own code, tests and docs in its own index.  After a
merge, this checkout's build sees the merged content as changed, and the
nodes that depend on it go stale.  The carry copies the worktree's
verification of a node across when the worktree verified exactly what this
checkout now holds:

1. the worktree index reads the node VERIFIED in both dimensions and holds a
   verification record for it;
2. the node's own live hash is the same in both indexes;
3. every link a verification settles (by recorded pair or by time: the
   node's dependency set) exists in the worktree, with the same link kinds,
   and each linked node is at the version the worktree verified it against:
   the recorded pair matches the linked node's live hash here, or, for a
   link with no recorded pair (settled by time in the worktree), the linked
   node's live hash is the same in both indexes.

Condition 3 covers every such link, not only the ones that hold the node
stale here, so a verification written on this index's clock never settles a
link by time that the worktree did not verify at this version.  For an
envelope that includes every annotated function and its whole
``delegates_to`` closure.

Two passes hold a node stale from a target's own status, whatever the
node's verification says: an envelope while an annotated function or a
delegated task is itself stale (Pass A' / B'; the link is inside condition
3), and a section through a transitive doc-to-doc link to a stale section
(Pass 3).  That doc-to-doc link is the only one outside condition 3: no
verification settles it, so a carry cannot settle it wrongly.  A carried
node held either way stays stale until that target settles.

A node that fails the full carry is carried one dimension at a time (a
partial carry) when the worktree verified exactly this content: it reads own
VERIFIED there with a verification record, and its own live hash is the same
in both indexes.  The partial carry copies:

- the own-text verification, when the node's own status here has drifted
  (a text-only write: receipts and ``verified_at`` are left as they are);
- the worktree's receipt for each direct link holding the node stale here,
  when the receipt is a real pair (not an open pin), the link has the same
  kinds in both indexes, and the pair matches the linked node's live version
  here.

Every other link keeps this index's state: one settled by time in the
worktree (no receipt), left open there, absent there, or whose linked node
is at another version.  When the node has no verification row here, the row
the partial carry creates pins open each of those links that holds the node
stale here, so its time settles none of them, and also carries the
worktree's receipt for each other link that matches here (same kinds, a
real pair at the linked node's live version).  A link that holds the node
stale here is the only kind with a change the new row's time could settle,
so the row then reads exactly the offenders the worktree's receipts leave
open.  A node whose content differs carries nothing in either dimension.

Nodes stale only through what they inherit (a module, an envelope whose
composed children are stale) or through a transitive doc-to-doc link have
nothing of their own to carry: they settle when those do.

Every carry names the worktree's latest verification of the node (its last
verification history row, which may be a doc tool's text-only write) as its
provenance.

Everything here reads; :func:`axiom_graph.index.mark_clean.carry_verification_conn`
and :func:`axiom_graph.index.mark_clean.carry_partial_verification_conn`
write.  Both indexes are read through connections the caller opened (the
worktree's read-only), in batches sized by the stale nodes, never the whole
graph.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from axiom_annotations import task

from axiom_graph.index import db
from axiom_graph.index.builder import PROJECT_ID_META_KEY
from axiom_graph.index.dependency_set import (
    Hashes,
    LazyDependencyGraph,
    dependency_set,
    dependency_targets,
    is_digest_target,
    pair_hashes,
    pair_matches,
    warm_dependency_sets,
)
from axiom_graph.index.mark_clean import VERIFIES_META_KEY
from axiom_graph.index.staleness import _StoredLive
from axiom_graph.index.status import BROKEN_LINK, LINKED_STALE, NOT_FOUND, RENAMED, VERIFIED

#: Verdict: the node's worktree verification is copied.
CARRY = "carry"
#: Verdict: part of the node's worktree verification is copied (its own text and/or some link receipts).
CARRY_PARTIAL = "carry_partial"
#: Verdict: stale only through what it inherits or a transitive doc link; settles with them.
FOLLOWS = "follows"
#: Reason: absent from the worktree index, not VERIFIED there, or no verification record there.
NOT_VERIFIED = "not_verified"
#: Reason: the node's own content differs between the two indexes.
CONTENT_DIFFERS = "content_differs"
#: Reason: a link the node has here is absent in the worktree (or has other link kinds there).
LINK_ABSENT = "link_absent"
#: Reason: a linked node is not at the version the worktree verified the node against.
LINKED_NODE_DIFFERS = "linked_node_differs"
#: Reason: NOT_FOUND, RENAMED or BROKEN_LINK here, which a carry never settles.
STRUCTURAL = "structural"
#: Why a link of a partly carried node stays open: the worktree holds no receipt
#: for it (settled by time there, or left open).
NO_RECEIPT = "no_receipt"

#: The reasons a stale node is not carried, in report order.
NOT_CARRIED_REASONS: tuple[str, ...] = (NOT_VERIFIED, CONTENT_DIFFERS, LINK_ABSENT, LINKED_NODE_DIFFERS, STRUCTURAL)


@dataclass(frozen=True)
class CarryProvenance:
    """The worktree's latest verification of a node, which a carry names.

    Read from the node's last verification history row in the worktree, so a
    doc tool's text-only write (which leaves the verification record naming an
    older verifier) is named as itself.

    Attributes:
        verified_by: The verifier: the record's when the latest row is a full
            verification, else ``"agent"`` / ``"human"`` from the row's type.
        verified_at: When the worktree recorded that verification.
        reason: Its reason, or ``None``.
        op: Its ``verification_op`` (``mark_clean``, ``doc_edit``, ...), or ``None``.
    """

    verified_by: str
    verified_at: str
    reason: str | None
    op: str | None


@dataclass(frozen=True)
class CarriedVerification:
    """What a full carry writes for one node.

    Attributes:
        node_id: The node.
        code_hash: Its code hash here (equal to the worktree's).
        desc_hash: Its desc hash here (equal to the worktree's).
        verified_by: The worktree verification record's verifier, kept on the record.
        pairs: Every pair the worktree verification recorded.
        provenance: The worktree's latest verification of the node.
    """

    node_id: str
    code_hash: str
    desc_hash: str | None
    verified_by: str
    pairs: dict[str, tuple[str, str | None]]
    provenance: CarryProvenance


@dataclass(frozen=True)
class PartialCarry:
    """What a partial carry writes for one node.

    Attributes:
        node_id: The node.
        code_hash: Its code hash here (equal to the worktree's).
        desc_hash: Its desc hash here (equal to the worktree's).
        text: Carry the own-text verification (the node's own status drifted here).
        receipts: Link target -> the worktree's receipt to record (matches the target here).
            On a node with no verification row here this also holds the
            receipts for links that do not hold it stale, so the row the
            carry creates records them as the worktree did.
        open_targets: The direct links holding the node stale here that get
            no receipt (the :attr:`held` targets): pinned open when the carry
            has to create the node's verification row.
        held: ``(target, reason)`` for each direct link holding the node stale
            here that carries nothing, in id order; ``reason`` is
            :data:`LINK_ABSENT`, :data:`LINKED_NODE_DIFFERS` or :data:`NO_RECEIPT`.
        verified_by: The worktree verification record's verifier, for a row
            the carry creates.
        provenance: The worktree's latest verification of the node.
        has_row: The node has a verification row here already.
    """

    node_id: str
    code_hash: str
    desc_hash: str | None
    text: bool
    receipts: dict[str, tuple[str, str | None]]
    open_targets: frozenset[str]
    held: tuple[tuple[str, str], ...]
    verified_by: str
    provenance: CarryProvenance
    has_row: bool


@dataclass
class CarryPlan:
    """The verdict on every node stale here.

    Attributes:
        verdicts: Node id -> ``(verdict, detail)`` in stored node order.
            ``verdict`` is :data:`CARRY`, :data:`CARRY_PARTIAL`,
            :data:`FOLLOWS` or one of :data:`NOT_CARRIED_REASONS`; ``detail``
            names the link target for :data:`LINK_ABSENT` /
            :data:`LINKED_NODE_DIFFERS`, else ``None``.
        carry: The writes for the :data:`CARRY` nodes, in the same order.
        partial: The writes for the :data:`CARRY_PARTIAL` nodes, in the same order.
    """

    verdicts: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    carry: list[CarriedVerification] = field(default_factory=list)
    partial: list[PartialCarry] = field(default_factory=list)


def index_identity_conn(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """Return an index's schema version and project id, read on *conn*.

    The project id is the one ``index_meta`` stores; for an index built
    before it was stored, the one prefix every node id shares
    (:func:`axiom_graph.index.builder.indexed_project_id`'s rule).

    Args:
        conn: Open connection to the index (read-only is enough).

    Returns:
        ``(user_version, project_id)``; the id is ``None`` when the index has
        none.
    """
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    try:
        row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (PROJECT_ID_META_KEY,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None and row[0]:
        return version, row[0]
    try:
        rows = conn.execute(
            "SELECT DISTINCT substr(id, 1, instr(id, '::') - 1) FROM nodes WHERE instr(id, '::') > 1 LIMIT 2"
        ).fetchall()
    except sqlite3.OperationalError:
        return version, None
    return version, (rows[0][0] if len(rows) == 1 else None)


def _own_live(row: Mapping) -> Hashes | None:
    """A node's own live pair as the index stores it: the live hashes, else the baseline."""
    live = row["live_code_hash"]
    if live == db.MISSING_LIVE_HASH:
        return None
    if live:
        return live, row["live_desc_hash"]
    return (row["code_hash"], row["desc_hash"]) if row["code_hash"] else None


@task(
    purpose="Judge every node stale here against the worktree index: same content, same links at the verified "
    "versions, VERIFIED there with a record: carry in full; same content verified there but a link not settled at "
    "this version: carry the own text and the receipts that match here; else the reason it keeps its status.  "
    "Provenance is the worktree's latest verification of each node, read in one batch",
    inputs="open connections to this index and the worktree index (read-only), the stale nodes and their vias",
    outputs="CarryPlan: a verdict per stale node, the full carries and the partial carries to write",
    critical="Reads only; a partial carry onto a node with no verification row here pins only the open offenders "
    "it gives no receipt",
)
def plan_carry_conn(
    main_conn: sqlite3.Connection,
    worktree_conn: sqlite3.Connection,
    stale: Sequence[tuple[str, str, str]],
    vias: Mapping[str, Sequence[str]],
) -> CarryPlan:
    """Decide, for every node stale here, whether the worktree's verification of it carries.

    Args:
        main_conn: Open connection to this checkout's index (statuses refreshed).
        worktree_conn: Open (read-only) connection to the worktree's index.
        stale: ``(node_id, own_status, link_status)`` of every node not
            VERIFIED here, in stored node order.
        vias: Node id -> the vias that hold it LINKED_STALE here.

    Returns:
        The :class:`CarryPlan`.
    """
    plan = CarryPlan()
    ids = [nid for nid, _own, _link in stale]
    main_rows = db.get_live_rows_conn(main_conn, ids)
    main_graph = LazyDependencyGraph(main_conn)
    warm_dependency_sets(main_graph, ids)

    candidates: list[str] = []
    main_deps: dict[str, dict[str, frozenset[str]]] = {}
    own_drifts: dict[str, bool] = {}
    direct_vias: dict[str, list[str]] = {}
    carried: set[str] = set()
    for nid, own, link in stale:
        row = main_rows.get(nid)
        if row is None or own in (NOT_FOUND, RENAMED) or link == BROKEN_LINK:
            plan.verdicts[nid] = (STRUCTURAL, None)
            continue
        deps = dependency_set(main_graph, nid)
        live = _own_live(row)
        own_drift = (
            not is_digest_target(row["node_type"], row["subtype"])
            and live is not None
            and live != (row["code_hash"], row["desc_hash"])
        )
        direct = [v for v in vias.get(nid, ()) if v in deps]
        if not own_drift and not direct:
            plan.verdicts[nid] = (FOLLOWS, None)
            continue
        plan.verdicts[nid] = (NOT_VERIFIED, None)  # placeholder, decided below
        candidates.append(nid)
        main_deps[nid] = deps
        own_drifts[nid] = own_drift
        direct_vias[nid] = sorted(set(direct))
        if link == LINKED_STALE and not direct:
            carried.add(nid)
    if not candidates:
        return plan

    main_has_row = set(db.get_verifications_for_conn(main_conn, candidates))
    wt_rows = db.get_live_rows_conn(worktree_conn, candidates)
    wt_verifications = db.get_verifications_for_conn(worktree_conn, candidates)
    wt_latest = db.latest_verification_rows_conn(worktree_conn, candidates)
    wt_pairs = db.get_verification_targets_for_conn(worktree_conn, candidates) if db.pairs_ready(worktree_conn) else {}
    wt_graph = LazyDependencyGraph(worktree_conn)
    warm_dependency_sets(wt_graph, candidates)
    main_view = _StoredLive(None, raw_missing=False, conn=main_conn).view()
    wt_view = _StoredLive(None, raw_missing=False, conn=worktree_conn).view()
    assert main_view.prefetch is not None and wt_view.prefetch is not None
    main_view.prefetch(dependency_targets(main_graph, candidates))
    wt_view.prefetch(dependency_targets(wt_graph, candidates))

    for nid in candidates:
        row = main_rows[nid]
        wt_row = wt_rows.get(nid)
        if wt_row is None or wt_row["own_status"] != VERIFIED:
            continue  # NOT_VERIFIED
        live = _own_live(row)
        if live is None or _own_live(wt_row) != live:
            plan.verdicts[nid] = (CONTENT_DIFFERS, None)
            continue
        verification = wt_verifications.get(nid)
        if verification is None:
            continue  # NOT_VERIFIED
        recorded = wt_pairs.get(nid, {})
        wt_deps = dependency_set(wt_graph, nid)
        provenance = _provenance(verification, wt_latest.get(nid))
        blocked: tuple[str, str | None] | None = (
            (NOT_VERIFIED, None)
            if wt_row["link_status"] != VERIFIED
            else _links_verdict(main_deps[nid], wt_deps, recorded, main_graph, wt_graph, main_view, wt_view)
        )
        if blocked is None:
            plan.verdicts[nid] = (CARRY, None)
            plan.carry.append(
                CarriedVerification(
                    node_id=nid,
                    code_hash=live[0] or "",
                    desc_hash=live[1],
                    verified_by=verification["verified_by"],
                    pairs=dict(recorded),
                    provenance=provenance,
                )
            )
            continue
        receipts, held = _carryable_receipts(direct_vias[nid], main_deps[nid], wt_deps, recorded, main_graph, main_view)
        if not own_drifts[nid] and not receipts:
            plan.verdicts[nid] = blocked
            continue
        has_row = nid in main_has_row
        open_targets = {target for target, _reason in held}
        if not has_row:
            # The row the carry creates settles by its time every link it
            # gives no pair; only the links holding the node stale have a
            # change to settle, and the held ones are pinned.  The others take
            # the worktree's receipt where it matches here.
            others = sorted(set(main_deps[nid]) - set(direct_vias[nid]))
            extra, not_carried = _carryable_receipts(others, main_deps[nid], wt_deps, recorded, main_graph, main_view)
            receipts = {**receipts, **extra}
            if nid in carried:
                # A frozen section carrying LINKED_STALE names no via, so every
                # link given no receipt stays open: the new row's time must not
                # settle the cause the carry needs.
                open_targets.update(target for target, _reason in not_carried)
        plan.verdicts[nid] = (CARRY_PARTIAL, None)
        plan.partial.append(
            PartialCarry(
                node_id=nid,
                code_hash=live[0] or "",
                desc_hash=live[1],
                text=own_drifts[nid],
                receipts=dict(sorted(receipts.items())),
                open_targets=frozenset(open_targets),
                held=tuple(held),
                verified_by=verification["verified_by"],
                provenance=provenance,
                has_row=has_row,
            )
        )
    return plan


def _provenance(record: Mapping, latest: Mapping | None) -> CarryProvenance:
    """Name the worktree's latest verification of a node.

    Args:
        record: The node's verification record in the worktree.
        latest: Its latest verification history row there
            (:func:`axiom_graph.db.history.latest_verification_rows_conn`), or
            ``None`` when it has none.

    Returns:
        The :class:`CarryProvenance`: the history row's when there is one,
        else the record's.
    """
    if latest is None:
        return CarryProvenance(record["verified_by"], record["verified_at"], record["reason"], None)
    meta = latest["meta"]
    if VERIFIES_META_KEY in meta:
        # A partial verification leaves the record naming an older verifier;
        # the row names only whether an agent or a human made it.
        verified_by = "agent" if latest["change_type"] == "AGENT_VERIFIED" else "human"
    else:
        verified_by = record["verified_by"]
    return CarryProvenance(verified_by, latest["scanned_at"], meta.get("reason") or None, meta.get("verification_op"))


def _carryable_receipts(
    direct: Sequence[str],
    main_deps: Mapping[str, frozenset[str]],
    wt_deps: Mapping[str, frozenset[str]],
    recorded: Mapping[str, tuple[str, str | None]],
    main_graph,
    main_view,
) -> tuple[dict[str, tuple[str, str | None]], list[tuple[str, str]]]:
    """Split some of a node's links into receipts to carry and links that stay open.

    Args:
        direct: The links to judge, sorted: the node's direct vias here (its
            own links holding it stale), or, for a row the carry creates, its
            other links.
        main_deps: The node's dependency set here.
        wt_deps: Its dependency set in the worktree.
        recorded: The pairs its worktree verification recorded.
        main_graph: Links here.
        main_view: Live hashes here.

    Returns:
        ``(receipts, held)``: target -> the worktree's receipt, for each link
        whose receipt is a real pair over the same link kinds that matches
        the target here; ``(target, reason)`` for every other link judged.
    """
    receipts: dict[str, tuple[str, str | None]] = {}
    held: list[tuple[str, str]] = []
    for target in direct:
        kinds = main_deps[target]
        if wt_deps.get(target) != kinds:
            held.append((target, LINK_ABSENT))
            continue
        pair = recorded.get(target)
        if pair is None or pair[0] == db.OPEN_RECEIPT_HASH:
            held.append((target, NO_RECEIPT))
            continue
        main_live = main_view.value(main_graph, target)
        if main_live is None or not pair_matches(kinds, pair, main_live):
            held.append((target, LINKED_NODE_DIFFERS))
            continue
        receipts[target] = pair
    return receipts, held


def _links_verdict(
    main_deps: Mapping[str, frozenset[str]],
    wt_deps: Mapping[str, frozenset[str]],
    recorded: Mapping[str, tuple[str, str | None]],
    main_graph,
    wt_graph,
    main_view,
    wt_view,
) -> tuple[str, str] | None:
    """Return why the node's links block the carry, or ``None`` when every link was verified at this version.

    Args:
        main_deps: The node's dependency set here.
        wt_deps: Its dependency set in the worktree.
        recorded: The pairs its worktree verification recorded.
        main_graph: Links here.
        wt_graph: Links in the worktree.
        main_view: Live hashes here.
        wt_view: Live hashes in the worktree.

    Returns:
        ``(reason, target)`` for the first blocking link in id order, or ``None``.
    """
    for target in sorted(main_deps):
        kinds = main_deps[target]
        if wt_deps.get(target) != kinds:
            return LINK_ABSENT, target
        main_live = main_view.value(main_graph, target)
        pair = recorded.get(target)
        if pair is not None:
            if pair[0] == db.OPEN_RECEIPT_HASH or main_live is None or not pair_matches(kinds, pair, main_live):
                return LINKED_NODE_DIFFERS, target
            continue
        wt_live = wt_view.value(wt_graph, target)
        if (main_live is None) != (wt_live is None):
            return LINKED_NODE_DIFFERS, target
        if (
            main_live is not None
            and wt_live is not None
            and pair_hashes(kinds, main_live) != pair_hashes(kinds, wt_live)
        ):
            return LINKED_NODE_DIFFERS, target
    return None
