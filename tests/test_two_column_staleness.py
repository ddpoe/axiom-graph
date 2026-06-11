"""Tests for the two-column staleness split (own_status + link_status).

Covers: schema migration, two-column computation, per-dimension composite
inheritance, verification scoping, persist/read round-trip, and transition
events.
"""

from __future__ import annotations

from pathlib import Path


from axiom_graph.index import db
from axiom_graph.index.staleness import (
    apply_composite_inheritance,
    compute_staleness,
    _transition_change_type,
    _OWN_SEVERITY,
    _LINK_SEVERITY,
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
# Task 1: Schema columns
# ---------------------------------------------------------------------------


class TestSchemaColumns:
    """Verify that init_db creates the two-column schema."""

    def test_fresh_db_has_both_columns(self, db_path: Path):
        """A freshly created DB should have own_status and link_status columns."""
        with db._connect(db_path) as conn:
            info = conn.execute("PRAGMA table_info(nodes)").fetchall()
            col_names = {r["name"] for r in info}
        assert "own_status" in col_names
        assert "link_status" in col_names

    def test_fresh_db_defaults_verified(self, db_path: Path):
        """New nodes should default to VERIFIED for both columns."""
        n = _node("proj::mod::func")
        db.upsert_node(db_path, n, discovery_only=False)
        with db._connect(db_path) as conn:
            row = conn.execute(
                "SELECT own_status, link_status FROM nodes WHERE id = ?",
                ("proj::mod::func",),
            ).fetchone()
        assert row["own_status"] == "VERIFIED"
        assert row["link_status"] == "VERIFIED"


# ---------------------------------------------------------------------------
# Task 1: persist_staleness / get_all_staleness round-trip (two columns)
# ---------------------------------------------------------------------------


class TestPersistStalenessRoundTrip:
    """Verify two-column persist and read."""

    def test_round_trip_two_columns(self, db_path: Path):
        """Write two-column statuses, read back both."""
        a = _node("proj::mod::a")
        b = _node("proj::mod::b")
        db.upsert_node(db_path, a, discovery_only=False)
        db.upsert_node(db_path, b, discovery_only=False)

        statuses = {
            "proj::mod::a": ("CONTENT_UPDATED", "VERIFIED"),
            "proj::mod::b": ("VERIFIED", "LINKED_STALE"),
        }
        db.persist_staleness(db_path, statuses)

        result = db.get_all_staleness(db_path)
        assert result["proj::mod::a"] == ("CONTENT_UPDATED", "VERIFIED")
        assert result["proj::mod::b"] == ("VERIFIED", "LINKED_STALE")

    def test_overwrite(self, db_path: Path):
        """Persisting again overwrites previous values."""
        n = _node("proj::mod::func")
        db.upsert_node(db_path, n, discovery_only=False)

        db.persist_staleness(db_path, {"proj::mod::func": ("VERIFIED", "VERIFIED")})
        assert db.get_all_staleness(db_path)["proj::mod::func"] == ("VERIFIED", "VERIFIED")

        db.persist_staleness(db_path, {"proj::mod::func": ("CONTENT_UPDATED", "LINKED_STALE")})
        assert db.get_all_staleness(db_path)["proj::mod::func"] == ("CONTENT_UPDATED", "LINKED_STALE")


# ---------------------------------------------------------------------------
# Task 2: Staleness engine — compute produces two values
# ---------------------------------------------------------------------------


class TestComputeTwoColumns:
    """Verify compute_staleness returns tuples."""

    def test_clean_node_returns_verified_pair(self, mini_project: Path, db_path: Path):
        """An unchanged node should return (VERIFIED, VERIFIED)."""
        src = mini_project / "mod.py"
        src.write_text('def greet():\n    """Say hello."""\n    return "hello"\n')
        from axiom_graph.index import builder

        builder.build(mini_project, project_id="proj", discovery_only=False)

        nodes = db.all_nodes(db_path)
        statuses = compute_staleness(db_path, mini_project, nodes)
        func_nodes = [n for n in nodes if n.title == "greet"]
        assert len(func_nodes) == 1
        s = statuses[func_nodes[0].id]
        assert isinstance(s, tuple)
        assert len(s) == 3
        # Both dimensions should be clean, via list empty
        assert s[0] in ("VERIFIED", "VERIFIED")
        assert s[1] in ("VERIFIED", "VERIFIED")
        assert s[2] == []

    def test_content_change_sets_own_status(self, mini_project: Path, db_path: Path):
        """Editing function body sets own_status to CONTENT_UPDATED."""
        src = mini_project / "mod.py"
        src.write_text('def greet():\n    """Say hello."""\n    return "hello"\n')
        from axiom_graph.index import builder

        builder.build(mini_project, project_id="proj", discovery_only=False)

        src.write_text('def greet():\n    """Say hello."""\n    return "goodbye"\n')

        nodes = db.all_nodes(db_path)
        statuses = compute_staleness(db_path, mini_project, nodes)
        func_nodes = [n for n in nodes if n.title == "greet"]
        own, link, via = statuses[func_nodes[0].id]
        assert own == "CONTENT_UPDATED"
        # Link status should not be affected by own content change
        assert link in ("VERIFIED", "VERIFIED")
        assert via == []


# ---------------------------------------------------------------------------
# Task 3: Composite inheritance per dimension
# ---------------------------------------------------------------------------


def _setup_composite(db_path: Path, children_statuses: dict[str, tuple[str, str]]) -> tuple[str, dict]:
    """Insert a composite parent and leaf children, return parent id and statuses dict."""
    parent_id = "proj::pkg"
    db.upsert_node(db_path, _node(parent_id, node_type="composite_process"), discovery_only=False)
    statuses = {}
    for child_id, status_pair in children_statuses.items():
        db.upsert_node(db_path, _node(child_id), discovery_only=False)
        db.upsert_edge(db_path, _edge(parent_id, "composes", child_id))
        statuses[child_id] = status_pair
    return parent_id, statuses


class TestCompositeInheritancePerDimension:
    """Composite nodes should inherit worst own_status and worst link_status independently."""

    def test_independent_dimensions(self, db_path: Path):
        """One child CONTENT_UPDATED, another BROKEN_LINK: parent gets worst of each."""
        parent_id, statuses = _setup_composite(
            db_path,
            {
                "proj::pkg::a": ("CONTENT_UPDATED", "VERIFIED"),
                "proj::pkg::b": ("VERIFIED", "BROKEN_LINK"),
            },
        )
        result = apply_composite_inheritance(statuses, db_path)
        own, link = result[parent_id]
        assert own == "CONTENT_UPDATED"
        assert link == "BROKEN_LINK"

    def test_all_verified_stays_verified(self, db_path: Path):
        """All clean children mean parent is (VERIFIED, VERIFIED)."""
        parent_id, statuses = _setup_composite(
            db_path,
            {
                "proj::pkg::a": ("VERIFIED", "VERIFIED"),
                "proj::pkg::b": ("VERIFIED", "VERIFIED"),
            },
        )
        result = apply_composite_inheritance(statuses, db_path)
        own, link = result[parent_id]
        assert own in ("VERIFIED", "VERIFIED")
        assert link in ("VERIFIED", "VERIFIED")

    def test_own_severity_ordering(self, db_path: Path):
        """NOT_FOUND beats CONTENT_UPDATED in own_status."""
        parent_id, statuses = _setup_composite(
            db_path,
            {
                "proj::pkg::a": ("CONTENT_UPDATED", "VERIFIED"),
                "proj::pkg::b": ("NOT_FOUND", "VERIFIED"),
            },
        )
        result = apply_composite_inheritance(statuses, db_path)
        own, link = result[parent_id]
        assert own == "NOT_FOUND"

    def test_link_severity_ordering(self, db_path: Path):
        """BROKEN_LINK beats LINKED_STALE in link_status."""
        parent_id, statuses = _setup_composite(
            db_path,
            {
                "proj::pkg::a": ("VERIFIED", "LINKED_STALE"),
                "proj::pkg::b": ("VERIFIED", "BROKEN_LINK"),
            },
        )
        result = apply_composite_inheritance(statuses, db_path)
        own, link = result[parent_id]
        assert link == "BROKEN_LINK"


# ---------------------------------------------------------------------------
# Task 4: Verification scoping — mark_clean resets own_status only
# ---------------------------------------------------------------------------


class TestVerificationScoping:
    """mark_clean / update_node_baseline should only reset own_status."""

    def test_update_baseline_resets_own_only(self, db_path: Path):
        """After update_node_baseline, own_status=VERIFIED but link_status is preserved."""
        n = _node("proj::mod::func")
        db.upsert_node(db_path, n, discovery_only=False)
        # Set both columns to stale values
        db.persist_staleness(
            db_path,
            {
                "proj::mod::func": ("CONTENT_UPDATED", "LINKED_STALE"),
            },
        )

        db.update_node_baseline(
            db_path,
            "proj::mod::func",
            code_hash="new_hash",
            desc_hash="new_desc",
        )

        result = db.get_all_staleness(db_path)
        own, link = result["proj::mod::func"]
        assert own == "VERIFIED"
        assert link == "LINKED_STALE"  # must be preserved


# ---------------------------------------------------------------------------
# Task 2: _transition_change_type — new event names
# ---------------------------------------------------------------------------


class TestTransitionChangeTypeNewNames:
    """Verify _transition_change_type uses new BECAME_* names for new statuses."""

    def test_became_content_updated(self):
        events = _transition_change_type(
            ("VERIFIED", "VERIFIED"),
            ("CONTENT_UPDATED", "VERIFIED"),
        )
        assert "BECAME_CONTENT_UPDATED" in events

    def test_became_desc_updated(self):
        events = _transition_change_type(
            ("VERIFIED", "VERIFIED"),
            ("DESC_UPDATED", "VERIFIED"),
        )
        assert "BECAME_DESC_UPDATED" in events

    def test_became_linked_stale(self):
        events = _transition_change_type(
            ("VERIFIED", "VERIFIED"),
            ("VERIFIED", "LINKED_STALE"),
        )
        assert "BECAME_LINKED_STALE" in events

    def test_became_broken_link(self):
        events = _transition_change_type(
            ("VERIFIED", "VERIFIED"),
            ("VERIFIED", "BROKEN_LINK"),
        )
        assert "BECAME_BROKEN_LINK" in events

    def test_became_not_found(self):
        events = _transition_change_type(
            ("VERIFIED", "VERIFIED"),
            ("NOT_FOUND", "VERIFIED"),
        )
        assert "BECAME_NOT_FOUND" in events

    def test_no_change_returns_empty(self):
        events = _transition_change_type(
            ("VERIFIED", "VERIFIED"),
            ("VERIFIED", "VERIFIED"),
        )
        assert events == []

    def test_both_dimensions_change(self):
        """A single compute pass can produce events for both dimensions."""
        events = _transition_change_type(
            ("VERIFIED", "VERIFIED"),
            ("CONTENT_UPDATED", "LINKED_STALE"),
        )
        assert "BECAME_CONTENT_UPDATED" in events
        assert "BECAME_LINKED_STALE" in events
        assert len(events) == 2

    def test_own_became_verified(self):
        """Stale own -> VERIFIED: emits BECAME_VERIFIED event."""
        events = _transition_change_type(
            ("CONTENT_UPDATED", "VERIFIED"),
            ("VERIFIED", "VERIFIED"),
        )
        assert events == ["BECAME_VERIFIED"]

    def test_link_became_verified(self):
        """Link goes from stale to VERIFIED: emit BECAME_VERIFIED_LINK."""
        events = _transition_change_type(
            ("VERIFIED", "LINKED_STALE"),
            ("VERIFIED", "VERIFIED"),
        )
        assert "LINK_BECAME_VERIFIED" in events


# ---------------------------------------------------------------------------
# Severity constants
# ---------------------------------------------------------------------------


class TestSeverityConstants:
    """Verify the new severity ladders exist and are ordered correctly."""

    def test_own_severity_order(self):
        assert _OWN_SEVERITY["VERIFIED"] < _OWN_SEVERITY["DESC_UPDATED"]
        assert _OWN_SEVERITY["DESC_UPDATED"] < _OWN_SEVERITY["CONTENT_UPDATED"]
        assert _OWN_SEVERITY["CONTENT_UPDATED"] < _OWN_SEVERITY["NOT_FOUND"]

    def test_link_severity_order(self):
        assert _LINK_SEVERITY["VERIFIED"] < _LINK_SEVERITY["LINKED_STALE"]
        assert _LINK_SEVERITY["LINKED_STALE"] < _LINK_SEVERITY["BROKEN_LINK"]
