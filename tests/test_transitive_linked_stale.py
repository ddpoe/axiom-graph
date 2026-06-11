"""Tests for transitive LINKED_STALE propagation (ADR-016).

Covers: direct staleness unchanged (no config), single-hop transitive,
multi-hop chain, cycle detection, tag gating, multiple causes, no false
positives, via in CLI text output, via in JSON output.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


from axiom_graph.config import AxiomGraphConfig
from axiom_graph.index import db
from axiom_graph.index.staleness import (
    _get_linked_stale_ids,
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


def _seed_doc_graph(db_path: Path, *, tags: str = '["consumer"]') -> None:
    """Seed a doc graph: consumer_doc -> dev_spec -> code_fn.

    consumer_doc is tagged with *tags*. dev_spec has a 'documents' edge
    to code_fn. consumer_doc has a 'documents' edge to dev_spec.
    code_fn has a history row making dev_spec stale.
    """
    now = db._now_utc()

    # --- nodes ---
    code_fn = _node("proj::mod.fn", location="src/mod.py")
    dev_spec = _node(
        "proj::docs.spec::overview",
        subtype="docjson",
        code_hash="spechash",
        location="docs/spec.json",
    )
    consumer = _node(
        "proj::docs.guide::intro",
        subtype="docjson",
        code_hash="guidehash",
        location="docs/guide.json",
    )

    with db._connect(db_path) as conn:
        for n in (code_fn, dev_spec, consumer):
            db.upsert_node_conn(conn, n, now)

        # --- doc_sections + docs ---
        conn.execute(
            "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("proj::docs.spec", "Spec", "[]", "docs/spec.json", None, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, desc_hash, parent_id, depth, position, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "proj::docs.spec::overview",
                "proj::docs.spec",
                "Overview",
                2,
                None,
                "spec content",
                None,
                None,
                0,
                0,
                now,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("proj::docs.guide", "Guide", tags, "docs/guide.json", None, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, desc_hash, parent_id, depth, position, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("proj::docs.guide::intro", "proj::docs.guide", "Intro", 2, None, "guide content", None, None, 0, 0, now),
        )

        # --- edges ---
        # dev_spec -> code_fn (documents)
        db.upsert_edge_conn(
            conn,
            _edge("proj::docs.spec::overview", "documents", "proj::mod.fn"),
        )
        # consumer -> dev_spec (documents, doc-to-doc)
        db.upsert_edge_conn(
            conn,
            _edge("proj::docs.guide::intro", "documents", "proj::docs.spec::overview"),
        )

        # --- history: make code_fn look changed AFTER dev_spec was updated ---
        # This makes dev_spec LINKED_STALE via direct doc-to-code.
        time.sleep(0.05)
        later = db._now_utc()
        conn.execute(
            "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("proj::mod.fn", later, "CONTENT_ONLY", "abc123", "{}", 0),
        )


# ---------------------------------------------------------------------------
# Test: Direct staleness is unchanged without config
# ---------------------------------------------------------------------------


class TestDirectStalenessUnchanged:
    """Without transitive_tags, only direct doc-to-code signals fire."""

    def test_direct_staleness_no_transitive(self, db_path: Path):
        """dev_spec is LINKED_STALE but consumer_doc is NOT, since no tags configured."""
        _seed_doc_graph(db_path, tags='["consumer"]')

        result = _get_linked_stale_ids(db_path, transitive_tags=None)

        assert "proj::docs.spec::overview" in result
        assert "proj::docs.guide::intro" not in result


# ---------------------------------------------------------------------------
# Test: Tag gating
# ---------------------------------------------------------------------------


class TestTagGating:
    """Transitive propagation only fires for docs matching configured tags."""

    def test_matching_tag_enables_propagation(self, db_path: Path):
        """Consumer doc with matching tag gets LINKED_STALE transitively."""
        _seed_doc_graph(db_path, tags='["consumer"]')

        result = _get_linked_stale_ids(db_path, transitive_tags=["consumer"])

        assert "proj::docs.spec::overview" in result
        assert "proj::docs.guide::intro" in result

    def test_non_matching_tag_blocks_propagation(self, db_path: Path):
        """Consumer doc with non-matching tag does NOT get LINKED_STALE."""
        _seed_doc_graph(db_path, tags='["internal"]')

        result = _get_linked_stale_ids(db_path, transitive_tags=["consumer"])

        assert "proj::docs.spec::overview" in result
        assert "proj::docs.guide::intro" not in result

    def test_empty_tags_blocks_propagation(self, db_path: Path):
        """Empty transitive_tags list means no propagation."""
        _seed_doc_graph(db_path, tags='["consumer"]')

        result = _get_linked_stale_ids(db_path, transitive_tags=[])

        assert "proj::docs.spec::overview" in result
        assert "proj::docs.guide::intro" not in result


# ---------------------------------------------------------------------------
# Test: Single-hop transitive propagation
# ---------------------------------------------------------------------------


class TestSingleHopTransitive:
    """A consumer doc linking to a stale dev spec becomes LINKED_STALE."""

    def test_via_contains_intermediate(self, db_path: Path):
        """The via list for the consumer doc should contain the dev spec section."""
        _seed_doc_graph(db_path, tags='["consumer"]')

        result = _get_linked_stale_ids(db_path, transitive_tags=["consumer"])

        assert result["proj::docs.guide::intro"] == ["proj::docs.spec::overview"]


# ---------------------------------------------------------------------------
# Test: Cycle detection
# ---------------------------------------------------------------------------


class TestCycleDetection:
    """A->B->A cycle does not cause infinite loops."""

    def test_cycle_terminates(self, db_path: Path):
        """Two docs that reference each other, both LINKED_STALE, terminate correctly."""
        now = db._now_utc()

        code_fn = _node("proj::mod.fn", location="src/mod.py")
        doc_a = _node("proj::docs.a::sec", subtype="docjson", code_hash="ah", location="docs/a.json")
        doc_b = _node("proj::docs.b::sec", subtype="docjson", code_hash="bh", location="docs/b.json")

        with db._connect(db_path) as conn:
            for n in (code_fn, doc_a, doc_b):
                db.upsert_node_conn(conn, n, now)

            # docs table entries
            conn.execute(
                "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("proj::docs.a", "Doc A", '["consumer"]', "docs/a.json", None, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, desc_hash, parent_id, depth, position, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("proj::docs.a::sec", "proj::docs.a", "Sec A", 2, None, "a", None, None, 0, 0, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("proj::docs.b", "Doc B", '["consumer"]', "docs/b.json", None, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, desc_hash, parent_id, depth, position, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("proj::docs.b::sec", "proj::docs.b", "Sec B", 2, None, "b", None, None, 0, 0, now),
            )

            # doc_a -> code_fn (direct)
            db.upsert_edge_conn(conn, _edge("proj::docs.a::sec", "documents", "proj::mod.fn"))
            # doc_b -> doc_a (doc-to-doc)
            db.upsert_edge_conn(conn, _edge("proj::docs.b::sec", "documents", "proj::docs.a::sec"))
            # doc_a -> doc_b (cycle!)
            db.upsert_edge_conn(conn, _edge("proj::docs.a::sec", "documents", "proj::docs.b::sec"))

            # Make code_fn stale
            time.sleep(0.05)
            later = db._now_utc()
            conn.execute(
                "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("proj::mod.fn", later, "CONTENT_ONLY", "abc", "{}", 0),
            )

        result = _get_linked_stale_ids(db_path, transitive_tags=["consumer"])

        # Both should be stale (no infinite loop)
        assert "proj::docs.a::sec" in result
        assert "proj::docs.b::sec" in result


# ---------------------------------------------------------------------------
# Test: Multiple causes
# ---------------------------------------------------------------------------


class TestMultipleCauses:
    """A consumer doc linking to two stale specs gets both in via."""

    def test_multiple_via_entries(self, db_path: Path):
        """Consumer linking to two stale dev specs has both in via list."""
        now = db._now_utc()

        code_fn1 = _node("proj::mod.fn1", location="src/mod.py", code_hash="h1")
        code_fn2 = _node("proj::mod.fn2", location="src/mod.py", code_hash="h2")
        spec1 = _node("proj::docs.spec1::sec", subtype="docjson", code_hash="s1h", location="docs/spec1.json")
        spec2 = _node("proj::docs.spec2::sec", subtype="docjson", code_hash="s2h", location="docs/spec2.json")
        consumer = _node("proj::docs.guide::sec", subtype="docjson", code_hash="gh", location="docs/guide.json")

        with db._connect(db_path) as conn:
            for n in (code_fn1, code_fn2, spec1, spec2, consumer):
                db.upsert_node_conn(conn, n, now)

            # docs + sections
            for doc_id, title, fp in [
                ("proj::docs.spec1", "Spec1", "docs/spec1.json"),
                ("proj::docs.spec2", "Spec2", "docs/spec2.json"),
            ]:
                conn.execute(
                    "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (doc_id, title, "[]", fp, None, now),
                )
            for sec_id, doc_id in [
                ("proj::docs.spec1::sec", "proj::docs.spec1"),
                ("proj::docs.spec2::sec", "proj::docs.spec2"),
            ]:
                conn.execute(
                    "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, desc_hash, parent_id, depth, position, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sec_id, doc_id, "Sec", 2, None, "x", None, None, 0, 0, now),
                )

            conn.execute(
                "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("proj::docs.guide", "Guide", '["consumer"]', "docs/guide.json", None, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, desc_hash, parent_id, depth, position, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("proj::docs.guide::sec", "proj::docs.guide", "Sec", 2, None, "guide", None, None, 0, 0, now),
            )

            # edges: spec1 -> code_fn1, spec2 -> code_fn2
            db.upsert_edge_conn(conn, _edge("proj::docs.spec1::sec", "documents", "proj::mod.fn1"))
            db.upsert_edge_conn(conn, _edge("proj::docs.spec2::sec", "documents", "proj::mod.fn2"))
            # consumer -> spec1, consumer -> spec2
            db.upsert_edge_conn(conn, _edge("proj::docs.guide::sec", "documents", "proj::docs.spec1::sec"))
            db.upsert_edge_conn(conn, _edge("proj::docs.guide::sec", "documents", "proj::docs.spec2::sec"))

            # Make both code functions stale
            time.sleep(0.05)
            later = db._now_utc()
            for fn_id in ("proj::mod.fn1", "proj::mod.fn2"):
                conn.execute(
                    "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (fn_id, later, "CONTENT_ONLY", "abc", "{}", 0),
                )

        result = _get_linked_stale_ids(db_path, transitive_tags=["consumer"])

        via = result["proj::docs.guide::sec"]
        assert "proj::docs.spec1::sec" in via
        assert "proj::docs.spec2::sec" in via
        assert len(via) == 2


# ---------------------------------------------------------------------------
# Test: No false positives
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    """Docs not linked to stale specs do not become LINKED_STALE."""

    def test_unlinked_doc_stays_verified(self, db_path: Path):
        """A consumer doc that links to a VERIFIED spec stays clean."""
        now = db._now_utc()

        code_fn = _node("proj::mod.fn", location="src/mod.py")
        spec = _node("proj::docs.spec::sec", subtype="docjson", code_hash="sh", location="docs/spec.json")
        consumer = _node("proj::docs.guide::sec", subtype="docjson", code_hash="gh", location="docs/guide.json")

        with db._connect(db_path) as conn:
            for n in (code_fn, spec, consumer):
                db.upsert_node_conn(conn, n, now)

            conn.execute(
                "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("proj::docs.spec", "Spec", "[]", "docs/spec.json", None, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, desc_hash, parent_id, depth, position, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("proj::docs.spec::sec", "proj::docs.spec", "Sec", 2, None, "x", None, None, 0, 0, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("proj::docs.guide", "Guide", '["consumer"]', "docs/guide.json", None, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, desc_hash, parent_id, depth, position, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("proj::docs.guide::sec", "proj::docs.guide", "Sec", 2, None, "guide", None, None, 0, 0, now),
            )

            # spec -> code (documents), consumer -> spec (documents)
            db.upsert_edge_conn(conn, _edge("proj::docs.spec::sec", "documents", "proj::mod.fn"))
            db.upsert_edge_conn(conn, _edge("proj::docs.guide::sec", "documents", "proj::docs.spec::sec"))

            # NO history row making code stale — everything is clean.

        result = _get_linked_stale_ids(db_path, transitive_tags=["consumer"])

        # Neither should be stale
        assert "proj::docs.spec::sec" not in result
        assert "proj::docs.guide::sec" not in result


# ---------------------------------------------------------------------------
# Test: Via in CLI output (text and JSON)
# ---------------------------------------------------------------------------


class TestViaInCliOutput:
    """Via hints appear in axiom-graph check output."""

    def test_via_in_text_output(self, db_path: Path, mini_project: Path, capsys):
        """Text output shows 'via <node_id>' suffix on LINKED_STALE rows."""
        from click.testing import CliRunner
        from axiom_graph.cli import cmd_check

        _seed_doc_graph(db_path, tags='["consumer"]')

        # Write dummy files so compute_staleness doesn't mark them NOT_FOUND
        (mini_project / "src").mkdir(exist_ok=True)
        (mini_project / "src" / "mod.py").write_text("def fn(): pass\n")
        (mini_project / "docs").mkdir(exist_ok=True)
        (mini_project / "docs" / "spec.json").write_text('{"sections": []}')
        (mini_project / "docs" / "guide.json").write_text('{"sections": []}')

        # Write axiom-graph.toml to enable transitive propagation
        (mini_project / "axiom-graph.toml").write_text('[axiom_graph.staleness]\ntransitive_tags = ["consumer"]\n')

        runner = CliRunner()
        result = runner.invoke(cmd_check, [str(mini_project), "--all"])

        # The consumer doc should show 'via proj::docs.spec::overview'
        assert "via proj::docs.spec::overview" in result.output

    def test_via_in_json_output(self, db_path: Path, mini_project: Path):
        """JSON output includes linked_via and linked_via_count for LINKED_STALE nodes."""
        from click.testing import CliRunner
        from axiom_graph.cli import cmd_check

        _seed_doc_graph(db_path, tags='["consumer"]')

        (mini_project / "src").mkdir(exist_ok=True)
        (mini_project / "src" / "mod.py").write_text("def fn(): pass\n")
        (mini_project / "docs").mkdir(exist_ok=True)
        (mini_project / "docs" / "spec.json").write_text('{"sections": []}')
        (mini_project / "docs" / "guide.json").write_text('{"sections": []}')

        (mini_project / "axiom-graph.toml").write_text('[axiom_graph.staleness]\ntransitive_tags = ["consumer"]\n')

        runner = CliRunner()
        result = runner.invoke(cmd_check, [str(mini_project), "--format", "json"])

        data = json.loads(result.output)
        consumer_entry = data["statuses"].get("proj::docs.guide::intro", {})
        assert consumer_entry.get("link_status") == "LINKED_STALE"
        assert "linked_via" in consumer_entry
        assert "proj::docs.spec::overview" in consumer_entry["linked_via"]
        assert consumer_entry["linked_via_count"] >= 1


# ---------------------------------------------------------------------------
# Test: StalenessConfig
# ---------------------------------------------------------------------------


class TestStalenessConfig:
    """Config loads transitive_tags correctly."""

    def test_default_config_has_empty_tags(self):
        """Default StalenessConfig has empty transitive_tags."""
        cfg = AxiomGraphConfig()
        assert cfg.staleness.transitive_tags == []

    def test_load_from_toml(self, tmp_path: Path):
        """AxiomGraphConfig.load reads [axiom_graph.staleness] transitive_tags."""
        (tmp_path / "axiom-graph.toml").write_text(
            '[axiom_graph.staleness]\ntransitive_tags = ["consumer", "public"]\n'
        )
        cfg = AxiomGraphConfig.load(tmp_path)
        assert cfg.staleness.transitive_tags == ["consumer", "public"]

    def test_default_config_has_empty_frozen_tags(self):
        """Default StalenessConfig has empty frozen_tags."""
        cfg = AxiomGraphConfig()
        assert cfg.staleness.frozen_tags == []

    def test_load_frozen_tags_from_toml(self, tmp_path: Path):
        """AxiomGraphConfig.load reads [axiom_graph.staleness] frozen_tags."""
        (tmp_path / "axiom-graph.toml").write_text('[axiom_graph.staleness]\nfrozen_tags = ["adr", "plan"]\n')
        cfg = AxiomGraphConfig.load(tmp_path)
        assert cfg.staleness.frozen_tags == ["adr", "plan"]

    def test_frozen_tags_absent_from_toml_defaults_to_empty(self, tmp_path: Path):
        """Missing [staleness] frozen_tags key yields empty list."""
        (tmp_path / "axiom-graph.toml").write_text('[axiom_graph.staleness]\ntransitive_tags = ["consumer"]\n')
        cfg = AxiomGraphConfig.load(tmp_path)
        assert cfg.staleness.frozen_tags == []


# ---------------------------------------------------------------------------
# Test: Frozen-tags propagation skip
# ---------------------------------------------------------------------------


class TestFrozenTagsPropagation:
    """frozen_tags skips LINKED_STALE at Pass 1 (doc-to-code) and Pass 3
    (doc-to-doc).  Sections under a frozen-tagged doc never receive
    LINKED_STALE signal.
    """

    def test_pass3_transitive_skip_via_transitive_tagged_chain(self, db_path: Path):
        """A frozen-tagged consumer doc does NOT receive Pass 3 propagation
        when the same tag is also used for transitive opt-in.

        Distinct from ``test_pass3_skip_for_frozen_doc`` (which uses
        separate ``transitive_tags=["consumer"]`` and
        ``frozen_tags=["adr"]``): here a single tag drives BOTH
        transitive enable AND freeze, exercising the path where Pass 3
        sees ``src_id`` in ``frozen_section_ids`` and short-circuits.
        """
        # Seed with tags='["adr"]' so the consumer (guide) doc carries
        # the frozen tag and is the transitive-tag target.
        _seed_doc_graph(db_path, tags='["adr"]')

        # Baseline check: WITHOUT frozen_tags the dev_spec IS stale.
        baseline = _get_linked_stale_ids(db_path, transitive_tags=None, frozen_tags=None)
        assert "proj::docs.spec::overview" in baseline

        # Pass 3 with transitive_tags=["adr"] would normally propagate
        # guide LINKED_STALE; with frozen_tags=["adr"] it must NOT.
        result = _get_linked_stale_ids(db_path, transitive_tags=["adr"], frozen_tags=["adr"])
        assert "proj::docs.guide::intro" not in result

    def test_pass1_skip_when_section_doc_tagged_frozen(self, db_path: Path):
        """A doc-to-code linked section under a frozen-tagged doc never enters Pass 1."""
        now = db._now_utc()

        code_fn = _node("proj::mod.fn", location="src/mod.py")
        adr_section = _node(
            "proj::docs.adr-001::ctx",
            subtype="docjson",
            code_hash="adrhash",
            location="docs/adr-001.json",
        )

        with db._connect(db_path) as conn:
            db.upsert_node_conn(conn, code_fn, now)
            db.upsert_node_conn(conn, adr_section, now)
            # ADR doc carries the frozen "adr" tag.
            conn.execute(
                "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("proj::docs.adr-001", "ADR-001", '["adr"]', "docs/adr-001.json", None, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, "
                "desc_hash, parent_id, depth, position, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "proj::docs.adr-001::ctx",
                    "proj::docs.adr-001",
                    "Context",
                    2,
                    None,
                    "ctx body",
                    None,
                    None,
                    0,
                    0,
                    now,
                ),
            )
            db.upsert_edge_conn(conn, _edge("proj::docs.adr-001::ctx", "documents", "proj::mod.fn"))
            # Make code stale.
            time.sleep(0.05)
            later = db._now_utc()
            conn.execute(
                "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("proj::mod.fn", later, "CONTENT_ONLY", "abc", "{}", 0),
            )

        # Without frozen_tags — ADR section IS stale (baseline).
        baseline = _get_linked_stale_ids(db_path, transitive_tags=None, frozen_tags=None)
        assert "proj::docs.adr-001::ctx" in baseline

        # With frozen_tags=["adr"] — ADR section is skipped.
        result = _get_linked_stale_ids(db_path, transitive_tags=None, frozen_tags=["adr"])
        assert "proj::docs.adr-001::ctx" not in result

    def test_pass3_skip_for_frozen_doc(self, db_path: Path):
        """A frozen-tagged consumer doc does NOT receive transitive propagation."""
        # guide doc is tagged consumer AND adr (frozen).  Without frozen_tags,
        # guide goes LINKED_STALE via transitive.  With frozen_tags=["adr"],
        # guide is skipped at Pass 3.
        _seed_doc_graph(db_path, tags='["consumer", "adr"]')

        # Baseline: consumer transitive propagation makes guide LINKED_STALE.
        baseline = _get_linked_stale_ids(db_path, transitive_tags=["consumer"], frozen_tags=None)
        assert "proj::docs.guide::intro" in baseline

        # With frozen_tags=["adr"]: guide is now frozen, must not propagate.
        result = _get_linked_stale_ids(db_path, transitive_tags=["consumer"], frozen_tags=["adr"])
        assert "proj::docs.spec::overview" in result  # spec still stale at Pass 1
        assert "proj::docs.guide::intro" not in result

    def test_empty_frozen_tags_matches_baseline(self, db_path: Path):
        """frozen_tags=[] or None yields identical stale_map to baseline.

        Regression guard for backward compat — projects that have not opted
        in to frozen_tags must see identical behavior.
        """
        _seed_doc_graph(db_path, tags='["consumer"]')

        baseline = _get_linked_stale_ids(db_path, transitive_tags=["consumer"])
        explicit_none = _get_linked_stale_ids(db_path, transitive_tags=["consumer"], frozen_tags=None)
        explicit_empty = _get_linked_stale_ids(db_path, transitive_tags=["consumer"], frozen_tags=[])

        assert baseline == explicit_none
        assert baseline == explicit_empty

    def test_preexisting_linked_stale_preserved_on_frozen_adoption(self, db_path: Path, tmp_path: Path):
        """ADR-018 sticky invariant: adopting frozen_tags must NOT silently
        clear pre-existing LINKED_STALE on frozen sections.

        Scenario:
        1. ADR-tagged doc section is linked to code that has drifted.
        2. record_staleness runs with frozen_tags=[] -> section becomes
           LINKED_STALE in DB.
        3. Project adopts frozen_tags=["adr"] and record_staleness runs
           again -> section MUST remain LINKED_STALE (only mark_clean
           is allowed to clear it).
        """
        from axiom_graph.index.staleness import record_staleness

        now = db._now_utc()

        # Code node that lives at a real path under the project root.
        # Pre-create the file so own_status doesn't go NOT_FOUND.
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        code_file = src_dir / "mod.py"
        code_file.write_text("def fn():\n    return 1\n")

        code_fn = _node("proj::mod.fn", location="src/mod.py")
        adr_section = _node(
            "proj::docs.adr-001::ctx",
            subtype="docjson",
            code_hash="adrhash",
            location="docs/adr-001.json",
        )

        with db._connect(db_path) as conn:
            db.upsert_node_conn(conn, code_fn, now)
            db.upsert_node_conn(conn, adr_section, now)
            conn.execute(
                "INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("proj::docs.adr-001", "ADR-001", '["adr"]', "docs/adr-001.json", None, now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO doc_sections (id, doc_id, heading, level, tags, content, "
                "desc_hash, parent_id, depth, position, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "proj::docs.adr-001::ctx",
                    "proj::docs.adr-001",
                    "Context",
                    2,
                    None,
                    "ctx body",
                    None,
                    None,
                    0,
                    0,
                    now,
                ),
            )
            db.upsert_edge_conn(conn, _edge("proj::docs.adr-001::ctx", "documents", "proj::mod.fn"))
            # Mark code drifted AFTER ADR was updated.
            time.sleep(0.05)
            later = db._now_utc()
            conn.execute(
                "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("proj::mod.fn", later, "CONTENT_ONLY", "abc", "{}", 0),
            )

        # --- Stage 1: project has NOT yet adopted frozen_tags.  Run
        # record_staleness with frozen_tags=[] -> ADR section MUST become
        # LINKED_STALE in DB.
        nodes = db.all_nodes(db_path)
        record_staleness(db_path, tmp_path, nodes, transitive_tags=None, frozen_tags=[])
        with db._connect(db_path) as conn:
            row = conn.execute(
                "SELECT link_status FROM nodes WHERE id = ?",
                ("proj::docs.adr-001::ctx",),
            ).fetchone()
        assert row["link_status"] == "LINKED_STALE", "Pre-adoption baseline: ADR section must be LINKED_STALE"

        # --- Stage 2: project adopts frozen_tags=["adr"].  Run
        # record_staleness again — propagation skip prevents the signal
        # from being RECORDED, but the prior LINKED_STALE MUST NOT be
        # silently cleared.  Only mark_clean is allowed to clear it.
        nodes = db.all_nodes(db_path)
        record_staleness(db_path, tmp_path, nodes, transitive_tags=None, frozen_tags=["adr"])
        with db._connect(db_path) as conn:
            row = conn.execute(
                "SELECT link_status FROM nodes WHERE id = ?",
                ("proj::docs.adr-001::ctx",),
            ).fetchone()
        assert row["link_status"] == "LINKED_STALE", (
            "ADR-018 sticky invariant violated: pre-existing LINKED_STALE "
            "was silently cleared to VERIFIED when frozen_tags was adopted. "
            "Only mark_clean should be able to clear LINKED_STALE."
        )
