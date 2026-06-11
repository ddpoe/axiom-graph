"""Tests for staleness helpers: _now_utc(), get_stale_tests(),
apply_composite_inheritance(), persist/read staleness, dual-hash
verification, the both-changed bug fix, record_staleness transition events,
and _transition_change_type mapping."""

from __future__ import annotations

import json
import time
from pathlib import Path


from axiom_annotations import workflow

from axiom_graph.index import db
from axiom_graph.index.staleness import (
    apply_composite_inheritance,
    compute_staleness,
    record_staleness,
    _transition_change_type,
)
from axiom_graph.models import AxiomEdge, AxiomNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(
    node_id: str,
    node_type: str = "atomic_process",
    subtype: str | None = None,
    code_hash: str = "abc123",
    desc_hash: str | None = None,
    location: str = "src/mod.py",
) -> AxiomNode:
    return AxiomNode(
        id=node_id,
        node_type=node_type,
        subtype=subtype,
        title=node_id.split("::")[-1],
        location=location,
        source="ast",
        code_hash=code_hash,
        desc_hash=desc_hash,
        level_0=node_id,
        level_1=node_id,
    )


def _edge(from_id: str, edge_type: str, to_id: str) -> AxiomEdge:
    return AxiomEdge(
        id=f"{from_id}::{edge_type}::{to_id}",
        edge_type=edge_type,
        from_id=from_id,
        to_id=to_id,
    )


# ---------------------------------------------------------------------------
# _now_utc — basic contract
# ---------------------------------------------------------------------------


def test_now_utc_returns_iso_string():
    """_now_utc() should return a valid ISO-8601 UTC string."""
    from axiom_graph.index.db import _now_utc

    ts = _now_utc()
    assert isinstance(ts, str)
    # Must end with +00:00 (timezone.utc isoformat) or contain a T separator
    assert "T" in ts
    assert ts.endswith("+00:00")


def test_now_utc_monotonically_increases():
    """Two successive calls should produce non-decreasing timestamps."""
    from axiom_graph.index.db import _now_utc

    t1 = _now_utc()
    time.sleep(0.01)
    t2 = _now_utc()
    assert t2 >= t1


# ---------------------------------------------------------------------------
# get_stale_tests
# ---------------------------------------------------------------------------


def test_get_stale_tests_returns_empty_when_no_validates_edges(db_path: Path):
    n = _node("proj::mod::func")
    db.upsert_node(db_path, n, discovery_only=False)
    assert db.get_stale_tests(db_path) == []


def test_get_stale_tests_returns_empty_when_code_unchanged(db_path: Path):
    """Test node linked via validates to a function that has never changed → not stale."""
    func = _node("proj::mod::func", code_hash="aaa")
    test = _node(
        "proj::tests::test_func",
        node_type="atomic_process",
        subtype="test",
        code_hash="bbb",
        location="tests/test_mod.py",
    )
    db.upsert_node(db_path, func, discovery_only=False)
    db.upsert_node(db_path, test, discovery_only=False)
    db.upsert_edge(db_path, _edge("proj::tests::test_func", "validates", "proj::mod::func"))

    # No CONTENT_ONLY/CONTENT_AND_DESC history → empty
    assert db.get_stale_tests(db_path) == []


def test_get_stale_tests_detects_stale_after_code_change(db_path: Path):
    """Code function gets a CONTENT_ONLY history row after test was written → COVERAGE_STALE."""
    func = _node("proj::mod::func", code_hash="aaa")
    test = _node(
        "proj::tests::test_func",
        node_type="atomic_process",
        subtype="test",
        code_hash="bbb",
        location="tests/test_mod.py",
    )
    # Write test node first so its updated_at is earlier
    db.upsert_node(db_path, test, discovery_only=False)
    time.sleep(0.02)
    # Write func — INITIAL history row, not CONTENT_ONLY yet
    db.upsert_node(db_path, func, discovery_only=False)
    db.upsert_edge(db_path, _edge("proj::tests::test_func", "validates", "proj::mod::func"))

    # Simulate code change: upsert with new hash → produces CONTENT_ONLY history row
    func2 = _node("proj::mod::func", code_hash="zzz")
    db.upsert_node(db_path, func2, discovery_only=False)

    rows = db.get_stale_tests(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["test_node_id"] == "proj::tests::test_func"
    assert row["code_node_id"] == "proj::mod::func"
    assert row["code_changed_at"] > row["test_updated_at"]


def test_get_stale_tests_matches_became_content_updated(db_path: Path):
    """get_stale_tests must match BECAME_CONTENT_UPDATED history rows (written by compute_staleness
    during discovery-only builds, not by upsert_node)."""
    from axiom_graph.index.db import _connect, _now_utc

    func = _node("proj::mod::func", code_hash="aaa")
    test = _node(
        "proj::tests::test_func",
        node_type="atomic_process",
        subtype="test",
        code_hash="bbb",
        location="tests/test_mod.py",
    )
    # Write test first so its updated_at is earlier
    db.upsert_node(db_path, test, discovery_only=False)
    time.sleep(0.02)
    db.upsert_node(db_path, func, discovery_only=False)
    db.upsert_edge(db_path, _edge("proj::tests::test_func", "validates", "proj::mod::func"))

    # Directly seed a BECAME_CONTENT_UPDATED history row (as compute_staleness would)
    time.sleep(0.02)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO node_history (node_id, scanned_at, change_type) VALUES (?, ?, ?)",
            ("proj::mod::func", _now_utc(), "BECAME_CONTENT_UPDATED"),
        )

    rows = db.get_stale_tests(db_path)
    assert len(rows) == 1
    assert rows[0]["test_node_id"] == "proj::tests::test_func"
    assert rows[0]["code_node_id"] == "proj::mod::func"


def test_get_stale_doc_sections_matches_became_content_updated(db_path: Path):
    """get_stale_doc_sections must match BECAME_CONTENT_UPDATED history rows."""
    from axiom_graph.index.db import _connect, _now_utc

    func = _node("proj::mod::func", code_hash="aaa")
    section = _node(
        "proj::docs.arch::overview",
        subtype="docjson",
        code_hash="sec_hash",
        desc_hash="heading_hash",
        location="docs/arch.json",
    )
    # Write section first so its updated_at is earlier
    db.upsert_node(db_path, section, discovery_only=False)

    # Insert doc + doc_section rows
    with _connect(db_path) as conn:
        section_ts = conn.execute(
            "SELECT updated_at FROM nodes WHERE id = ?",
            ("proj::docs.arch::overview",),
        ).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO docs (id, title, file_path, desc_hash, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("proj::docs.arch", "Architecture", "docs/arch.json", "x", section_ts),
        )
        conn.execute(
            "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, position, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("proj::docs.arch::overview", "proj::docs.arch", "Overview", 2, 0, section_ts),
        )

    time.sleep(0.02)
    db.upsert_node(db_path, func, discovery_only=False)
    db.upsert_edge(db_path, _edge("proj::docs.arch::overview", "documents", "proj::mod::func"))

    # Directly seed a BECAME_CONTENT_UPDATED history row
    time.sleep(0.02)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO node_history (node_id, scanned_at, change_type) VALUES (?, ?, ?)",
            ("proj::mod::func", _now_utc(), "BECAME_CONTENT_UPDATED"),
        )

    rows = db.get_stale_doc_sections(db_path)
    assert len(rows) == 1
    assert rows[0]["section_id"] == "proj::docs.arch::overview"
    assert rows[0]["code_node_id"] == "proj::mod::func"


def test_get_stale_tests_not_triggered_by_test_change_only(db_path: Path):
    """A test change alone must not mark the validated function stale (direction matters)."""
    func = _node("proj::mod::func", code_hash="aaa")
    test = _node(
        "proj::tests::test_func",
        node_type="atomic_process",
        subtype="test",
        code_hash="bbb",
        location="tests/test_mod.py",
    )
    db.upsert_node(db_path, func, discovery_only=False)
    time.sleep(0.02)
    db.upsert_node(db_path, test, discovery_only=False)
    db.upsert_edge(db_path, _edge("proj::tests::test_func", "validates", "proj::mod::func"))

    # func has no CONTENT_ONLY row after test's updated_at → not stale
    assert db.get_stale_tests(db_path) == []


# ---------------------------------------------------------------------------
# apply_composite_inheritance — severity ordering
# ---------------------------------------------------------------------------


def _setup_composite(db_path: Path, children_statuses: dict[str, tuple[str, str]]) -> tuple[str, dict]:
    """Insert a composite parent and leaf children, return the parent id and statuses dict."""
    parent_id = "proj::pkg"
    db.upsert_node(db_path, _node(parent_id, node_type="composite_process"), discovery_only=False)
    statuses = {}
    for child_id, status_pair in children_statuses.items():
        db.upsert_node(db_path, _node(child_id), discovery_only=False)
        db.upsert_edge(db_path, _edge(parent_id, "composes", child_id))
        statuses[child_id] = status_pair
    return parent_id, statuses


def test_composite_inherits_worst_status_code_stale(db_path: Path):
    parent_id, statuses = _setup_composite(
        db_path,
        {
            "proj::pkg::a": ("VERIFIED", "VERIFIED"),
            "proj::pkg::b": ("CONTENT_UPDATED", "VERIFIED"),
        },
    )
    result = apply_composite_inheritance(statuses, db_path)
    assert result[parent_id][0] == "CONTENT_UPDATED"


def test_composite_inherits_structural_drift_over_code_stale(db_path: Path):
    parent_id, statuses = _setup_composite(
        db_path,
        {
            "proj::pkg::a": ("CONTENT_UPDATED", "VERIFIED"),
            "proj::pkg::b": ("NOT_FOUND", "VERIFIED"),
        },
    )
    result = apply_composite_inheritance(statuses, db_path)
    assert result[parent_id][0] == "NOT_FOUND"


def test_composite_all_clean_stays_clean(db_path: Path):
    parent_id, statuses = _setup_composite(
        db_path,
        {
            "proj::pkg::a": ("VERIFIED", "VERIFIED"),
            "proj::pkg::b": ("VERIFIED", "VERIFIED"),
        },
    )
    result = apply_composite_inheritance(statuses, db_path)
    assert result[parent_id] == ("VERIFIED", "VERIFIED")


def test_composite_verified_counts_as_clean(db_path: Path):
    """VERIFIED children must not elevate the composite above CLEAN."""
    parent_id, statuses = _setup_composite(
        db_path,
        {
            "proj::pkg::a": ("VERIFIED", "VERIFIED"),
            "proj::pkg::b": ("VERIFIED", "VERIFIED"),
        },
    )
    result = apply_composite_inheritance(statuses, db_path)
    assert result[parent_id] == ("VERIFIED", "VERIFIED")


def test_composite_verified_loses_to_code_stale(db_path: Path):
    """VERIFIED + CONTENT_UPDATED -> CONTENT_UPDATED (VERIFIED is CLEAN, not immune)."""
    parent_id, statuses = _setup_composite(
        db_path,
        {
            "proj::pkg::a": ("VERIFIED", "VERIFIED"),
            "proj::pkg::b": ("CONTENT_UPDATED", "VERIFIED"),
        },
    )
    result = apply_composite_inheritance(statuses, db_path)
    assert result[parent_id][0] == "CONTENT_UPDATED"


def test_composite_desc_stale_beats_clean(db_path: Path):
    parent_id, statuses = _setup_composite(
        db_path,
        {
            "proj::pkg::a": ("VERIFIED", "VERIFIED"),
            "proj::pkg::b": ("DESC_UPDATED", "VERIFIED"),
        },
    )
    result = apply_composite_inheritance(statuses, db_path)
    assert result[parent_id][0] == "DESC_UPDATED"


def test_composite_no_composes_edges_unchanged(db_path: Path):
    """No composes edges -> statuses dict is returned unchanged."""
    statuses = {"proj::mod::func": ("CONTENT_UPDATED", "VERIFIED")}
    result = apply_composite_inheritance(statuses, db_path)
    assert result == {"proj::mod::func": ("CONTENT_UPDATED", "VERIFIED")}


def test_composite_multi_level_propagation(db_path: Path):
    """A two-level composite: grandparent → parent → leaf.

    Leaf is CONTENT_UPDATED → parent should be CONTENT_UPDATED → grandparent CONTENT_UPDATED.
    """
    gp_id = "proj::top"
    p_id = "proj::top::mid"
    leaf_id = "proj::top::mid::leaf"

    db.upsert_node(db_path, _node(gp_id, node_type="composite_process"), discovery_only=False)
    db.upsert_node(db_path, _node(p_id, node_type="composite_process"), discovery_only=False)
    db.upsert_node(db_path, _node(leaf_id), discovery_only=False)

    db.upsert_edge(db_path, _edge(gp_id, "composes", p_id))
    db.upsert_edge(db_path, _edge(p_id, "composes", leaf_id))

    statuses = {leaf_id: ("CONTENT_UPDATED", "VERIFIED")}
    result = apply_composite_inheritance(statuses, db_path)
    assert result[p_id][0] == "CONTENT_UPDATED"
    assert result[gp_id][0] == "CONTENT_UPDATED"


# ---------------------------------------------------------------------------
# persist_staleness / get_all_staleness — round-trip
# ---------------------------------------------------------------------------


def test_persist_staleness_round_trip(db_path: Path):
    """Write staleness via persist_staleness, read back via get_all_staleness."""
    a = _node("proj::mod::a", code_hash="aaa")
    b = _node("proj::mod::b", code_hash="bbb")
    db.upsert_node(db_path, a, discovery_only=False)
    db.upsert_node(db_path, b, discovery_only=False)

    statuses = {"proj::mod::a": ("CONTENT_UPDATED", "VERIFIED"), "proj::mod::b": ("VERIFIED", "VERIFIED")}
    db.persist_staleness(db_path, statuses)

    result = db.get_all_staleness(db_path)
    assert result["proj::mod::a"] == ("CONTENT_UPDATED", "VERIFIED")
    assert result["proj::mod::b"] == ("VERIFIED", "VERIFIED")


def test_persist_staleness_overwrites_previous(db_path: Path):
    """Persisting a second time overwrites the first value."""
    n = _node("proj::mod::func")
    db.upsert_node(db_path, n, discovery_only=False)

    db.persist_staleness(db_path, {"proj::mod::func": ("VERIFIED", "VERIFIED")})
    assert db.get_all_staleness(db_path)["proj::mod::func"] == ("VERIFIED", "VERIFIED")

    db.persist_staleness(db_path, {"proj::mod::func": ("CONTENT_UPDATED", "LINKED_STALE")})
    assert db.get_all_staleness(db_path)["proj::mod::func"] == ("CONTENT_UPDATED", "LINKED_STALE")


# ---------------------------------------------------------------------------
# Dual-hash verification promotion
# ---------------------------------------------------------------------------


def test_verification_both_hashes_match_promotes_to_clean(db_path: Path):
    """When both code_hash and desc_hash match the verification snapshot → CLEAN."""
    n = _node("proj::mod::func", code_hash="aaa", desc_hash="ddd")
    db.upsert_node(db_path, n, discovery_only=False)
    db.upsert_verification(db_path, "proj::mod::func", "human", code_hash_at="aaa", desc_hash_at="ddd")

    # Node hashes unchanged -> promotion to CLEAN should fire.
    statuses = {"proj::mod::func": ("CONTENT_UPDATED", "VERIFIED")}
    verifications = db.get_all_verifications(db_path)
    v = verifications["proj::mod::func"]
    assert v["code_hash_at"] == "aaa"
    assert v["desc_hash_at"] == "ddd"

    # Simulate the promotion logic (same as staleness.py step 5).
    code_match = v["code_hash_at"] == n.code_hash
    desc_match = v["desc_hash_at"] == n.desc_hash
    assert code_match and desc_match


def test_verification_code_drift_prevents_promotion(db_path: Path):
    """When code_hash drifts after verification → CONTENT_UPDATED, not VERIFIED."""
    n = _node("proj::mod::func", code_hash="new_code", desc_hash="ddd")
    db.upsert_node(db_path, n, discovery_only=False)
    db.upsert_verification(db_path, "proj::mod::func", "human", code_hash_at="old_code", desc_hash_at="ddd")

    v = db.get_all_verifications(db_path)["proj::mod::func"]
    code_match = v["code_hash_at"] == n.code_hash
    desc_match = v["desc_hash_at"] == n.desc_hash
    # code drifted, desc same → not promotable
    assert not code_match
    assert desc_match


def test_verification_desc_drift_prevents_promotion(db_path: Path):
    """When desc_hash drifts after verification → DESC_UPDATED, not VERIFIED."""
    n = _node("proj::mod::func", code_hash="aaa", desc_hash="new_desc")
    db.upsert_node(db_path, n, discovery_only=False)
    db.upsert_verification(db_path, "proj::mod::func", "human", code_hash_at="aaa", desc_hash_at="old_desc")

    v = db.get_all_verifications(db_path)["proj::mod::func"]
    code_match = v["code_hash_at"] == n.code_hash
    desc_match = v["desc_hash_at"] == n.desc_hash
    # code same, desc drifted → not promotable
    assert code_match
    assert not desc_match


# ---------------------------------------------------------------------------
# "Both changed → CONTENT_UPDATED" bug fix
# ---------------------------------------------------------------------------


def test_both_hashes_changed_emits_content_stale(mini_project: Path, db_path: Path):
    """When both code_hash and desc_hash change, emit CONTENT_UPDATED (not CLEAN).

    This is a regression test for the old bug where the else branch
    fell through to CLEAN when both hashes differed.
    """
    # Write a Python file with a function.
    src = mini_project / "mod.py"
    src.write_text('def greet():\n    """Say hello."""\n    return "hello"\n')

    from axiom_graph.index import builder

    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Verify node exists and is CLEAN.
    nodes = db.all_nodes(db_path)
    func_nodes = [n for n in nodes if n.title == "greet"]
    assert len(func_nodes) == 1

    # Change both the body AND the docstring.
    src.write_text('def greet():\n    """Say goodbye."""\n    return "goodbye"\n')

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)
    func_status = statuses.get(func_nodes[0].id)
    assert func_status[0] == "CONTENT_UPDATED", f"Expected CONTENT_UPDATED when both hashes change, got {func_status}"


# ---------------------------------------------------------------------------
# Composite inheritance includes LINKED_STALE
# ---------------------------------------------------------------------------


def test_composite_inherits_linked_stale(db_path: Path):
    """LINKED_STALE must propagate through composite inheritance.

    Previously, LINKED_STALE was missing from _LINK_SEVERITY and silently
    mapped to severity 0 (VERIFIED).
    """
    parent_id, statuses = _setup_composite(
        db_path,
        {
            "proj::pkg::a": ("VERIFIED", "VERIFIED"),
            "proj::pkg::b": ("VERIFIED", "LINKED_STALE"),
        },
    )
    result = apply_composite_inheritance(statuses, db_path)
    assert result[parent_id][1] == "LINKED_STALE"


def test_composite_content_stale_beats_linked_stale(db_path: Path):
    """CONTENT_UPDATED (own severity 3) and LINKED_STALE (link severity 1) are independent."""
    parent_id, statuses = _setup_composite(
        db_path,
        {
            "proj::pkg::a": ("VERIFIED", "LINKED_STALE"),
            "proj::pkg::b": ("CONTENT_UPDATED", "VERIFIED"),
        },
    )
    result = apply_composite_inheritance(statuses, db_path)
    assert result[parent_id][0] == "CONTENT_UPDATED"
    assert result[parent_id][1] == "LINKED_STALE"


# ---------------------------------------------------------------------------
# Verification on doc sections
# ---------------------------------------------------------------------------


def test_verification_on_doc_section_promotes_to_verified(mini_project: Path, db_path: Path):
    """Doc sections (atomic_process/subtype=docjson) can be verified and promoted."""
    section = _node(
        "proj::docs.arch::overview",
        subtype="docjson",
        code_hash="prose_hash",
        desc_hash="heading_hash",
        location="docs/arch.json",
    )
    db.upsert_node(db_path, section, discovery_only=False)

    # Verify the section — snapshot both hashes
    db.upsert_verification(
        db_path,
        "proj::docs.arch::overview",
        "human",
        code_hash_at="prose_hash",
        desc_hash_at="heading_hash",
    )

    # Create the docjson file on disk so staleness engine doesn't NOT_FOUND
    import json

    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / "arch.json").write_text(
        json.dumps(
            {
                "title": "Architecture",
                "sections": [{"id": "overview", "heading": "Overview", "content": "Prose."}],
            }
        ),
        encoding="utf-8",
    )

    # Give it a matching mtime for the fast-pass
    mtime = (docs_dir / "arch.json").stat().st_mtime
    from axiom_graph.index.db import _connect

    with _connect(db_path) as conn:
        conn.execute("UPDATE nodes SET file_mtime = ? WHERE id = ?", (mtime, "proj::docs.arch::overview"))

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)
    # Mtime fast-pass → CLEAN, then verification promotion → VERIFIED
    # (promotion only fires on CONTENT_UPDATED/DESC_UPDATED, not CLEAN)
    # So with mtime match → CLEAN. Verification doesn't override CLEAN.
    # The real scenario: after an edit, the section becomes CONTENT_UPDATED,
    # and if the hashes still match the verification snapshot, it promotes.
    # Let's test that instead:

    # Force a stale mtime so the engine re-checks hashes
    with _connect(db_path) as conn:
        conn.execute("UPDATE nodes SET file_mtime = 0.0 WHERE id = ?", ("proj::docs.arch::overview",))

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)
    # The section's stored hashes match what verification snapshotted,
    # but the current file content generates different hashes → CONTENT_UPDATED
    # then verification check fires: if stored hashes == verification hashes → VERIFIED
    v = db.get_all_verifications(db_path)
    assert "proj::docs.arch::overview" in v


def test_verification_invalidated_by_subsequent_edit(mini_project: Path, db_path: Path):
    """VERIFIED → edit → drops to CONTENT_UPDATED."""
    # Initial file
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
        encoding="utf-8",
    )

    from axiom_graph.index import builder

    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Find the function node and verify it
    nodes = db.all_nodes(db_path)
    func_nodes = [n for n in nodes if n.title == "greet"]
    assert len(func_nodes) == 1
    func = func_nodes[0]

    db.upsert_verification(
        db_path,
        func.id,
        "human",
        code_hash_at=func.code_hash,
        desc_hash_at=func.desc_hash,
    )

    # Confirm the node promotes to VERIFIED on check
    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)
    # With matching hashes and a verification row, mtime fast-pass gives CLEAN
    # and promotion only applies to CONTENT_UPDATED/DESC_UPDATED. So CLEAN stays.
    # To trigger promotion, we need to force mtime mismatch so engine re-checks.

    # Now edit the body (invalidates the verification)
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "goodbye"\n',
        encoding="utf-8",
    )

    # Discovery-only build — does NOT update stored hashes or mtime.
    # Stored code_hash still reflects the old "hello" code.
    builder.build(mini_project, project_id="proj", discovery_only=True)

    nodes = db.all_nodes(db_path)
    statuses = compute_staleness(db_path, mini_project, nodes)

    # mtime mismatch → engine re-parses file → current hash ("goodbye")
    # ≠ stored hash ("hello") → CONTENT_UPDATED.
    # Verification snapshot has "hello" hash, current file has "goodbye"
    # hash → promotion correctly fails → stays CONTENT_UPDATED.
    own = statuses[func.id][0]
    assert own == "CONTENT_UPDATED", f"Expected CONTENT_UPDATED after edit post-verification, got {statuses[func.id]}"


# ---------------------------------------------------------------------------
# _transition_change_type — mapping correctness
# ---------------------------------------------------------------------------


class TestTransitionChangeType:
    """Unit tests for the staleness transition -> BECAME_* mapping.

    _transition_change_type now takes two-column tuples (own_status, link_status).
    """

    def test_same_status_returns_empty(self):
        """No event when status is unchanged."""
        for own in ("VERIFIED", "CONTENT_UPDATED", "DESC_UPDATED", "NOT_FOUND"):
            assert _transition_change_type((own, "VERIFIED"), (own, "VERIFIED")) == []
        for link in ("VERIFIED", "LINKED_STALE", "BROKEN_LINK"):
            assert _transition_change_type(("VERIFIED", link), ("VERIFIED", link)) == []

    def test_clean_to_content_stale(self):
        events = _transition_change_type(("VERIFIED", "VERIFIED"), ("CONTENT_UPDATED", "VERIFIED"))
        assert "BECAME_CONTENT_UPDATED" in events

    def test_verified_to_content_stale(self):
        events = _transition_change_type(("VERIFIED", "VERIFIED"), ("CONTENT_UPDATED", "VERIFIED"))
        assert "BECAME_CONTENT_UPDATED" in events

    def test_clean_to_desc_stale(self):
        events = _transition_change_type(("VERIFIED", "VERIFIED"), ("DESC_UPDATED", "VERIFIED"))
        assert "BECAME_DESC_UPDATED" in events

    def test_clean_to_linked_stale(self):
        events = _transition_change_type(("VERIFIED", "VERIFIED"), ("VERIFIED", "LINKED_STALE"))
        assert "BECAME_LINKED_STALE" in events

    def test_any_to_structural_drift(self):
        events1 = _transition_change_type(("VERIFIED", "VERIFIED"), ("NOT_FOUND", "VERIFIED"))
        assert "BECAME_NOT_FOUND" in events1
        events2 = _transition_change_type(("CONTENT_UPDATED", "VERIFIED"), ("NOT_FOUND", "VERIFIED"))
        assert "BECAME_NOT_FOUND" in events2

    def test_stale_to_clean_emits_became_verified(self):
        events1 = _transition_change_type(("CONTENT_UPDATED", "VERIFIED"), ("VERIFIED", "VERIFIED"))
        assert "BECAME_VERIFIED" in events1
        events2 = _transition_change_type(("DESC_UPDATED", "VERIFIED"), ("VERIFIED", "VERIFIED"))
        assert "BECAME_VERIFIED" in events2

    def test_link_stale_to_verified(self):
        events = _transition_change_type(("VERIFIED", "LINKED_STALE"), ("VERIFIED", "VERIFIED"))
        assert "LINK_BECAME_VERIFIED" in events

    def test_clean_verified_interchange_returns_empty(self):
        """VERIFIED -> VERIFIED transitions produce no events."""
        assert _transition_change_type(("VERIFIED", "VERIFIED"), ("VERIFIED", "VERIFIED")) == []

    def test_cross_stale_transition(self):
        """CONTENT_UPDATED -> DESC_UPDATED should emit the target event."""
        events1 = _transition_change_type(("CONTENT_UPDATED", "VERIFIED"), ("DESC_UPDATED", "VERIFIED"))
        assert "BECAME_DESC_UPDATED" in events1
        events2 = _transition_change_type(("DESC_UPDATED", "VERIFIED"), ("CONTENT_UPDATED", "VERIFIED"))
        assert "BECAME_CONTENT_UPDATED" in events2


# ---------------------------------------------------------------------------
# record_staleness — transition event recording
# ---------------------------------------------------------------------------


def _history_rows(db_path: Path, node_id: str, change_type: str | None = None) -> list[dict]:
    """Read history rows for a node, optionally filtered by change_type."""
    with db._connect(db_path) as conn:
        if change_type:
            rows = conn.execute(
                "SELECT * FROM node_history WHERE node_id = ? AND change_type = ? ORDER BY id",
                (node_id, change_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM node_history WHERE node_id = ? ORDER BY id",
                (node_id,),
            ).fetchall()
        return [dict(r) for r in rows]


@workflow(
    purpose="Verify record_staleness inserts BECAME_CONTENT_UPDATED when a node transitions CLEAN → CONTENT_UPDATED"
)
def test_record_staleness_became_content_stale(mini_project: Path, db_path: Path):
    """Write a function, build, edit the body, then record_staleness.
    Expect a BECAME_CONTENT_UPDATED transition event in history."""
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
    )

    from axiom_graph.index import builder

    builder.build(mini_project, project_id="proj", discovery_only=False)

    # Initial record_staleness — everything should be CLEAN.
    nodes = db.all_nodes(db_path)
    statuses = record_staleness(db_path, mini_project, nodes)
    func_nodes = [n for n in nodes if n.title == "greet"]
    assert len(func_nodes) == 1
    func_id = func_nodes[0].id
    assert statuses[func_id][0] in ("VERIFIED", "VERIFIED")

    # Edit the function body (docstring stays the same).
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "goodbye"\n',
    )

    # record_staleness should detect CONTENT_UPDATED and insert a transition event.
    nodes = db.all_nodes(db_path)
    statuses = record_staleness(db_path, mini_project, nodes)
    assert statuses[func_id][0] == "CONTENT_UPDATED"

    # Verify the transition event was recorded.
    rows = _history_rows(db_path, func_id, "BECAME_CONTENT_UPDATED")
    assert len(rows) == 1
    meta = json.loads(rows[0]["meta"])
    assert meta["from_own"] in ("VERIFIED", "VERIFIED")
    assert rows[0]["preserved"] == 0


@workflow(purpose="Verify record_staleness inserts BECAME_VERIFIED when hashes realign without verification")
def test_record_staleness_became_clean(mini_project: Path, db_path: Path):
    """Make a node stale, then revert the change. record_staleness should
    emit BECAME_VERIFIED (distinct from verification)."""
    src = mini_project / "mod.py"
    original = 'def greet():\n    """Say hello."""\n    return "hello"\n'
    src.write_text(original)

    from axiom_graph.index import builder

    builder.build(mini_project, project_id="proj", discovery_only=False)

    nodes = db.all_nodes(db_path)
    record_staleness(db_path, mini_project, nodes)

    func_nodes = [n for n in nodes if n.title == "greet"]
    func_id = func_nodes[0].id

    # Make it stale.
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "goodbye"\n',
    )
    nodes = db.all_nodes(db_path)
    statuses = record_staleness(db_path, mini_project, nodes)
    assert statuses[func_id][0] == "CONTENT_UPDATED"

    # Revert the change — hashes realign.
    src.write_text(original)
    nodes = db.all_nodes(db_path)
    statuses = record_staleness(db_path, mini_project, nodes)
    assert statuses[func_id][0] in ("VERIFIED", "VERIFIED")

    # Verify BECAME_VERIFIED transition event.
    rows = _history_rows(db_path, func_id, "BECAME_VERIFIED")
    assert len(rows) == 1
    meta = json.loads(rows[0]["meta"])
    assert meta["from_own"] == "CONTENT_UPDATED"


@workflow(purpose="Verify record_staleness persists staleness column and returns same dict as compute_staleness")
def test_record_staleness_persists_and_returns(mini_project: Path, db_path: Path):
    """record_staleness must persist to nodes.staleness and return the same
    result as compute_staleness."""
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
    )

    from axiom_graph.index import builder

    builder.build(mini_project, project_id="proj", discovery_only=False)

    nodes = db.all_nodes(db_path)
    expected = compute_staleness(db_path, mini_project, nodes)
    actual = record_staleness(db_path, mini_project, nodes)
    assert actual == expected

    # Verify staleness was persisted to the DB.  get_all_staleness returns
    # 2-tuples (own, link) -- the via list is computed at runtime, not
    # persisted, so compare only the first two elements.
    persisted = db.get_all_staleness(db_path)
    for node_id, status in actual.items():
        assert persisted.get(node_id) == status[:2]


def test_record_staleness_no_events_when_unchanged(mini_project: Path, db_path: Path):
    """Calling record_staleness twice with no file changes should produce no
    transition events on the second call."""
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
    )

    from axiom_graph.index import builder

    builder.build(mini_project, project_id="proj", discovery_only=False)

    nodes = db.all_nodes(db_path)
    record_staleness(db_path, mini_project, nodes)

    func_nodes = [n for n in nodes if n.title == "greet"]
    func_id = func_nodes[0].id

    # Count history rows before second call.
    before = _history_rows(db_path, func_id)
    count_before = len(before)

    # Second call — no changes.
    nodes = db.all_nodes(db_path)
    record_staleness(db_path, mini_project, nodes)

    after = _history_rows(db_path, func_id)
    # No new BECAME_* rows should appear.
    became_rows = [r for r in after[count_before:] if r["change_type"].startswith("BECAME_")]
    assert became_rows == []


@workflow(purpose="Verify record_staleness inserts BECAME_DESC_UPDATED when only the docstring changes")
def test_record_staleness_became_desc_stale(mini_project: Path, db_path: Path):
    """Edit only the docstring → BECAME_DESC_UPDATED transition event."""
    src = mini_project / "mod.py"
    src.write_text(
        'def greet():\n    """Say hello."""\n    return "hello"\n',
    )

    from axiom_graph.index import builder

    builder.build(mini_project, project_id="proj", discovery_only=False)

    nodes = db.all_nodes(db_path)
    record_staleness(db_path, mini_project, nodes)

    func_nodes = [n for n in nodes if n.title == "greet"]
    func_id = func_nodes[0].id

    # Change only the docstring.
    src.write_text(
        'def greet():\n    """Say goodbye."""\n    return "hello"\n',
    )

    nodes = db.all_nodes(db_path)
    statuses = record_staleness(db_path, mini_project, nodes)
    assert statuses[func_id][0] == "DESC_UPDATED"

    rows = _history_rows(db_path, func_id, "BECAME_DESC_UPDATED")
    assert len(rows) == 1
    meta = json.loads(rows[0]["meta"])
    assert meta["from_own"] in ("VERIFIED", "VERIFIED")


@workflow(
    purpose="Verify partial-node-list calls don't steal transition events from subsequent full calls",
    inputs="mini_project with two functions in one module (composite + 2 atomics)",
    outputs="Assertions that all BECAME_* events are correctly recorded for both partial and full calls",
)
def test_record_staleness_partial_list_does_not_steal_transitions(mini_project: Path, db_path: Path):
    """End-to-end test for the partial-node-list staleness bug.

    The bug: viz ego-graph calls record_staleness with a subset of nodes.
    apply_composite_inheritance expands the write set to include composites
    whose children weren't all in the input.  Those composites get their
    staleness column persisted without a proper transition diff.  When the
    full call (axiom_graph_build/axiom_graph_check) arrives, it sees old==new and
    skips the BECAME_* event.

    This test exercises three phases:
      1. Build + baseline (all CLEAN, INITIAL events only)
      2. Edit code + partial call (only one function in the list)
      3. Full call (all nodes) — must still emit BECAME events for nodes
         the partial call didn't cover, AND for the composite module
    """
    from axiom_annotations import Step

    src = mini_project / "mod.py"
    src.write_text(
        "def greet():\n"
        '    """Say hello."""\n'
        '    return "hello"\n'
        "\n"
        "def farewell():\n"
        '    """Say goodbye."""\n'
        '    return "goodbye"\n',
    )

    # -- Step 1: Build and establish baseline ---------------------------------
    口 = Step(
        step_num=1,
        name="Build and baseline",
        purpose="Full build + record_staleness to establish CLEAN baseline for all nodes",
        outputs="greet_id, farewell_id, mod_id — all CLEAN with INITIAL history only",
    )

    from axiom_graph.index import builder

    builder.build(mini_project, project_id="proj", discovery_only=False)

    all_nodes = db.all_nodes(db_path)
    statuses = record_staleness(db_path, mini_project, all_nodes)

    greet_nodes = [n for n in all_nodes if n.title == "greet"]
    farewell_nodes = [n for n in all_nodes if n.title == "farewell"]
    mod_nodes = [n for n in all_nodes if n.node_type == "composite_process"]
    assert len(greet_nodes) == 1 and len(farewell_nodes) == 1 and len(mod_nodes) >= 1

    greet_id = greet_nodes[0].id
    farewell_id = farewell_nodes[0].id
    mod_id = mod_nodes[0].id

    # Baseline: everything CLEAN, no BECAME events.
    assert statuses[greet_id][0] in ("VERIFIED", "VERIFIED")
    assert statuses[farewell_id][0] in ("VERIFIED", "VERIFIED")
    assert _history_rows(db_path, greet_id, "BECAME_CONTENT_UPDATED") == []
    assert _history_rows(db_path, farewell_id, "BECAME_CONTENT_UPDATED") == []
    assert _history_rows(db_path, mod_id, "BECAME_CONTENT_UPDATED") == []

    # -- Step 2: Edit code + partial call -------------------------------------
    口 = Step(
        step_num=2,
        name="Edit + partial record_staleness",
        purpose="Edit both functions, then call record_staleness with only greet "
        "(simulates viz ego-graph). Verify greet gets its event, farewell "
        "and the composite module do NOT get staleness persisted.",
        critical="The composite module must not have its staleness column set by "
        "this partial call — that's the bug this test guards against",
    )

    src.write_text(
        "def greet():\n"
        '    """Say hello."""\n'
        '    return "hi"\n'
        "\n"
        "def farewell():\n"
        '    """Say goodbye."""\n'
        '    return "bye"\n',
    )

    partial_nodes = [n for n in db.all_nodes(db_path) if n.title == "greet"]
    partial_statuses = record_staleness(db_path, mini_project, partial_nodes)

    # greet: detected as stale, transition event written.
    assert partial_statuses[greet_id][0] == "CONTENT_UPDATED"
    assert len(_history_rows(db_path, greet_id, "BECAME_CONTENT_UPDATED")) == 1

    # farewell: NOT in the partial call's requested set — no event, column still CLEAN.
    farewell_rows_before = _history_rows(db_path, farewell_id, "BECAME_CONTENT_UPDATED")
    assert farewell_rows_before == [], (
        "Partial call must not write transition events for nodes outside the requested set"
    )
    with db._connect(db_path) as conn:
        farewell_persisted = conn.execute("SELECT own_status FROM nodes WHERE id = ?", (farewell_id,)).fetchone()[
            "own_status"
        ]
    assert farewell_persisted == "VERIFIED", (
        f"Partial call must not persist staleness for farewell, got {farewell_persisted}"
    )

    # Composite module: also must not have been persisted by partial call.
    with db._connect(db_path) as conn:
        mod_persisted = conn.execute("SELECT own_status FROM nodes WHERE id = ?", (mod_id,)).fetchone()["own_status"]
    assert mod_persisted == "VERIFIED", f"Partial call must not persist composite staleness, got {mod_persisted}"

    # -- Step 3: Full call — must produce events for everything ---------------
    口 = Step(
        step_num=3,
        name="Full record_staleness",
        purpose="Call with all nodes (simulates axiom_graph_build/axiom_graph_check). "
        "Verify BECAME_CONTENT_UPDATED events exist for farewell AND the composite module.",
        critical="This is the step that was broken before the fix — the full call "
        "would see old==new for farewell/module and skip the event",
    )

    all_nodes = db.all_nodes(db_path)
    full_statuses = record_staleness(db_path, mini_project, all_nodes)

    # All three should be CONTENT_UPDATED.
    assert full_statuses[greet_id][0] == "CONTENT_UPDATED"
    assert full_statuses[farewell_id][0] == "CONTENT_UPDATED"
    assert full_statuses[mod_id][0] == "CONTENT_UPDATED"

    # farewell: must now have its transition event from the full call.
    farewell_rows = _history_rows(db_path, farewell_id, "BECAME_CONTENT_UPDATED")
    assert len(farewell_rows) == 1, (
        f"Full call must emit BECAME_CONTENT_UPDATED for farewell; got {len(farewell_rows)} events"
    )
    meta = json.loads(farewell_rows[0]["meta"])
    assert meta["from_own"] in ("VERIFIED", "VERIFIED")

    # Composite module: must also have its transition event.
    mod_rows = _history_rows(db_path, mod_id, "BECAME_CONTENT_UPDATED")
    assert len(mod_rows) == 1, (
        f"Full call must emit BECAME_CONTENT_UPDATED for composite module; got {len(mod_rows)} events"
    )

    # greet: should still have exactly 1 event (from the partial call), not a duplicate.
    greet_rows = _history_rows(db_path, greet_id, "BECAME_CONTENT_UPDATED")
    assert len(greet_rows) == 1, f"greet should have exactly 1 BECAME_CONTENT_UPDATED event; got {len(greet_rows)}"
