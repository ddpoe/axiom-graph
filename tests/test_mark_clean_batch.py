"""Tests for batch axiom_graph_mark_clean support."""

from __future__ import annotations

from pathlib import Path

from axiom_graph.index import db
from axiom_graph.models import AxiomNode


def _upsert_node(db_path: Path, node_id: str, **kwargs) -> None:
    defaults = dict(
        id=node_id,
        node_type="atomic_process",
        title=node_id.split("::")[-1],
        location="mod.py",
        source="ast",
        code_hash="hash_aaa",
        level_0=node_id.split("::")[-1],
        level_1=node_id.split("::")[-1],
    )
    defaults.update(kwargs)
    db.upsert_node(db_path, AxiomNode(**defaults), discovery_only=False)


class TestMarkCleanBatch:
    """Batch mode for axiom_graph_mark_clean."""

    def test_single_node_id_still_works(self, mini_project: Path) -> None:
        """Existing single-node behavior is preserved."""
        from axiom_graph.mcp_server import axiom_graph_mark_clean

        db_path = mini_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "test::mod::func_a")

        result = axiom_graph_mark_clean(str(mini_project), node_id="test::mod::func_a", reason="docs match")
        assert "AGENT_VERIFIED" in result
        assert "test::mod::func_a" in result

    def test_batch_node_ids_marks_all(self, mini_project: Path) -> None:
        """node_ids list marks all nodes clean in one call."""
        from axiom_graph.mcp_server import axiom_graph_mark_clean

        db_path = mini_project / ".axiom_graph" / "graph.db"
        ids = [f"test::mod::func_{i}" for i in range(5)]
        for nid in ids:
            _upsert_node(db_path, nid)

        result = axiom_graph_mark_clean(
            str(mini_project),
            node_id="ignored",
            reason="batch verification",
            node_ids=ids,
        )
        assert "5" in result
        assert "AGENT_VERIFIED" in result
        for nid in ids:
            assert nid in result

    def test_batch_skips_unknown_nodes(self, mini_project: Path) -> None:
        """Unknown node IDs in batch are reported but don't block others."""
        from axiom_graph.mcp_server import axiom_graph_mark_clean

        db_path = mini_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "test::mod::exists")

        result = axiom_graph_mark_clean(
            str(mini_project),
            node_id="ignored",
            reason="partial batch",
            node_ids=["test::mod::exists", "test::mod::ghost"],
        )
        assert "1" in result  # 1 marked
        assert "AGENT_VERIFIED" in result
        assert "ghost" in result  # mentioned as not found

    def test_batch_uses_shared_reason_and_verified_by(self, mini_project: Path) -> None:
        """All nodes in a batch share the same reason and verified_by."""
        from axiom_graph.mcp_server import axiom_graph_mark_clean

        db_path = mini_project / ".axiom_graph" / "graph.db"
        ids = ["test::mod::a", "test::mod::b"]
        for nid in ids:
            _upsert_node(db_path, nid)

        axiom_graph_mark_clean(
            str(mini_project),
            node_id="ignored",
            reason="batch reason",
            verified_by="agent:test",
            node_ids=ids,
        )

        # Verify DB has verification rows for both
        for nid in ids:
            v = db.get_verification(db_path, nid)
            assert v is not None
            assert v["verified_by"] == "agent:test"
            assert v["reason"] == "batch reason"
