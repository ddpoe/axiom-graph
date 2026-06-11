"""Scoped-similarity rename detection (ADR-013 / pev-2026-06-05).

Covers the scoped-similarity matcher, its graceful degradation, and the
manual apply/revert escape hatch.

Tier 1 — plain pytest:
    Pure matcher internals (ratio scoring, guard predicate, greedy pairing,
    threshold boundary, pool-cap fallback) exercised through a fake adapter.

Tier 2 — @workflow(purpose=...):
    Subsystem scenarios: newly-appeared guard, greedy multi-candidate,
    pool-cap + per-node suspect signal, non-git suspect signal, manual apply
    escape hatch, discovery_only path.

Tier 3 — @workflow(purpose=...) + Step():
    User-story acceptance: pure rename, rename+edit, sub-threshold non-weld,
    cross-file ``-M`` rename, revert round-trip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.index import db
from axiom_graph.index.rename_matcher import (
    FoundNode,
    LostNode,
    ScopePool,
    body_ratio,
    is_valid_target,
    run_matcher,
)
from axiom_graph.lifecycle.api import apply_rename, build_index, mark_clean_nodes, revert_rename


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def _commit(root: Path, msg: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", msg)


def _own_status(db_path: Path, node_id: str) -> str | None:
    with db._connect(db_path) as conn:
        row = conn.execute("SELECT own_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return row["own_status"] if row else None


def _rename_map(db_path: Path) -> dict[str, str]:
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT old_id, new_id FROM node_renames").fetchall()
    return {r["old_id"]: r["new_id"] for r in rows}


def _history_change_types(db_path: Path, node_id: str) -> list[dict]:
    return db.get_history(db_path, node_id, limit=100)


class _FakeAdapter:
    """In-memory adapter for Tier-1 core tests (no git, no DB)."""

    def __init__(self, scopes, old_bodies, threshold=0.6):
        self._scopes = scopes
        self._old_bodies = old_bodies
        self.threshold = threshold
        self.applied: list[tuple[str, str]] = []

    def discover_scopes(self):
        return self._scopes

    def old_body(self, lost):
        return self._old_bodies.get(lost.node_id)

    def apply(self, old_id, new_id):
        self.applied.append((old_id, new_id))


def _lost(node_id, code_hash="h", location="mod.py"):
    return LostNode(node_id=node_id, code_hash=code_hash, location=location, start_line=1, end_line=3, git_sha="sha")


def _found(node_id, body, code_hash="h", location="mod.py", is_new=True):
    return FoundNode(node_id=node_id, code_hash=code_hash, location=location, body=body, is_new=is_new)


# ---------------------------------------------------------------------------
# Tier 1 — pure matcher internals
# ---------------------------------------------------------------------------


def test_body_ratio_exact_hash_is_fast_path():
    """Equal code_hash short-circuits to 1.0 regardless of body text."""
    assert body_ratio("totally", "different", "abc", "abc") == 1.0


def test_body_ratio_similarity_scoring():
    """Different hashes fall through to a SequenceMatcher ratio (similar=high, unrelated=low)."""
    high = body_ratio("def f():\n    return 1\n", "def f():\n    return 2\n", "h1", "h2")
    low = body_ratio("def f():\n    return 1\n", "class Z:\n    pass\n", "h1", "h3")
    assert high > 0.6
    assert low < 0.6


def test_is_valid_target_requires_newly_appeared():
    """A rename target must have no prior baseline (is_new)."""
    assert is_valid_target(_found("n", "body", is_new=True)) is True
    assert is_valid_target(_found("n", "body", is_new=False)) is False


def test_run_matcher_greedy_deterministic_pairing():
    """Crowded healthy scope: each lost welds to its best-ratio candidate."""
    lost_a = _lost("p::m::a", code_hash="ha")
    lost_b = _lost("p::m::b", code_hash="hb")
    found_a = _found("p::m::a2", "AAAA AAAA AAAA", code_hash="fa")
    found_b = _found("p::m::b2", "ZZZZ ZZZZ ZZZZ", code_hash="fb")
    scope = ScopePool(lost=[lost_a, lost_b], found=[found_a, found_b])
    adapter = _FakeAdapter(
        [scope],
        {"p::m::a": "AAAA AAAA AAAB", "p::m::b": "ZZZZ ZZZZ ZZZY"},
        threshold=0.6,
    )
    result = run_matcher(adapter, pool_cap=50)
    assert set(adapter.applied) == {("p::m::a", "p::m::a2"), ("p::m::b", "p::m::b2")}
    assert not result.skipped


def test_run_matcher_threshold_boundary():
    """A pair below threshold is not welded; the same pair above threshold is."""
    lost = _lost("p::m::x", code_hash="hx")
    found = _found("p::m::y", "the quick brown fox jumped", code_hash="fy")
    scope = ScopePool(lost=[lost], found=[found])
    old = {"p::m::x": "an entirely unrelated sentence here"}

    strict = _FakeAdapter([scope], old, threshold=0.95)
    run_matcher(strict, pool_cap=50)
    assert strict.applied == []

    loose = _FakeAdapter(
        [
            ScopePool(
                lost=[_lost("p::m::x", code_hash="hx")],
                found=[_found("p::m::y", "an entirely unrelated sentence here", code_hash="fy")],
            )
        ],
        {"p::m::x": "an entirely unrelated sentence here"},
        threshold=0.5,
    )
    run_matcher(loose, pool_cap=50)
    assert loose.applied == [("p::m::x", "p::m::y")]


def test_run_matcher_pool_cap_routes_to_exact_fallback():
    """A scope past pool_cap degrades to exact-hash; an unmatched lost node is recorded skipped."""
    lost = _lost("p::m::x", code_hash="nohit")
    found1 = _found("p::m::y", "body1", code_hash="f1")
    found2 = _found("p::m::z", "body2", code_hash="f2")
    scope = ScopePool(lost=[lost], found=[found1, found2])
    adapter = _FakeAdapter([scope], {"p::m::x": "irrelevant"}, threshold=0.6)
    result = run_matcher(adapter, pool_cap=1)
    assert adapter.applied == []
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "pool_cap"
    assert result.skipped[0].candidates == 2
    assert result.degraded_scopes.get("pool_cap") == 1


# ---------------------------------------------------------------------------
# Tier 2 — subsystem scenarios
# ---------------------------------------------------------------------------


@workflow(
    purpose="A candidate that already had a prior baseline (is_new=False) is never used "
    "as a rename target, even when its body matches the lost node exactly"
)
def test_newly_appeared_guard_refuses_baseline_target():
    lost = _lost("p::m::x", code_hash="same")
    target = _found("p::m::y", "identical body", code_hash="same", is_new=False)
    scope = ScopePool(lost=[lost], found=[target])
    adapter = _FakeAdapter([scope], {"p::m::x": "identical body"}, threshold=0.6)
    result = run_matcher(adapter, pool_cap=50)
    assert adapter.applied == []
    assert "p::m::x" in result.not_found


@workflow(
    purpose="A scope with multiple lost and multiple newly-appeared nodes resolves to a "
    "deterministic, reproducible best-ratio assignment"
)
def test_greedy_multi_candidate_is_deterministic():
    lost = [_lost("p::m::a", code_hash="ha"), _lost("p::m::b", code_hash="hb")]
    found = [
        _found("p::m::a2", "alpha alpha alpha", code_hash="fa"),
        _found("p::m::b2", "bravo bravo bravo", code_hash="fb"),
    ]
    old = {"p::m::a": "alpha alpha alphx", "p::m::b": "bravo bravo bravx"}
    runs = []
    for _ in range(3):
        adapter = _FakeAdapter([ScopePool(lost=list(lost), found=list(found))], old, threshold=0.6)
        run_matcher(adapter, pool_cap=50)
        runs.append(sorted(adapter.applied))
    assert runs[0] == runs[1] == runs[2]
    assert runs[0] == [("p::m::a", "p::m::a2"), ("p::m::b", "p::m::b2")]


@workflow(
    purpose="A pool past the cap falls back to exact-hash at build time; an edited lost node "
    "becomes NOT_FOUND and carries a per-node RENAME_SCORING_SKIPPED (reason=pool_cap) "
    "event, with the aggregate skipped-scope summary on the build output"
)
def test_pool_cap_fallback_records_per_node_suspect_signal(git_project, git_db_path):
    (git_project / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "pc"\n\n[axiom_graph.rename]\npool_cap = 1\n',
        encoding="utf-8",
    )
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef gamma():\n    total = 0\n    for i in range(10):\n        total += i\n    return total\n"
    )
    _commit(git_project, "c1")
    build_index(git_db_path, git_project, project_id="pc", discovery_only=False)

    # Rename+edit gamma AND add a sibling so found_new for the scope is > cap.
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef gamma_renamed():\n    total = 1\n    for i in range(10):\n        total += i\n    return total\n\n\ndef delta():\n    return 7\n"
    )
    _commit(git_project, "c2")
    summary = build_index(git_db_path, git_project, project_id="pc", discovery_only=True)

    assert _own_status(git_db_path, "pc::mod_a::gamma") == "NOT_FOUND"
    events = _history_change_types(git_db_path, "pc::mod_a::gamma")
    skipped = [e for e in events if e["change_type"] == "RENAME_SCORING_SKIPPED"]
    assert skipped, "gamma must carry a per-node RENAME_SCORING_SKIPPED event"
    import json as _json

    meta = _json.loads(skipped[0]["meta"])
    assert meta["reason"] == "pool_cap"
    assert meta["candidates"] == 2
    assert any("similarity skipped" in w and "pool_cap" in w for w in summary.warnings)


@workflow(
    purpose="In a non-git project an edited lost node falls back to exact-hash, becomes "
    "NOT_FOUND, and carries a per-node RENAME_SCORING_SKIPPED (reason=no_git) event; "
    "a byte-identical move still welds via exact-hash"
)
def test_non_git_suspect_signal_and_exact_hash_weld(mini_project, db_path):
    (mini_project / "mod_a.py").write_text("def beta():\n    return 100\n\n\ndef ident():\n    return 5\n")
    build_index(db_path, mini_project, project_id="ng", discovery_only=False)

    # mod_a.py stays on disk (so beta -> NOT_FOUND rather than purged); beta has
    # no exact match anywhere, ident moves byte-identical to mod_b (exact-hash weld).
    # extra_b keeps the mod_b module-level hash distinct from the ident function
    # hash, so the exact-hash weld targets the function node, not the module node.
    (mini_project / "mod_a.py").write_text("def placeholder():\n    return 0\n")
    (mini_project / "mod_b.py").write_text("def ident():\n    return 5\n\n\ndef extra_b():\n    return 7\n")
    build_index(db_path, mini_project, project_id="ng", discovery_only=True)

    # Byte-identical move welded via exact-hash even without git.
    assert _rename_map(db_path).get("ng::mod_a::ident") == "ng::mod_b::ident"
    # No exact match for beta and no git to similarity-score it -> suspect NOT_FOUND.
    assert _own_status(db_path, "ng::mod_a::beta") == "NOT_FOUND"
    import json as _json

    events = _history_change_types(db_path, "ng::mod_a::beta")
    skipped = [e for e in events if e["change_type"] == "RENAME_SCORING_SKIPPED"]
    assert skipped
    assert _json.loads(skipped[0]["meta"])["reason"] == "no_git"


@workflow(
    purpose="A real sub-threshold rename that fell to NOT_FOUND + fresh node can be manually "
    "welded via apply_rename; the safety contract refuses a pair where old is still live"
)
def test_apply_rename_escape_hatch_and_contract(git_project, git_db_path):
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef original():\n    return sum([1, 2, 3])\n"
    )
    _commit(git_project, "c1")
    build_index(git_db_path, git_project, project_id="eh", discovery_only=False)

    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef renamed():\n"
        "    while True:\n        print('completely different body')\n        break\n"
    )
    _commit(git_project, "c2")
    build_index(git_db_path, git_project, project_id="eh", discovery_only=True)

    old_id = "eh::mod_a::original"
    new_id = "eh::mod_a::renamed"
    assert _own_status(git_db_path, old_id) == "NOT_FOUND"
    assert old_id not in _rename_map(git_db_path)

    # Contract refusal: keeper is still live, not NOT_FOUND.
    refused = apply_rename(git_db_path, git_project, "eh::mod_a::keeper", new_id)
    assert refused.applied is False
    assert old_id not in _rename_map(git_db_path)

    # Valid weld.
    applied = apply_rename(git_db_path, git_project, old_id, new_id)
    assert applied.applied is True
    assert _rename_map(git_db_path).get(old_id) == new_id
    assert _own_status(git_db_path, new_id) == "RENAMED"
    assert db.get_history(git_db_path, new_id), "history migrated to the new id"


@workflow(
    purpose="The matcher runs on the default discovery_only=True build path (not gated to a "
    "full re-index): a rename+edit is detected and applied"
)
def test_discovery_only_build_runs_matcher(git_project, git_db_path):
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef compute_total(n):\n    acc = 0\n    for i in range(n):\n        acc += i\n    return acc\n"
    )
    _commit(git_project, "c1")
    build_index(git_db_path, git_project, project_id="do", discovery_only=False)

    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef compute_sum(n):\n    acc = 0\n    for i in range(n):\n        acc += i\n    return acc + 0\n"
    )
    _commit(git_project, "c2")
    summary = build_index(git_db_path, git_project, project_id="do", discovery_only=True)

    assert summary.nodes_renamed >= 1
    assert _rename_map(git_db_path).get("do::mod_a::compute_total") == "do::mod_a::compute_sum"


# ---------------------------------------------------------------------------
# Tier 3 — user-story acceptance
# ---------------------------------------------------------------------------


@workflow(
    purpose="US-1 pure rename: a function renamed with a byte-identical body across a commit "
    "migrates history + inbound edges via the exact-hash fast path; the old id is not "
    "treated as a plain deletion"
)
def test_us1_pure_rename_migrates_via_exact_hash(git_project, git_db_path):
    口 = Step(step_num=1, name="Baseline", purpose="Index a function and commit it")
    (git_project / "mod_a.py").write_text("def keeper():\n    return 0\n\n\ndef widget(x):\n    return x * 2\n")
    _commit(git_project, "c1")
    build_index(git_db_path, git_project, project_id="u1", discovery_only=False)

    口 = Step(step_num=2, name="Pure rename", purpose="Rename widget->gadget, body byte-identical")
    (git_project / "mod_a.py").write_text("def keeper():\n    return 0\n\n\ndef gadget(x):\n    return x * 2\n")
    _commit(git_project, "c2")
    build_index(git_db_path, git_project, project_id="u1", discovery_only=True)

    口 = Step(step_num=3, name="Assert weld", purpose="History migrated to the new id, rename recorded")
    assert _rename_map(git_db_path).get("u1::mod_a::widget") == "u1::mod_a::gadget"
    assert db.get_history(git_db_path, "u1::mod_a::gadget")
    assert db.get_node(git_db_path, "u1::mod_a::gadget") is not None


@workflow(
    purpose="US-2 rename+edit: a function renamed AND edited above threshold is detected via "
    "body similarity and the new node is marked own_status=RENAMED (not CONTENT_UPDATED, "
    "not a fresh pair)"
)
def test_us2_rename_plus_edit_marks_renamed(git_project, git_db_path):
    口 = Step(step_num=1, name="Baseline", purpose="Index compute_total and commit")
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef compute_total(n):\n    acc = 0\n    for i in range(n):\n        acc += i\n    return acc\n"
    )
    _commit(git_project, "c1")
    build_index(git_db_path, git_project, project_id="u2", discovery_only=False)

    口 = Step(step_num=2, name="Rename + edit", purpose="compute_total->running_sum with a small edit")
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef running_sum(n):\n    acc = 0\n    for i in range(n):\n        acc += i\n    return acc  # sum\n"
    )
    _commit(git_project, "c2")
    build_index(git_db_path, git_project, project_id="u2", discovery_only=True)

    口 = Step(step_num=3, name="Assert RENAMED", purpose="IDs migrate, new node own_status RENAMED")
    assert _rename_map(git_db_path).get("u2::mod_a::compute_total") == "u2::mod_a::running_sum"
    assert _own_status(git_db_path, "u2::mod_a::running_sum") == "RENAMED"


@workflow(
    purpose="US-3 sub-threshold: a function whose body is rewritten below threshold stays a "
    "deletion (NOT_FOUND), the new node is fresh, and no history/edge migration happens"
)
def test_us3_sub_threshold_no_false_weld(git_project, git_db_path):
    口 = Step(step_num=1, name="Baseline", purpose="Index original and commit")
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef original():\n    return sum([1, 2, 3, 4, 5])\n"
    )
    _commit(git_project, "c1")
    build_index(git_db_path, git_project, project_id="u3", discovery_only=False)

    口 = Step(step_num=2, name="Heavy rewrite", purpose="Replace with an unrelated body below threshold")
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef unrelated():\n"
        "    raise NotImplementedError('a wholly different implementation entirely')\n"
    )
    _commit(git_project, "c2")
    build_index(git_db_path, git_project, project_id="u3", discovery_only=True)

    口 = Step(step_num=3, name="Assert no weld", purpose="Old NOT_FOUND, new fresh, no rename row")
    assert _own_status(git_db_path, "u3::mod_a::original") == "NOT_FOUND"
    assert "u3::mod_a::original" not in _rename_map(git_db_path)
    assert _own_status(git_db_path, "u3::mod_a::unrelated") == "VERIFIED"


@workflow(
    purpose="US-4 + US-1/2 cross-file: a function moved into a git -M renamed file with a small "
    "edit migrates via git scope-reduction and surfaces as RENAMED"
)
def test_us4_cross_file_rename_via_git_minus_m(git_project, git_db_path):
    口 = Step(step_num=1, name="Baseline", purpose="Index a function in mod_old.py and commit")
    (git_project / "mod_old.py").write_text(
        "def keeper():\n    return 0\n\n\ndef transform(data):\n    out = []\n    for d in data:\n        out.append(d + 1)\n    return out\n"
    )
    _commit(git_project, "c1")
    build_index(git_db_path, git_project, project_id="u4", discovery_only=False)

    口 = Step(
        step_num=2,
        name="git mv + edit",
        purpose="Rename the file and edit the function body "
        "(left uncommitted so the working-tree -M diff bridges the scope)",
    )
    _git(git_project, "mv", "mod_old.py", "mod_new.py")
    (git_project / "mod_new.py").write_text(
        "def keeper():\n    return 0\n\n\ndef transform(data):\n    out = []\n    for d in data:\n        out.append(d + 1)\n    return out  # moved\n"
    )
    build_index(git_db_path, git_project, project_id="u4", discovery_only=True)

    口 = Step(step_num=3, name="Assert cross-file weld", purpose="IDs migrate across files, RENAMED surfaces")
    assert _rename_map(git_db_path).get("u4::mod_old::transform") == "u4::mod_new::transform"
    assert _own_status(git_db_path, "u4::mod_new::transform") == "RENAMED"


@workflow(
    purpose="US-6 revert round-trip: after a rename is auto-applied, revert_rename restores the "
    "old id as the live identity and detaches the new id as fresh, fully un-welding"
)
def test_us6_revert_round_trip(git_project, git_db_path):
    口 = Step(step_num=1, name="Baseline", purpose="Index handler and commit")
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef handle_request(req):\n    result = req.upper()\n    return result\n"
    )
    _commit(git_project, "c1")
    build_index(git_db_path, git_project, project_id="u6", discovery_only=False)

    口 = Step(step_num=2, name="Auto-apply rename+edit", purpose="handle_request->process_request, marked RENAMED")
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef process_request(req):\n    result = req.upper()\n    return result  # processed\n"
    )
    _commit(git_project, "c2")
    build_index(git_db_path, git_project, project_id="u6", discovery_only=True)

    old_id = "u6::mod_a::handle_request"
    new_id = "u6::mod_a::process_request"
    assert _rename_map(git_db_path).get(old_id) == new_id
    assert _own_status(git_db_path, new_id) == "RENAMED"

    口 = Step(step_num=3, name="Revert", purpose="revert_rename(new) migrates back, un-welds the pair")
    result = revert_rename(git_db_path, git_project, new_id)
    assert result.reverted is True
    assert result.old_id == old_id

    口 = Step(step_num=4, name="Assert restored identity", purpose="Old live, new detached, no rename rows")
    assert _own_status(git_db_path, old_id) == "VERIFIED"
    assert _own_status(git_db_path, new_id) == "VERIFIED"
    assert old_id not in _rename_map(git_db_path)
    assert new_id not in _rename_map(git_db_path)
    assert db.get_history(git_db_path, old_id), "history migrated back to the old id"


@workflow(
    purpose="US-4 mark_clean clears RENAMED: after a rename+edit auto-applies and the new node "
    "is marked own_status=RENAMED, mark_clean on that node resets the baseline to VERIFIED "
    "and the durable clear survives a rebuild (the sticky RENAMED overlay yields once the "
    "persisted status is no longer RENAMED)"
)
def test_us4_mark_clean_clears_renamed(git_project, git_db_path):
    口 = Step(step_num=1, name="Baseline", purpose="Index compute_total and commit")
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef compute_total(n):\n    acc = 0\n    for i in range(n):\n        acc += i\n    return acc\n"
    )
    _commit(git_project, "c1")
    build_index(git_db_path, git_project, project_id="u4mc", discovery_only=False)

    口 = Step(step_num=2, name="Rename + edit", purpose="compute_total->running_sum with a small edit, auto-applies")
    (git_project / "mod_a.py").write_text(
        "def keeper():\n    return 0\n\n\ndef running_sum(n):\n    acc = 0\n    for i in range(n):\n        acc += i\n    return acc  # sum\n"
    )
    _commit(git_project, "c2")
    build_index(git_db_path, git_project, project_id="u4mc", discovery_only=True)

    new_id = "u4mc::mod_a::running_sum"
    口 = Step(step_num=3, name="Assert RENAMED", purpose="Auto-applied rename marks the new node RENAMED")
    assert _rename_map(git_db_path).get("u4mc::mod_a::compute_total") == new_id
    assert _own_status(git_db_path, new_id) == "RENAMED"

    口 = Step(step_num=4, name="mark_clean", purpose="Verify the new node, resetting its baseline to VERIFIED")
    result = mark_clean_nodes(git_db_path, git_project, [new_id], reason="reviewed rename", verified_by="human")
    assert result.marked == [new_id]
    assert _own_status(git_db_path, new_id) == "VERIFIED"

    口 = Step(
        step_num=5, name="Assert durable clear", purpose="Rebuild — the sticky overlay yields, RENAMED stays cleared"
    )
    build_index(git_db_path, git_project, project_id="u4mc", discovery_only=True)
    assert _own_status(git_db_path, new_id) == "VERIFIED"
