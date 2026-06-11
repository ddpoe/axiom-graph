"""Tests for MCP tool improvements (PEV orchestrator simplification prerequisites).

0a. axiom_graph_read_doc TOC mode for large docs
0b. axiom_graph_check filter param + DOC_SECTION_LONG
0c. axiom_graph_source module-level truncation
0d. axiom_graph_graph max_results cap
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path


from axiom_graph.index import db
from axiom_graph.models import AxiomEdge, AxiomNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_doc(db_path: Path, doc_id: str, title: str, sections: list[dict]) -> None:
    """Write a doc with sections into the DB, including the doc node."""
    now = datetime.now(timezone.utc).isoformat()
    with db._connect(db_path) as conn:
        db.upsert_doc(
            conn,
            {
                "id": doc_id,
                "title": title,
                "tags": "",
                "file_path": f"docs/{doc_id.split('::')[-1]}.json",
                "desc_hash": "test",
                "updated_at": now,
            },
        )
        for i, sec in enumerate(sections):
            content = sec.get("content", "")
            db.upsert_doc_section(
                conn,
                {
                    "id": sec["id"],
                    "doc_id": doc_id,
                    "heading": sec["heading"],
                    "level": sec.get("level", 2),
                    "tags": sec.get("tags", ""),
                    "content": content,
                    "desc_hash": hashlib.sha256(content.encode()).hexdigest()[:16],
                    "parent_id": sec.get("parent_id"),
                    "depth": sec.get("depth", 0),
                    "position": i,
                    "updated_at": now,
                },
            )
    # Create a node so get_node() finds the doc
    _upsert_node(
        db_path,
        doc_id,
        title=title,
        node_type="composite_process",
        subtype="docjson",
        location=f"docs/{doc_id.split('::')[-1]}.json",
        source="json_doc_scanner",
    )


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
    node = AxiomNode(**defaults)
    db.upsert_node(db_path, node, discovery_only=False)


def _insert_edge(db_path: Path, edge_type: str, from_id: str, to_id: str) -> None:
    edge = AxiomEdge(
        id=f"{from_id}::{edge_type}::{to_id}",
        edge_type=edge_type,
        from_id=from_id,
        to_id=to_id,
    )
    db.upsert_edge(db_path, edge)


# ===========================================================================
# 0a: axiom_graph_read_doc TOC mode
# ===========================================================================


class TestReadDocTocMode:
    """When full rendered doc exceeds threshold and no section param, return TOC."""

    def test_small_doc_returns_full_content(self, mini_project: Path) -> None:
        """Docs under the threshold should return full markdown as before."""
        from axiom_graph.mcp_server import axiom_graph_read_doc

        db_path = mini_project / ".axiom_graph" / "graph.db"
        _write_doc(
            db_path,
            "test::docs.small",
            "Small Doc",
            [
                {"id": "test::docs.small::intro", "heading": "Intro", "content": "Short content."},
            ],
        )
        result = axiom_graph_read_doc(str(mini_project), "test::docs.small")
        assert "# Small Doc" in result
        assert "Short content." in result
        # Should NOT contain the TOC hint
        assert "Use section=" not in result

    def test_large_doc_returns_toc(self, mini_project: Path) -> None:
        """Docs over the threshold should return a TOC with section slugs and char counts."""
        from axiom_graph.mcp_server import axiom_graph_read_doc

        db_path = mini_project / ".axiom_graph" / "graph.db"
        # Create a doc with enough content to exceed 3000 chars
        sections = []
        for i in range(10):
            sections.append(
                {
                    "id": f"test::docs.big::section-{i}",
                    "heading": f"Section {i}",
                    "content": "x" * 400,  # 10 * 400 = 4000 chars of content alone
                }
            )
        _write_doc(db_path, "test::docs.big", "Big Doc", sections)

        result = axiom_graph_read_doc(str(mini_project), "test::docs.big")

        # Should contain the TOC hint instructing how to drill in
        assert "section=" in result
        # Should list section slugs
        assert "section-0" in result
        assert "section-9" in result
        # Should show char counts
        assert "400" in result
        # Should NOT contain the full content body
        assert "x" * 400 not in result

    def test_large_doc_with_section_param_returns_full_section(self, mini_project: Path) -> None:
        """Even for large docs, specifying section= returns the full section content."""
        from axiom_graph.mcp_server import axiom_graph_read_doc

        db_path = mini_project / ".axiom_graph" / "graph.db"
        sections = []
        for i in range(10):
            sections.append(
                {
                    "id": f"test::docs.big2::section-{i}",
                    "heading": f"Section {i}",
                    "content": f"unique-content-{i} " + "x" * 400,
                }
            )
        _write_doc(db_path, "test::docs.big2", "Big Doc 2", sections)

        result = axiom_graph_read_doc(str(mini_project), "test::docs.big2", section="section-3")
        assert "unique-content-3" in result

    def test_toc_shows_section_count_and_total_chars(self, mini_project: Path) -> None:
        """TOC should mention number of sections and total char count."""
        from axiom_graph.mcp_server import axiom_graph_read_doc

        db_path = mini_project / ".axiom_graph" / "graph.db"
        sections = [{"id": f"test::docs.big3::s-{i}", "heading": f"S{i}", "content": "a" * 500} for i in range(8)]
        _write_doc(db_path, "test::docs.big3", "Big Doc 3", sections)

        result = axiom_graph_read_doc(str(mini_project), "test::docs.big3")
        assert "8 sections" in result


# ===========================================================================
# 0b: axiom_graph_check (slimmed) + axiom_graph_drift_query DOC_SECTION_LONG
#
# History note (2026-05 drift_query cycle): axiom_graph_check used to take
# verbose+filter params; those were removed in favour of
# axiom_graph_drift_query.  Tests below exercise the slim check + the
# drift_query DOC_SECTION_LONG migration path.
# ===========================================================================


class TestCheckSlimmed:
    """axiom_graph_check returns one-line summary; verbose/filter are gone."""

    def test_check_with_long_section_shows_count_in_summary(self, git_project: Path) -> None:
        """Summary always includes DOC_SECTION_LONG count when long sections exist."""
        from axiom_graph.mcp_server import axiom_graph_check

        db_path = git_project / ".axiom_graph" / "graph.db"
        _write_doc(
            db_path,
            "test::docs.long",
            "Long Doc",
            [
                {"id": "test::docs.long::big", "heading": "Big Section", "content": "y" * 2500},
            ],
        )
        _upsert_node(db_path, "test::mod::func_a")

        result = axiom_graph_check(str(git_project))
        # Slim summary now reports DOC_SECTION_LONG count whenever any
        # long sections exist (no filter param to gate it).
        assert "DOC_SECTION_LONG" in result
        # And per-section detail is NOT in check output.
        assert "test::docs.long::big" not in result

    def test_check_no_long_sections_omits_advisory(self, git_project: Path) -> None:
        """When no long sections exist, summary omits DOC_SECTION_LONG token."""
        from axiom_graph.mcp_server import axiom_graph_check

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "test::mod::lone")

        result = axiom_graph_check(str(git_project))
        assert "DOC_SECTION_LONG" not in result

    def test_check_rejects_legacy_verbose_param(self, git_project: Path) -> None:
        """check(verbose=...) raises TypeError -- migration is mechanical."""
        from axiom_graph.mcp_server import axiom_graph_check

        import pytest

        _upsert_node(git_project / ".axiom_graph" / "graph.db", "test::mod::any")
        with pytest.raises(TypeError):
            axiom_graph_check(str(git_project), verbose=True)

    def test_check_rejects_legacy_filter_param(self, git_project: Path) -> None:
        """check(filter=...) raises TypeError -- migrate to drift_query."""
        from axiom_graph.mcp_server import axiom_graph_check

        import pytest

        _upsert_node(git_project / ".axiom_graph" / "graph.db", "test::mod::any2")
        with pytest.raises(TypeError):
            axiom_graph_check(str(git_project), filter="staleness")


# ===========================================================================
# 0c: axiom_graph_source module-level truncation
# ===========================================================================


class TestSourceModuleTruncation:
    """Large modules show first 50 lines + children TOC instead of full file."""

    def test_small_module_returns_full_source(self, mini_project: Path) -> None:
        """Modules under 200 lines should return full content."""
        from axiom_graph.mcp_server import axiom_graph_source

        db_path = mini_project / ".axiom_graph" / "graph.db"
        src = mini_project / "small.py"
        src.write_text("\n".join(f"line_{i} = {i}" for i in range(50)), encoding="utf-8")

        _upsert_node(
            db_path, "test::small", node_type="composite_process", location="small.py", level_3_location="small.py"
        )

        result = axiom_graph_source(str(mini_project), "test::small")
        assert "line_0 = 0" in result
        assert "line_49 = 49" in result

    def test_large_module_returns_truncated_with_children(self, mini_project: Path) -> None:
        """Modules over 200 lines should show first 50 lines + children listing."""
        from axiom_graph.mcp_server import axiom_graph_source

        db_path = mini_project / ".axiom_graph" / "graph.db"
        src = mini_project / "big.py"
        lines = [f"line_{i} = {i}" for i in range(300)]
        src.write_text("\n".join(lines), encoding="utf-8")

        _upsert_node(db_path, "test::big", node_type="composite_process", location="big.py", level_3_location="big.py")
        _upsert_node(
            db_path,
            "test::big::func_a",
            node_type="atomic_process",
            location="big.py",
            level_3_location="big.py#L10-L30",
        )
        _upsert_node(
            db_path,
            "test::big::func_b",
            node_type="atomic_process",
            location="big.py",
            level_3_location="big.py#L50-L80",
        )
        _insert_edge(db_path, "composes", "test::big", "test::big::func_a")
        _insert_edge(db_path, "composes", "test::big", "test::big::func_b")

        result = axiom_graph_source(str(mini_project), "test::big")

        # Should show first 50 lines
        assert "line_0 = 0" in result
        assert "line_49 = 49" in result
        # Should NOT show lines beyond 50
        assert "line_200 = 200" not in result
        # Should list children
        assert "test::big::func_a" in result
        assert "test::big::func_b" in result
        # Should mention the total line count
        assert "300 lines" in result

    def test_function_node_returns_full_source_regardless_of_length(self, mini_project: Path) -> None:
        """Function nodes (with #L range) should always return full source."""
        from axiom_graph.mcp_server import axiom_graph_source

        db_path = mini_project / ".axiom_graph" / "graph.db"
        src = mini_project / "funcs.py"
        lines = [f"line_{i} = {i}" for i in range(300)]
        src.write_text("\n".join(lines), encoding="utf-8")

        _upsert_node(
            db_path,
            "test::funcs::my_func",
            node_type="atomic_process",
            location="funcs.py",
            level_3_location="funcs.py#L1-L300",
        )

        result = axiom_graph_source(str(mini_project), "test::funcs::my_func")
        assert "line_299 = 299" in result

    def test_large_non_composite_returns_full_source(self, mini_project: Path) -> None:
        """Non-composite_process nodes over 200 lines still return full content."""
        from axiom_graph.mcp_server import axiom_graph_source

        db_path = mini_project / ".axiom_graph" / "graph.db"
        # Use a .txt file so the scanner won't rescan and change node_type
        src = mini_project / "data.txt"
        lines = [f"line_{i} = {i}" for i in range(300)]
        src.write_text("\n".join(lines), encoding="utf-8")

        _upsert_node(
            db_path, "test::data", node_type="atomic_process", location="data.txt", level_3_location="data.txt"
        )

        result = axiom_graph_source(str(mini_project), "test::data")
        # Full content returned — not a composite_process
        assert "line_299 = 299" in result


# ===========================================================================
# 0d: axiom_graph_graph max_results cap
# ===========================================================================


class TestGraphMaxResults:
    """axiom_graph_graph with max_results cap."""

    def test_under_cap_returns_all_edges(self, mini_project: Path) -> None:
        """When edges < max_results, all are returned normally."""
        from axiom_graph.mcp_server import axiom_graph_graph

        db_path = mini_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "test::root")
        for i in range(5):
            _upsert_node(db_path, f"test::dep_{i}")
            _insert_edge(db_path, "calls", "test::root", f"test::dep_{i}")

        result = axiom_graph_graph(str(mini_project), "test::root", direction="out")
        for i in range(5):
            assert f"test::dep_{i}" in result

    def test_over_cap_truncates_with_message(self, mini_project: Path) -> None:
        """When edges > max_results, output is truncated with guidance."""
        from axiom_graph.mcp_server import axiom_graph_graph

        db_path = mini_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "test::hub")
        for i in range(50):
            _upsert_node(db_path, f"test::spoke_{i}")
            _insert_edge(db_path, "calls", "test::hub", f"test::spoke_{i}")

        result = axiom_graph_graph(str(mini_project), "test::hub", direction="out", max_results=10)

        # Should have truncation message mentioning both the cap and total
        assert "10" in result
        assert "50" in result

    def test_default_max_results_is_40(self, mini_project: Path) -> None:
        """Default max_results should be 40."""
        from axiom_graph.mcp_server import axiom_graph_graph

        db_path = mini_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "test::bigroot")
        for i in range(50):
            _upsert_node(db_path, f"test::child_{i}")
            _insert_edge(db_path, "calls", "test::bigroot", f"test::child_{i}")

        result = axiom_graph_graph(str(mini_project), "test::bigroot", direction="out")

        # Should truncate at 40 (default) and mention 50 total
        assert "40" in result
        assert "50" in result

    def test_explicit_high_cap_returns_all(self, mini_project: Path) -> None:
        """Setting max_results higher than edge count returns everything."""
        from axiom_graph.mcp_server import axiom_graph_graph

        db_path = mini_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "test::center")
        for i in range(5):
            _upsert_node(db_path, f"test::leaf_{i}")
            _insert_edge(db_path, "calls", "test::center", f"test::leaf_{i}")

        result = axiom_graph_graph(str(mini_project), "test::center", direction="out", max_results=100)
        for i in range(5):
            assert f"test::leaf_{i}" in result
