"""Tests for axiom_graph_drift_query — filtered/grouped/paginated drift inventory.

Architect's test plan (US-1, US-2, US-3 from
pev-2026-05-01-drift-query-and-spill):

- US-1 location_glob filters before projection; format='ids' shape correct.
- US-1 pagination is correct (page=0,limit=N | page=1,limit=N = unpaginated).
- US-2 group_by='location_prefix' counts correct per top-level dir.
- US-2 group_by='feature' resolves via inbound documents edges + (undocumented) sentinel.
- US-2 filter-vocab parity (drift_query accepts the same vocab as legacy check(filter=...)).
- US-3 check(verbose=True) raises TypeError (covered in test_mcp_tool_improvements.py).

All tests use the ``git_project`` / ``mini_project`` fixtures from
``conftest.py`` and seed the DB directly so we get deterministic
own_status / link_status values without invoking the staleness compute
path.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from axiom_graph.index import db
from axiom_graph.models import AxiomEdge, AxiomNode


# ---------------------------------------------------------------------------
# Helpers (mirror test_mcp_tool_improvements style)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert_node(
    db_path: Path,
    node_id: str,
    *,
    own_status: str = "VERIFIED",
    link_status: str = "VERIFIED",
    location: str = "mod.py",
    level_3_location: str | None = None,
    node_type: str = "atomic_process",
    subtype: str | None = None,
) -> None:
    node = AxiomNode(
        id=node_id,
        node_type=node_type,
        subtype=subtype,
        title=node_id.split("::")[-1],
        location=location,
        level_3_location=level_3_location,
        source="ast",
        code_hash="hash_aaa",
        level_0=node_id.split("::")[-1],
        level_1=node_id.split("::")[-1],
    )
    db.upsert_node(db_path, node, discovery_only=False)
    # Patch own_status / link_status directly (compute path bypassed).
    with db._connect(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET own_status = ?, link_status = ? WHERE id = ?",
            (own_status, link_status, node_id),
        )


def _write_doc_with_section(
    db_path: Path,
    doc_id: str,
    section_id: str,
    *,
    title: str = "Doc",
    heading: str = "Section",
) -> None:
    now = _now_iso()
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
        db.upsert_doc_section(
            conn,
            {
                "id": section_id,
                "doc_id": doc_id,
                "heading": heading,
                "level": 2,
                "tags": "",
                "content": "x",
                "desc_hash": hashlib.sha256(b"x").hexdigest()[:16],
                "parent_id": None,
                "depth": 0,
                "position": 0,
                "updated_at": now,
            },
        )


def _add_documents_edge(db_path: Path, section_id: str, code_node_id: str) -> None:
    edge = AxiomEdge(
        id=f"{section_id}::documents::{code_node_id}",
        edge_type="documents",
        from_id=section_id,
        to_id=code_node_id,
    )
    db.upsert_edge(db_path, edge)


# ===========================================================================
# US-1: location_glob filtering, format='ids', pagination
# ===========================================================================


class TestLocationGlobAndIds:
    """drift_query filters by glob, returns IDs ready for mark_clean."""

    def test_location_glob_filters_before_projection(self, git_project: Path) -> None:
        """Glob restricts the slice; format='ids' returns just the matching IDs."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(
            db_path,
            "p::pkg_a.mod::f1",
            own_status="CONTENT_UPDATED",
            location="pkg_a/mod.py",
            level_3_location="pkg_a/mod.py",
        )
        _upsert_node(
            db_path,
            "p::pkg_b.mod::f2",
            own_status="CONTENT_UPDATED",
            location="pkg_b/mod.py",
            level_3_location="pkg_b/mod.py",
        )
        _upsert_node(
            db_path,
            "p::pkg_a.mod::f3",
            link_status="LINKED_STALE",
            location="pkg_a/mod.py",
            level_3_location="pkg_a/mod.py",
        )

        result = axiom_graph_drift_query(
            str(git_project),
            filter="all",
            location_glob="pkg_a/**",
            format="ids",
        )
        assert "p::pkg_a.mod::f1" in result
        assert "p::pkg_a.mod::f3" in result
        assert "p::pkg_b.mod::f2" not in result

    def test_pagination_disjoint_and_complete(self, git_project: Path) -> None:
        """page=0,limit=2 ∪ page=1,limit=2 = unpaginated; slices disjoint."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        for i in range(4):
            _upsert_node(
                db_path,
                f"p::mod::f{i}",
                own_status="CONTENT_UPDATED",
                location="m.py",
                level_3_location="m.py",
            )

        def _ids(**kw: int) -> list[str]:
            out = axiom_graph_drift_query(str(git_project), filter="all", format="ids", **kw)
            # Strip the [N of M drifted nodes] count header.
            return [ln for ln in out.splitlines() if ln and not ln.startswith("[")]

        full = _ids(limit=100)
        page0 = _ids(page=0, limit=2)
        page1 = _ids(page=1, limit=2)
        assert len(page0) == 2
        assert len(page1) == 2
        assert set(page0) | set(page1) == set(full)
        assert set(page0).isdisjoint(set(page1))

    def test_page_out_of_range_distinct_from_no_matches(self, git_project: Path) -> None:
        """Past-end pagination returns 'page out of range', not 'no matches'."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "p::mod::only_one", own_status="CONTENT_UPDATED")
        # page=5 with limit=10 -> offset 50, no rows.
        result = axiom_graph_drift_query(str(git_project), filter="all", format="ids", page=5, limit=10)
        assert result == "(page out of range)"
        # Whereas a glob that matches nothing -> "no matches".
        no_match = axiom_graph_drift_query(
            str(git_project),
            filter="all",
            location_glob="nowhere/**",
            format="ids",
            page=0,
        )
        assert no_match == "(no matches)"


# ===========================================================================
# US-2: group_by='location_prefix' / 'feature', filter parity
# ===========================================================================


class TestGroupBy:
    """group_by axes produce correct buckets and counts."""

    def test_group_by_location_prefix_counts(self, git_project: Path) -> None:
        """location_prefix groups by first 2 path components."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(
            db_path,
            "p::a::f1",
            own_status="CONTENT_UPDATED",
            location="axiom_graph/viz/x.py",
            level_3_location="axiom_graph/viz/x.py",
        )
        _upsert_node(
            db_path,
            "p::a::f2",
            own_status="CONTENT_UPDATED",
            location="axiom_graph/viz/y.py",
            level_3_location="axiom_graph/viz/y.py",
        )
        _upsert_node(
            db_path,
            "p::b::f3",
            own_status="CONTENT_UPDATED",
            location="axiom_graph/index/z.py",
            level_3_location="axiom_graph/index/z.py",
        )

        result = axiom_graph_drift_query(
            str(git_project),
            filter="all",
            group_by="location_prefix",
            format="counts",
        )
        # Expect two buckets: axiom_graph/viz=2, axiom_graph/index=1.
        lines = result.splitlines()
        d = {}
        for ln in lines:
            grp, n = ln.rsplit(maxsplit=1)
            d[grp.strip()] = int(n)
        assert d.get("axiom_graph/viz") == 2
        assert d.get("axiom_graph/index") == 1

    def test_group_by_feature_resolves_via_documents_edge(self, git_project: Path) -> None:
        """feature groups via inbound 'documents' edge to docs.features.X."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        # Three code nodes, all content-stale.
        _upsert_node(db_path, "p::axiom_graph.indexer::f_idx", own_status="CONTENT_UPDATED")
        _upsert_node(db_path, "p::axiom_graph.viz::f_viz", own_status="CONTENT_UPDATED")
        _upsert_node(db_path, "p::lonely::f_undoc", own_status="CONTENT_UPDATED")

        # Two feature docs with sections that document the first two nodes.
        _write_doc_with_section(
            db_path,
            "p::docs.features.indexer.design",
            "p::docs.features.indexer.design::overview",
        )
        _write_doc_with_section(
            db_path,
            "p::docs.features.viz.design",
            "p::docs.features.viz.design::overview",
        )
        _add_documents_edge(
            db_path,
            "p::docs.features.indexer.design::overview",
            "p::axiom_graph.indexer::f_idx",
        )
        _add_documents_edge(
            db_path,
            "p::docs.features.viz.design::overview",
            "p::axiom_graph.viz::f_viz",
        )
        # f_undoc has no inbound documents edge.

        result = axiom_graph_drift_query(
            str(git_project),
            filter="all",
            group_by="feature",
            format="ids",
        )
        # Expect three buckets: indexer, viz, (undocumented).
        assert "[indexer]" in result
        assert "[viz]" in result
        assert "[(undocumented)]" in result
        assert "p::axiom_graph.indexer::f_idx" in result
        assert "p::axiom_graph.viz::f_viz" in result
        assert "p::lonely::f_undoc" in result

    def test_filter_vocab_parity_links(self, git_project: Path) -> None:
        """filter='links' selects LINKED_STALE + BROKEN_LINK only."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "p::a::own_only", own_status="CONTENT_UPDATED")
        _upsert_node(db_path, "p::a::ls", link_status="LINKED_STALE")
        _upsert_node(db_path, "p::a::bl", link_status="BROKEN_LINK")

        result = axiom_graph_drift_query(str(git_project), filter="links", format="ids")
        ids = set(result.splitlines())
        assert "p::a::ls" in ids
        assert "p::a::bl" in ids
        assert "p::a::own_only" not in ids

    # ------------------------------------------------------------------
    # Filter-vocab parity (Reviewer iteration 1):
    # for each value the legacy ``check(filter=F)`` accepted, drift_query
    # must return exactly the set of node-IDs whose persisted column
    # values satisfy F per parse_drift_filter semantics.
    # ------------------------------------------------------------------
    @pytest.mark.parametrize(
        "filter_value",
        [
            None,
            "staleness",
            "links",
            "all",
            "doc_quality",
            "DOC_SECTION_LONG",
            "CONTENT_UPDATED",
            "DESC_UPDATED",
            "NOT_FOUND",
            "LINKED_STALE",
            "BROKEN_LINK",
        ],
    )
    def test_filter_vocab_parity_full_matrix(self, git_project: Path, filter_value: str | None) -> None:
        """drift_query rows for filter F = nodes whose columns satisfy F.

        Seed at least one row of every status kind plus one long
        docjson shadow row, then assert the set returned by
        ``drift_query(filter=F, format='ids')`` equals the set
        predicted by ``parse_drift_filter(F)`` over the same DB.

        Doc-quality filters (``doc_quality`` / ``DOC_SECTION_LONG``)
        select rows where ``subtype='docjson'`` AND
        ``LENGTH(level_2) > DOC_SECTION_LONG_THRESHOLD``.  The matrix
        seeds exactly one such row so the prediction is computable
        from the seeds alone.
        """
        from axiom_graph.db import staleness as st
        from axiom_graph.db.docs import DOC_SECTION_LONG_THRESHOLD
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        # Seed one row of every kind we care about.  Use distinctive IDs
        # so the predicted set comparison is unambiguous.
        seeds = {
            "p::a::cu": ("CONTENT_UPDATED", "VERIFIED"),
            "p::a::du": ("DESC_UPDATED", "VERIFIED"),
            "p::a::nf": ("NOT_FOUND", "VERIFIED"),
            "p::a::ls": ("VERIFIED", "LINKED_STALE"),
            "p::a::bl": ("VERIFIED", "BROKEN_LINK"),
            "p::a::ok": ("VERIFIED", "VERIFIED"),
        }
        for node_id, (own, link) in seeds.items():
            _upsert_node(db_path, node_id, own_status=own, link_status=link)

        # Seed one long docjson shadow row (DOC_SECTION_LONG candidate).
        long_id = "p::docs.spec::long_sec"
        long_doc_id = "p::docs.spec"
        long_content = "x" * (DOC_SECTION_LONG_THRESHOLD + 50)
        _write_doc_with_section(db_path, long_doc_id, long_id)
        # The helper seeds content="x" — overwrite via direct doc_sections
        # update + sync.  Calling index_doc_sections_fts will pick up the
        # change AND create the shadow row in nodes with the long level_2.
        from axiom_graph.db import docs as db_docs_mod

        with db._connect(db_path) as conn:
            conn.execute(
                "UPDATE doc_sections SET content = ? WHERE id = ?",
                (long_content, long_id),
            )
        db_docs_mod.index_doc_sections_fts(db_path)

        # Predicted set per parse_drift_filter semantics.
        show_own, show_link, show_doc_quality = st.parse_drift_filter(filter_value)
        predicted_ids = {nid for nid, (own, link) in seeds.items() if own in show_own or link in show_link}
        if show_doc_quality:
            predicted_ids.add(long_id)

        result = axiom_graph_drift_query(str(git_project), filter=filter_value, format="ids", limit=1000)

        if not predicted_ids:
            assert result == "(no matches)"
            return

        # Drop the [N of M drifted nodes] count header line.
        actual_ids = {ln for ln in result.splitlines() if ln and not ln.startswith("[")}
        assert actual_ids == predicted_ids, (
            f"filter={filter_value!r}: expected {sorted(predicted_ids)} got {sorted(actual_ids)}"
        )

    def test_filter_individual_status(self, git_project: Path) -> None:
        """filter='LINKED_STALE' selects only nodes with that link_status."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "p::a::ls", link_status="LINKED_STALE")
        _upsert_node(db_path, "p::a::bl", link_status="BROKEN_LINK")
        _upsert_node(db_path, "p::a::cu", own_status="CONTENT_UPDATED")

        result = axiom_graph_drift_query(str(git_project), filter="LINKED_STALE", format="ids")
        ids = {ln for ln in result.splitlines() if ln and not ln.startswith("[")}
        assert ids == {"p::a::ls"}

    def test_invalid_filter_raises(self, git_project: Path) -> None:
        """Unknown filter values raise ValueError, not silent fallback."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        with pytest.raises(ValueError):
            axiom_graph_drift_query(str(git_project), filter="bogus_filter_value")

    def test_format_counts_requires_group_by(self, git_project: Path) -> None:
        """format='counts' with group_by=None raises ValueError."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        _upsert_node(git_project / ".axiom_graph" / "graph.db", "p::a::x")
        with pytest.raises(ValueError):
            axiom_graph_drift_query(str(git_project), filter="all", format="counts")

    def test_invalid_group_by_raises(self, git_project: Path) -> None:
        """Unknown group_by axis raises ValueError."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        with pytest.raises(ValueError):
            axiom_graph_drift_query(str(git_project), filter="all", group_by="bogus")

    def test_full_format_includes_via_for_linked_stale(self, git_project: Path) -> None:
        """format='full' surfaces upstream offender IDs for LINKED_STALE rows."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        # Upstream code node that has drifted (CONTENT_UPDATED).
        _upsert_node(db_path, "p::up::offender", own_status="CONTENT_UPDATED")
        # Doc section that documents the offender (so via lookup finds it).
        _write_doc_with_section(
            db_path,
            "p::docs.x",
            "p::docs.x::sec",
        )
        _upsert_node(db_path, "p::docs.x::sec", link_status="LINKED_STALE")
        _add_documents_edge(db_path, "p::docs.x::sec", "p::up::offender")

        result = axiom_graph_drift_query(str(git_project), filter="LINKED_STALE", format="full")
        assert "p::docs.x::sec" in result
        assert "via=p::up::offender" in result


# ===========================================================================
# Frozen-tags filtering (cycle pev-2026-05-17-frozen-tags-staleness-skip)
# ===========================================================================


def _seed_frozen_doc_section(
    db_path: Path,
    doc_id: str,
    section_id: str,
    *,
    tags_json: str,
    link_status: str = "LINKED_STALE",
) -> None:
    """Seed a doc with a frozen-style tags JSON array and a stale section."""
    now = _now_iso()
    with db._connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, doc_id.split("::")[-1], tags_json, f"docs/{doc_id.split('::')[-1]}.json", "h", now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, "
            "desc_hash, parent_id, depth, position, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (section_id, doc_id, "Heading", 2, "", "body", "h2", None, 0, 0, now),
        )
    _upsert_node(db_path, section_id, link_status=link_status, location=f"docs/{doc_id.split('::')[-1]}.json")


def _write_frozen_tags_toml(project_root: Path, tags: list[str]) -> None:
    quoted = ", ".join(f'"{t}"' for t in tags)
    (project_root / "axiom-graph.toml").write_text(f"[axiom_graph.staleness]\nfrozen_tags = [{quoted}]\n")


class TestFrozenTagsFiltering:
    """drift_query honors `frozen_tags` config: excludes frozen rows by default,
    includes them with `[frozen]` marker on include_frozen=True, retains
    BROKEN_LINK on frozen with `[frozen-source]` marker."""

    def test_default_excludes_frozen_rows(self, git_project: Path) -> None:
        """Default `include_frozen=False`: frozen-tag rows do not appear."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _write_frozen_tags_toml(git_project, ["adr"])
        # Frozen ADR section with LINKED_STALE.
        _seed_frozen_doc_section(db_path, "p::docs.adr-1", "p::docs.adr-1::ctx", tags_json='["adr"]')
        # Non-frozen section with LINKED_STALE.
        _write_doc_with_section(db_path, "p::docs.spec", "p::docs.spec::sec")
        _upsert_node(db_path, "p::docs.spec::sec", link_status="LINKED_STALE")

        result = axiom_graph_drift_query(str(git_project), filter="LINKED_STALE", format="ids")
        ids = set(result.splitlines())
        assert "p::docs.spec::sec" in ids
        assert "p::docs.adr-1::ctx" not in ids

    def test_include_frozen_true_includes_with_marker(self, git_project: Path) -> None:
        """include_frozen=True surfaces frozen rows with [frozen] marker on format=full."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _write_frozen_tags_toml(git_project, ["adr"])
        _seed_frozen_doc_section(db_path, "p::docs.adr-2", "p::docs.adr-2::ctx", tags_json='["adr"]')

        result = axiom_graph_drift_query(
            str(git_project),
            filter="LINKED_STALE",
            format="full",
            include_frozen=True,
        )
        assert "p::docs.adr-2::ctx" in result
        assert "[frozen]" in result

    def test_broken_link_on_frozen_keeps_marker(self, git_project: Path) -> None:
        """BROKEN_LINK on a frozen doc surfaces in default output with [frozen-source]."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _write_frozen_tags_toml(git_project, ["adr"])
        _seed_frozen_doc_section(
            db_path,
            "p::docs.adr-3",
            "p::docs.adr-3::ctx",
            tags_json='["adr"]',
            link_status="BROKEN_LINK",
        )

        result = axiom_graph_drift_query(
            str(git_project),
            filter="links",
            format="full",
        )
        # Even though include_frozen=False, BROKEN_LINK on frozen is retained.
        assert "p::docs.adr-3::ctx" in result
        assert "[frozen-source]" in result

    def test_non_frozen_broken_link_has_no_marker(self, git_project: Path) -> None:
        """BROKEN_LINK on a non-frozen doc does NOT receive [frozen-source]."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _write_frozen_tags_toml(git_project, ["adr"])
        # Non-frozen doc with BROKEN_LINK.
        _seed_frozen_doc_section(
            db_path,
            "p::docs.guide",
            "p::docs.guide::sec",
            tags_json='["consumer"]',
            link_status="BROKEN_LINK",
        )

        result = axiom_graph_drift_query(
            str(git_project),
            filter="links",
            format="full",
        )
        assert "p::docs.guide::sec" in result
        assert "[frozen-source]" not in result
        assert "[frozen]" not in result

    def test_marker_not_in_ids_format(self, git_project: Path) -> None:
        """Markers never appear on format='ids'."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _write_frozen_tags_toml(git_project, ["adr"])
        _seed_frozen_doc_section(
            db_path,
            "p::docs.adr-4",
            "p::docs.adr-4::ctx",
            tags_json='["adr"]',
            link_status="BROKEN_LINK",
        )

        result = axiom_graph_drift_query(
            str(git_project),
            filter="links",
            format="ids",
            include_frozen=True,
        )
        # Just the bare ID — no markers in ids format.
        assert "p::docs.adr-4::ctx" in result
        assert "[frozen]" not in result
        assert "[frozen-source]" not in result


# ===========================================================================
# Performance regression: batched _via_for_node lookup (Reviewer iteration 1).
# ===========================================================================


class TestViaBatching:
    """``_via_for_node`` per-row caused N+1 ``_connect`` calls; fix is batched.

    The contract: a single ``query_drift_rows`` (or any of the
    ``query_drift_full_by_*`` helpers) must open at most O(1) DB
    connections regardless of how many LINKED_STALE rows are in the page.
    """

    def test_query_drift_rows_uses_o1_connections_for_n_linked_stale(self, git_project: Path) -> None:
        """N LINKED_STALE rows must NOT translate to N+1 ``_connect`` calls."""
        from unittest.mock import patch

        from axiom_graph.db import staleness as st

        db_path = git_project / ".axiom_graph" / "graph.db"

        # Seed an offender plus N LINKED_STALE rows that all point to it
        # via inbound 'documents' edges.  Each LINKED_STALE row, in the
        # unbatched implementation, triggered its own _via_for_node ->
        # _connect.  After the fix, the page resolves vias in one SELECT.
        N = 25
        _upsert_node(db_path, "p::up::offender", own_status="CONTENT_UPDATED")
        _write_doc_with_section(db_path, "p::docs.x", "p::docs.x::ignored")
        for i in range(N):
            sec_id = f"p::docs.x::sec_{i:02d}"
            _upsert_node(db_path, sec_id, link_status="LINKED_STALE")
            _add_documents_edge(db_path, sec_id, "p::up::offender")

        # Patch _connect *inside the staleness module* so we count the
        # connections opened by the drift-query code path specifically.
        # _upsert_node above used db.upsert_node which goes through the
        # legacy db path -- those calls don't increment our counter.
        original_connect = st._connect
        call_count = {"n": 0}

        def counting_connect(path):
            call_count["n"] += 1
            return original_connect(path)

        with patch.object(st, "_connect", counting_connect):
            rows = st.query_drift_rows(db_path, filter="LINKED_STALE", page=0, limit=1000)

        # Sanity: we got the rows we expected.
        linked_stale_rows = [r for r in rows if r["link_status"] == "LINKED_STALE"]
        assert len(linked_stale_rows) == N
        # Every LINKED_STALE row must surface the offender via.
        for r in linked_stale_rows:
            assert r["via"] == ["p::up::offender"], (
                f"row {r['id']} via={r['via']} -- batched lookup must preserve unbatched semantics"
            )

        # The fix: one connection for the row SELECT + at most one more
        # for the batched via lookup.  Accept up to 3 to leave a small
        # margin for incidental future helpers, but the unbatched code
        # would have produced N+1 = 26 here, so the assertion still
        # catches a regression cleanly.
        assert call_count["n"] <= 3, (
            f"query_drift_rows opened {call_count['n']} connections for "
            f"{N} LINKED_STALE rows -- expected O(1).  Did the batched "
            "_via_for_nodes_batch helper get unwired?"
        )

    def test_query_drift_full_by_status_uses_o1_connections(self, git_project: Path) -> None:
        """The grouped ``full`` helper also batches via lookups."""
        from unittest.mock import patch

        from axiom_graph.db import staleness as st

        db_path = git_project / ".axiom_graph" / "graph.db"
        N = 15
        _upsert_node(db_path, "p::up::offender", own_status="CONTENT_UPDATED")
        _write_doc_with_section(db_path, "p::docs.x", "p::docs.x::ignored")
        for i in range(N):
            sec_id = f"p::docs.x::sec_{i:02d}"
            _upsert_node(db_path, sec_id, link_status="LINKED_STALE")
            _add_documents_edge(db_path, sec_id, "p::up::offender")

        original_connect = st._connect
        call_count = {"n": 0}

        def counting_connect(path):
            call_count["n"] += 1
            return original_connect(path)

        with patch.object(st, "_connect", counting_connect):
            buckets = st.query_drift_full_by_status(db_path, filter="LINKED_STALE")

        # All N rows should land in one bucket VERIFIED/LINKED_STALE.
        flat = [r for b in buckets for r in b["rows"]]
        assert len(flat) == N
        for r in flat:
            assert r["via"] == ["p::up::offender"]

        # Same O(1) ceiling: fetch + via batch = 2 connections.
        assert call_count["n"] <= 3, (
            f"query_drift_full_by_status opened {call_count['n']} "
            f"connections for {N} LINKED_STALE rows -- expected O(1)."
        )


# ===========================================================================
# format='full' column header (audit-agent improvements 2026-05).
# ===========================================================================


HEADER_LINE = "# node_id  status_pair (own/link)  location  via"


class TestFullFormatHeader:
    """``format='full'`` prefixes a self-describing column header line.

    The header makes ``status_pair`` (own/link) ordering self-evident
    so audit agents don't have to re-read the docstring to interpret
    rows.  ``ids`` and ``counts`` formats are unaffected -- their
    output shapes are already self-evident.
    """

    def test_flat_full_first_line_is_header(self, git_project: Path) -> None:
        """Flat ``format='full'``: count header, then the column header."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "p::a::cu", own_status="CONTENT_UPDATED")

        result = axiom_graph_drift_query(str(git_project), filter="all", format="full")
        lines = result.splitlines()
        assert lines[0] == "[1 of 1 drifted nodes]"
        assert lines[1] == HEADER_LINE
        # Data row still present and unchanged in shape.
        assert any("p::a::cu  CONTENT_UPDATED/VERIFIED" in ln for ln in lines[2:])

    def test_grouped_full_first_line_is_header(self, git_project: Path) -> None:
        """Grouped ``format='full'``: count header, then the column header once."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "p::a::cu", own_status="CONTENT_UPDATED")
        _upsert_node(db_path, "p::a::ls", link_status="LINKED_STALE")

        result = axiom_graph_drift_query(str(git_project), filter="all", group_by="status", format="full")
        lines = result.splitlines()
        assert lines[0] == "[2 of 2 drifted nodes]"
        assert lines[1] == HEADER_LINE
        # Column header appears exactly once (not per bucket).
        assert sum(1 for ln in lines if ln == HEADER_LINE) == 1
        # Bucket markers still emitted after the headers.
        assert any(ln.startswith("[") and ln.endswith("]") for ln in lines[2:])

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"format": "ids"},
            {"format": "ids", "group_by": "status"},
            {"format": "counts", "group_by": "status"},
        ],
    )
    def test_non_full_formats_do_not_emit_header(self, git_project: Path, kwargs: dict) -> None:
        """``format='ids'`` and ``format='counts'`` never emit the header."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "p::a::cu", own_status="CONTENT_UPDATED")

        result = axiom_graph_drift_query(str(git_project), filter="all", **kwargs)
        assert HEADER_LINE not in result.splitlines()

    def test_no_matches_does_not_emit_header(self, git_project: Path) -> None:
        """Empty result still returns ``(no matches)`` -- no header prefix."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        # No drifted nodes seeded -- everything is VERIFIED/VERIFIED.
        result = axiom_graph_drift_query(str(git_project), filter="staleness", format="full")
        assert result == "(no matches)"


# ===========================================================================
# Grouped pagination + conditional default (drift-query-grouped-pagination
# instance, 2026-06-06).  Grouped paths previously ignored page/limit and
# defaulted to format='full', dumping the whole inventory.  Fix: grouped
# default is 'counts'; grouped full/ids honor page/limit; paginated output
# carries a [N of M drifted nodes] header like the sibling tools.
# ===========================================================================


def _grouped_member_ids(result: str) -> list[str]:
    """Extract member node IDs from grouped ids/full output.

    Bucket members are the indented (two-space) lines; the first
    whitespace-delimited token is the node ID for both ``ids`` (``  id``)
    and ``full`` (``  id  own/link  loc``) shapes.  Bucket markers
    (``[group]``) and the ``[N of M ...]`` header start at column 0, so
    they are excluded.
    """
    return [ln.split()[0] for ln in result.splitlines() if ln.startswith("  ")]


class TestGroupedDefaultIsCounts:
    """A grouped call with no explicit format returns the counts distribution."""

    @pytest.mark.parametrize("axis", ["status", "location_prefix", "feature"])
    def test_grouped_default_returns_counts_not_full(self, git_project: Path, axis: str) -> None:
        """``group_by=<axis>`` with no ``format`` yields ``group  count`` lines."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        for i in range(3):
            _upsert_node(
                db_path,
                f"p::axiom_graph.mod::f{i}",
                own_status="CONTENT_UPDATED",
                location="axiom_graph/mod/m.py",
                level_3_location="axiom_graph/mod/m.py",
            )

        result = axiom_graph_drift_query(str(git_project), filter="all", group_by=axis)
        lines = result.splitlines()
        # Counts shape: every line is "<group>  <int>"; no bucket markers,
        # no column header, no full-row "own/link" pairs.
        assert HEADER_LINE not in lines
        assert not any(ln.startswith("[") for ln in lines)
        for ln in lines:
            _, n = ln.rsplit(maxsplit=1)
            assert n.isdigit()

    def test_flat_default_still_full(self, git_project: Path) -> None:
        """No regression: the flat (ungrouped) default stays ``full``."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "p::a::cu", own_status="CONTENT_UPDATED")

        result = axiom_graph_drift_query(str(git_project), filter="all")
        assert HEADER_LINE in result.splitlines()
        assert "p::a::cu  CONTENT_UPDATED/VERIFIED" in result


class TestGroupedPagination:
    """Grouped full/ids honor page/limit; counts stays unpaginated."""

    def _seed_one_group(self, db_path: Path, n: int) -> None:
        # All same status -> a single 'status' bucket so pagination slices
        # within one group (proves page/limit reach the grouped path).
        for i in range(n):
            _upsert_node(
                db_path,
                f"p::mod::f{i:02d}",
                own_status="CONTENT_UPDATED",
                location="m.py",
                level_3_location="m.py",
            )

    def test_grouped_ids_pagination_disjoint_and_complete(self, git_project: Path) -> None:
        """page0 ∪ page1 = unpaginated set; slices disjoint (group_by ids)."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        self._seed_one_group(db_path, 4)

        full = _grouped_member_ids(
            axiom_graph_drift_query(str(git_project), filter="all", group_by="status", format="ids", limit=100)
        )
        page0 = _grouped_member_ids(
            axiom_graph_drift_query(str(git_project), filter="all", group_by="status", format="ids", page=0, limit=2)
        )
        page1 = _grouped_member_ids(
            axiom_graph_drift_query(str(git_project), filter="all", group_by="status", format="ids", page=1, limit=2)
        )
        assert len(full) == 4
        assert len(page0) == 2
        assert len(page1) == 2
        assert set(page0) | set(page1) == set(full)
        assert set(page0).isdisjoint(set(page1))

    def test_grouped_full_pagination_disjoint_and_complete(self, git_project: Path) -> None:
        """page0 ∪ page1 = unpaginated set; slices disjoint (group_by full)."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        self._seed_one_group(db_path, 4)

        full = _grouped_member_ids(
            axiom_graph_drift_query(str(git_project), filter="all", group_by="status", format="full", limit=100)
        )
        page0 = _grouped_member_ids(
            axiom_graph_drift_query(str(git_project), filter="all", group_by="status", format="full", page=0, limit=2)
        )
        page1 = _grouped_member_ids(
            axiom_graph_drift_query(str(git_project), filter="all", group_by="status", format="full", page=1, limit=2)
        )
        assert len(full) == 4
        assert len(page0) == 2
        assert len(page1) == 2
        assert set(page0) | set(page1) == set(full)
        assert set(page0).isdisjoint(set(page1))

    def test_grouped_counts_unaffected_by_limit(self, git_project: Path) -> None:
        """counts is a bounded distribution; limit does not truncate it."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        self._seed_one_group(db_path, 4)

        small = axiom_graph_drift_query(str(git_project), filter="all", group_by="status", format="counts", limit=1)
        big = axiom_graph_drift_query(str(git_project), filter="all", group_by="status", format="counts", limit=100)
        assert small == big
        # The single bucket reports the full count of 4 regardless of limit.
        assert small.strip().endswith(" 4")


class TestPaginationHeader:
    """Paginated full/ids output carries a [N of M drifted nodes] header."""

    def test_grouped_full_header_and_next_page_hint(self, git_project: Path) -> None:
        """Grouped full page 0 shows the count header + next-page hint."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        for i in range(4):
            _upsert_node(db_path, f"p::mod::f{i:02d}", own_status="CONTENT_UPDATED")

        result = axiom_graph_drift_query(
            str(git_project), filter="all", group_by="status", format="full", page=0, limit=2
        )
        lines = result.splitlines()
        assert lines[0] == "[2 of 4 drifted nodes]  (pass page=1 for next page)"
        # Column header follows the count header, exactly once.
        assert lines[1] == HEADER_LINE
        assert sum(1 for ln in lines if ln == HEADER_LINE) == 1

    def test_flat_full_header_present(self, git_project: Path) -> None:
        """Flat full also gains the count header (consistency with siblings)."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        for i in range(3):
            _upsert_node(db_path, f"p::mod::f{i:02d}", own_status="CONTENT_UPDATED")

        result = axiom_graph_drift_query(str(git_project), filter="all", format="full", limit=100)
        lines = result.splitlines()
        assert lines[0] == "[3 of 3 drifted nodes]"
        assert lines[1] == HEADER_LINE

    def test_last_page_has_no_next_page_hint(self, git_project: Path) -> None:
        """The final page omits the (pass page=...) hint."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        for i in range(4):
            _upsert_node(db_path, f"p::mod::f{i:02d}", own_status="CONTENT_UPDATED")

        result = axiom_graph_drift_query(
            str(git_project), filter="all", group_by="status", format="full", page=1, limit=2
        )
        assert result.splitlines()[0] == "[2 of 4 drifted nodes]"
        assert "pass page=" not in result


class TestGroupedPageOutOfRange:
    """Grouped out-of-range pagination mirrors the flat path."""

    def test_grouped_page_out_of_range(self, git_project: Path) -> None:
        """Past-end grouped page returns 'page out of range', not 'no matches'."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "p::mod::only_one", own_status="CONTENT_UPDATED")

        result = axiom_graph_drift_query(
            str(git_project), filter="all", group_by="status", format="ids", page=5, limit=10
        )
        assert result == "(page out of range)"

    def test_grouped_no_matches_distinct_from_out_of_range(self, git_project: Path) -> None:
        """A glob matching nothing returns 'no matches' even when grouped."""
        from axiom_graph.mcp_server import axiom_graph_drift_query

        db_path = git_project / ".axiom_graph" / "graph.db"
        _upsert_node(db_path, "p::mod::only_one", own_status="CONTENT_UPDATED")

        result = axiom_graph_drift_query(
            str(git_project),
            filter="all",
            location_glob="nowhere/**",
            group_by="status",
            format="ids",
        )
        assert result == "(no matches)"
