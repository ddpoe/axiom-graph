"""Scoped-similarity rename matcher with a real 3-method adapter seam.

ADR-013 promised *similarity* rename detection but the builder only ever did
an exact ``code_hash`` dictionary lookup.  This module supplies the missing
similarity matcher.

The matcher answers exactly one question per scoped pair: *a node disappeared
and a node appeared -- are they the same symbol?*  Git is used only as a
**scope reducer** (it keeps candidate pools tiny); the actual decision is a
single body-similarity ratio computed with :func:`difflib.SequenceMatcher.ratio`.
Exact ``code_hash`` equality short-circuits to ratio ``1.0`` (the 100%-confidence
fast path preserved from the old behaviour).

The body-similarity *core* (:func:`run_matcher`) is adapter-agnostic.  Three
adapter methods isolate everything domain-specific:

1. ``discover_scopes()`` -- enumerate scope-reduced ``(lost, found)`` pools.
2. ``old_body(lost)``    -- retrieve the prior body text for a lost node.
3. ``apply(old, new)``   -- migrate history/edges + write ``RENAMED`` status.

Only the **code** adapter (:class:`CodeRenameAdapter`) is implemented in v1.
The DocJSON prose adapter is deferred and gated on ADR-021 (decision D-4); the
seam exists and is exercised by the code adapter so the prose adapter drops in
later without restructuring the core.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value objects passed across the seam
# ---------------------------------------------------------------------------


@dataclass
class LostNode:
    """A code node present in the DB but missing from the current scan."""

    node_id: str
    code_hash: str
    location: str  # repo-relative path where the node *used to* live
    start_line: int | None
    end_line: int | None
    git_sha: str | None  # most-recent node_history git_sha (per-node baseline)


@dataclass
class FoundNode:
    """A code node discovered in the current scan (a rename-target candidate)."""

    node_id: str
    code_hash: str
    location: str  # repo-relative path where the node now lives
    body: str  # current body text read from disk
    is_new: bool  # True iff this node had no prior baseline (appeared this build)


@dataclass
class ScopePool:
    """One scope-reduced pool: nodes lost from a file vs nodes found for it.

    ``reason_if_degraded`` is set by the adapter when it already knows the
    scope cannot be similarity-scored (e.g. ``"no_git"``).  The core may
    additionally mark a scope ``"pool_cap"`` when it is too large.
    """

    lost: list[LostNode]
    found: list[FoundNode]
    reason_if_degraded: str | None = None


@dataclass
class RenameApplied:
    old_id: str
    new_id: str
    ratio: float


@dataclass
class ScoringSkipped:
    """A lost node that fell back to exact-hash and found no match.

    Its resulting ``NOT_FOUND`` may be an undetected rename -- recorded as a
    per-node ``RENAME_SCORING_SKIPPED`` history event by the builder.
    """

    node_id: str
    reason: str  # "pool_cap" | "no_git"
    candidates: int


@dataclass
class MatchResult:
    applied: list[RenameApplied] = field(default_factory=list)
    skipped: list[ScoringSkipped] = field(default_factory=list)
    # lost ids that stayed lost after scoring (genuine deletions, no marker)
    not_found: list[str] = field(default_factory=list)
    # reason -> number of scopes that degraded with that reason
    degraded_scopes: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The 3-method adapter seam
# ---------------------------------------------------------------------------


class RenameAdapter(Protocol):
    """The adapter contract.  Only the code adapter is implemented in v1."""

    threshold: float

    def discover_scopes(self) -> list[ScopePool]:
        """Enumerate scope-reduced ``(lost, found)`` candidate pools."""
        ...

    def old_body(self, lost: LostNode) -> str | None:
        """Return the prior body text for *lost*, or ``None`` if unreachable."""
        ...

    def apply(self, old_id: str, new_id: str) -> None:
        """Migrate history/edges from *old_id* to *new_id* and mark RENAMED."""
        ...


# ---------------------------------------------------------------------------
# Pure scoring helpers (Tier-1 testable, no I/O)
# ---------------------------------------------------------------------------


def body_ratio(old_body: str, new_body: str, old_hash: str, new_hash: str) -> float:
    """Return a 0..1 similarity ratio for two node bodies.

    Exact ``code_hash`` equality short-circuits to ``1.0`` (the
    100%-confidence fast path); otherwise the ratio is
    :meth:`difflib.SequenceMatcher.ratio` over the two body texts.

    Args:
        old_body: Prior body text.
        new_body: Current body text.
        old_hash: Prior node ``code_hash``.
        new_hash: Current node ``code_hash``.

    Returns:
        A float in ``[0.0, 1.0]``.
    """
    if old_hash and new_hash and old_hash == new_hash:
        return 1.0
    return difflib.SequenceMatcher(None, old_body, new_body).ratio()


def is_valid_target(found: FoundNode) -> bool:
    """Return ``True`` if *found* may be a rename target.

    A rename target must have *no prior baseline* -- it must have appeared in
    this build.  This structurally prevents welding two pre-existing nodes.
    """
    return found.is_new


# ---------------------------------------------------------------------------
# Adapter-agnostic core
# ---------------------------------------------------------------------------


def run_matcher(adapter: RenameAdapter, *, pool_cap: int) -> MatchResult:
    """Run the scoped-similarity matcher over every pool the adapter yields.

    For each scope:

    * If the scope is degraded (adapter-flagged ``no_git`` or the pool exceeds
      ``pool_cap``), fall back to exact-``code_hash`` matching **per lost
      node**.  A lost node with an exact match is a confident rename
      (applied, ratio 1.0); a lost node with no match yields a
      :class:`ScoringSkipped` record (the suspect ``NOT_FOUND``).
    * Otherwise score every ``(lost, found)`` pair with a body-similarity
      ratio, then assign greedily by descending ratio with a stable
      tie-break.  Pairs at/above ``adapter.threshold`` are applied; lost
      nodes left unmatched stayed lost (genuine deletion, no marker).  A lost
      node whose prior body is unreachable falls to per-node exact-hash with
      reason ``no_git``.

    Args:
        adapter: A :class:`RenameAdapter` implementation.
        pool_cap: Maximum ``len(lost) + len(valid found)`` before a scope is
            treated as degraded and routed to exact-hash fallback.

    Returns:
        A :class:`MatchResult` aggregating applied renames, per-node skipped
        records, unmatched lost ids, and per-reason degraded-scope counts.
    """
    result = MatchResult()
    scopes = adapter.discover_scopes()

    # --- Global exact-code_hash pre-pass -------------------------------------
    # An identical body (``code_hash`` equality, ratio 1.0) is a confident
    # rename regardless of scope: this is the pre-ADR-013 global behaviour, and
    # it is the only way to detect a *committed* cross-file move between builds,
    # which git's working-tree ``-M`` diff cannot bridge (``since..HEAD`` and
    # ``diff HEAD`` are both empty once the move is committed).  Scope reduction
    # governs *similarity* scoring (the expensive part); exact-hash matching
    # stays global and cheap.
    # Source candidates from the adapter's *full* found set when exposed: in git
    # mode a found node only lands in a scope if its file has a lost node or is a
    # git ``-M`` target, so a committed cross-file move (whose target file is
    # neither) would otherwise be invisible to this exact-hash pass.
    _all_candidates = getattr(adapter, "all_found_candidates", None)
    _candidate_iter = _all_candidates() if callable(_all_candidates) else [f for sc in scopes for f in sc.found]
    all_found_new: list[FoundNode] = []
    _seen_found: set[str] = set()
    for f in _candidate_iter:
        if is_valid_target(f) and f.code_hash and f.node_id not in _seen_found:
            _seen_found.add(f.node_id)
            all_found_new.append(f)
    by_hash: dict[str, FoundNode] = {}
    for f in sorted(all_found_new, key=lambda n: n.node_id):
        by_hash.setdefault(f.code_hash, f)

    used_found: set[str] = set()
    matched_lost: set[str] = set()
    for sc in scopes:
        for lost in sorted(sc.lost, key=lambda n: n.node_id):
            if lost.node_id in matched_lost or not lost.code_hash:
                continue
            f = by_hash.get(lost.code_hash)
            if f is not None and f.node_id not in used_found:
                adapter.apply(lost.node_id, f.node_id)
                result.applied.append(RenameApplied(lost.node_id, f.node_id, 1.0))
                used_found.add(f.node_id)
                matched_lost.add(lost.node_id)

    # --- Per-scope similarity for the remainder ------------------------------
    for scope in scopes:
        remaining_lost = [n for n in scope.lost if n.node_id not in matched_lost]
        if not remaining_lost:
            continue
        found_new = [f for f in scope.found if is_valid_target(f) and f.node_id not in used_found]

        reason = scope.reason_if_degraded
        if reason is None and (len(remaining_lost) + len(found_new)) > pool_cap:
            reason = "pool_cap"

        if reason is not None:
            result.degraded_scopes[reason] = result.degraded_scopes.get(reason, 0) + 1
            _exact_fallback(adapter, remaining_lost, found_new, reason, result)
            continue

        _similarity_scope(adapter, remaining_lost, found_new, result)

    return result


def _similarity_scope(
    adapter: RenameAdapter,
    lost_nodes: list[LostNode],
    found_new: list[FoundNode],
    result: MatchResult,
) -> None:
    """Similarity-score a healthy scope and greedily assign best matches."""
    scored: list[tuple[float, str, str, LostNode, FoundNode]] = []
    unscorable: list[LostNode] = []

    for lost in sorted(lost_nodes, key=lambda n: n.node_id):
        ob = adapter.old_body(lost)
        if ob is None:
            # Prior body unreachable (e.g. SHA force-pushed away) -> this lost
            # node cannot be similarity-scored; route to per-node exact-hash.
            unscorable.append(lost)
            continue
        for f in found_new:
            ratio = body_ratio(ob, f.body, lost.code_hash, f.code_hash)
            scored.append((ratio, lost.node_id, f.node_id, lost, f))

    # Greedy deterministic best-match: highest ratio wins; stable tie-break on
    # (lost_id, found_id) so builds are reproducible.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_lost: set[str] = set()
    used_found: set[str] = set()
    for ratio, lid, fid, lost, f in scored:
        if lid in used_lost or fid in used_found:
            continue
        if ratio >= adapter.threshold:
            adapter.apply(lost.node_id, f.node_id)
            result.applied.append(RenameApplied(lost.node_id, f.node_id, ratio))
            used_lost.add(lid)
            used_found.add(fid)

    for lost in lost_nodes:
        if lost in unscorable:
            continue
        if lost.node_id not in used_lost:
            # Scored but below threshold for every candidate -> genuine
            # deletion.  No suspect marker (it *was* scored).
            result.not_found.append(lost.node_id)

    if unscorable:
        _exact_fallback(adapter, unscorable, found_new, "no_git", result)


def _exact_fallback(
    adapter: RenameAdapter,
    lost_nodes: list[LostNode],
    found_new: list[FoundNode],
    reason: str,
    result: MatchResult,
) -> None:
    """Per-lost-node exact-``code_hash`` fallback for a degraded scope.

    Fallback drops *similarity*, not rename detection: a lost node with an
    exact-hash match is still applied as a confident rename.  A lost node with
    no exact match becomes the suspect ``NOT_FOUND`` and gets a
    :class:`ScoringSkipped` record (reason + candidate count).
    """
    by_hash: dict[str, FoundNode] = {}
    for f in sorted(found_new, key=lambda n: n.node_id):
        by_hash.setdefault(f.code_hash, f)

    used_found: set[str] = set()
    for lost in sorted(lost_nodes, key=lambda n: n.node_id):
        f = by_hash.get(lost.code_hash)
        if f is not None and f.node_id not in used_found:
            adapter.apply(lost.node_id, f.node_id)
            result.applied.append(RenameApplied(lost.node_id, f.node_id, 1.0))
            used_found.add(f.node_id)
        else:
            result.skipped.append(ScoringSkipped(lost.node_id, reason, len(found_new)))
            result.not_found.append(lost.node_id)


# ---------------------------------------------------------------------------
# Code adapter (the only adapter implemented in v1; D-4)
# ---------------------------------------------------------------------------


class CodeRenameAdapter:
    """The code-node implementation of the :class:`RenameAdapter` seam.

    Constructed by the builder with the lost/found node value objects already
    materialised.  Scope discovery groups candidates by file and bridges git
    file-rename pairs; old-body extraction goes through
    :func:`axiom_graph.index.git_utils.get_old_body`; apply delegates to
    ``db.record_code_rename`` (which migrates node_renames + history +
    verification + documents/validates/depends_on edges).
    """

    def __init__(
        self,
        db_path: Path,
        project_root: Path,
        lost: list[LostNode],
        found: list[FoundNode],
        since_sha: str | None,
        threshold: float,
        *,
        no_git: bool = False,
    ) -> None:
        self.db_path = db_path
        self.project_root = project_root
        self.threshold = threshold
        self._lost = lost
        self._found = found
        self._since_sha = since_sha
        self._no_git = no_git
        self._found_loc = {f.node_id: f.location for f in found}
        #: New IDs that received a migrated history/edges (RENAMED) this run.
        self.applied_new_ids: list[str] = []

    def all_found_candidates(self) -> list[FoundNode]:
        """Return every found node, for the core's global exact-hash pre-pass.

        Scope discovery may legitimately exclude a found node from all pools
        (its file has no lost node and no git ``-M`` pair); the exact-hash pass
        still needs to see it to catch committed cross-file moves.
        """
        return list(self._found)

    def discover_scopes(self) -> list[ScopePool]:
        from axiom_graph.index.git_utils import get_rename_pairs  # noqa: PLC0415

        # Without git there is no file-rename signal to bridge a cross-file
        # move, so file-scoping would silently lose every cross-file exact-hash
        # rename (the pre-ADR-013 global behaviour).  Degrade to a single global
        # scope: the matcher routes it to the per-node exact-hash fallback,
        # preserving cross-file exact-hash detection while still emitting the
        # per-node ``no_git`` suspect signal for unmatched lost nodes.
        if self._no_git:
            return [
                ScopePool(
                    lost=list(self._lost),
                    found=list(self._found),
                    reason_if_degraded="no_git",
                )
            ]

        lost_by_file: dict[str, list[LostNode]] = {}
        for n in self._lost:
            lost_by_file.setdefault(n.location, []).append(n)
        found_by_file: dict[str, list[FoundNode]] = {}
        for f in self._found:
            found_by_file.setdefault(f.location, []).append(f)

        pairs: dict[str, str] = {}
        if not self._no_git:
            pairs = get_rename_pairs(self.project_root, self._since_sha)

        scopes: list[ScopePool] = []
        for old_file, lost_list in lost_by_file.items():
            pool: list[FoundNode] = list(found_by_file.get(old_file, []))
            new_file = pairs.get(old_file)
            if new_file:
                pool.extend(found_by_file.get(new_file, []))
            scopes.append(
                ScopePool(
                    lost=lost_list,
                    found=pool,
                    reason_if_degraded="no_git" if self._no_git else None,
                )
            )
        return scopes

    def old_body(self, lost: LostNode) -> str | None:
        from axiom_graph.index.git_utils import get_old_body  # noqa: PLC0415

        if self._no_git or not lost.git_sha:
            return None
        return get_old_body(
            self.project_root,
            lost.git_sha,
            lost.location,
            lost.start_line,
            lost.end_line,
        )

    def apply(self, old_id: str, new_id: str) -> None:
        from axiom_graph.index import db  # noqa: PLC0415

        location = self._found_loc.get(new_id, "")
        db.record_code_rename(self.db_path, old_id, new_id, location, self.project_root)
        self.applied_new_ids.append(new_id)
