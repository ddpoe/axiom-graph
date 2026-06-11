"""DocJSON shadow row invariant tests (cycle pev-2026-05-15).

For every row in ``nodes`` with ``subtype='docjson'``, the invariant is:

    nodes.updated_at  == doc_sections.updated_at
    nodes.desc_hash   == doc_sections.desc_hash
    nodes.level_1     == doc_sections.heading
    nodes.level_2     == doc_sections.content

This module covers:

- Property test: invariant holds after every doc-section write path
  (``upsert_doc_section``, ``index_doc_sections_fts``, ``upsert_node_conn``
  on a ``subtype='docjson'`` node).
- Regression: end-to-end through ``axiom_graph_update_section`` on a
  LINKED_STALE section.  Invariant fields advance; ``link_status``
  semantics remain governed by the existing writer-is-verifier hook
  (NOT by ``_sync_docjson_shadow`` itself).
- Verification check: clean DB reports zero; synthesized drift reports
  the offending row.
- Helper unit tests: no-op when section absent; idempotent on re-call.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path


from axiom_graph.db import _core
from axiom_graph.db import docs as db_docs
from axiom_graph.db import nodes as db_nodes
from axiom_graph.models import AxiomNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_section(
    db_path: Path,
    doc_id: str,
    sec_id: str,
    *,
    heading: str = "Heading",
    content: str = "Body",
    updated_at: str | None = None,
    desc_hash: str | None = None,
) -> dict:
    """Insert a doc + doc_section row (canonical only — no shadow)."""
    now = updated_at or _now_iso()
    dh = desc_hash or hashlib.sha256(content.encode()).hexdigest()[:16]
    sec = {
        "id": sec_id,
        "doc_id": doc_id,
        "heading": heading,
        "level": 2,
        "tags": "",
        "content": content,
        "desc_hash": dh,
        "parent_id": None,
        "depth": 0,
        "position": 0,
        "updated_at": now,
    }
    with _core._connect(db_path) as conn:
        db_docs.upsert_doc(
            conn,
            {
                "id": doc_id,
                "title": "Doc",
                "tags": "",
                "file_path": f"docs/{doc_id.split('::')[-1]}.json",
                "desc_hash": "tdh",
                "updated_at": now,
            },
        )
        # Direct INSERT bypasses the helper sync, so the shadow row is
        # only created by the test setup that needs it.
        conn.execute(
            """
            INSERT OR REPLACE INTO doc_sections
                (id, doc_id, heading, level, tags, content, desc_hash,
                 parent_id, depth, position, updated_at)
            VALUES
                (:id, :doc_id, :heading, :level, :tags, :content,
                 :desc_hash, :parent_id, :depth, :position, :updated_at)
            """,
            sec,
        )
    return sec


def _seed_shadow(db_path: Path, sec: dict) -> None:
    """Insert a stale shadow row (heading/content/desc_hash/updated_at deliberately mismatched)."""
    with _core._connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO nodes
                (id, node_type, subtype, title, location, status, source,
                 code_hash, desc_hash, level_0, level_1, level_2,
                 level_3_location, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sec["id"],
                "atomic_process",
                "docjson",
                "STALE_HEADING",
                f"docs/{sec['doc_id'].split('::')[-1]}.json",
                "active",
                "docjson",
                "stale_code_hash",  # should be preserved
                "stale_desc_hash",
                "STALE_HEADING",
                "STALE_HEADING",  # level_1
                "STALE_CONTENT",  # level_2
                None,
                "1970-01-01T00:00:00+00:00",
            ),
        )


def _read_invariant(db_path: Path, sec_id: str) -> tuple[dict, dict] | None:
    """Return (nodes_row, doc_sections_row) for an id, or None."""
    with _core._connect(db_path) as conn:
        nrow = conn.execute(
            "SELECT id, level_1, level_2, desc_hash, updated_at, code_hash, subtype FROM nodes WHERE id = ?",
            (sec_id,),
        ).fetchone()
        srow = conn.execute(
            "SELECT id, heading, content, desc_hash, updated_at FROM doc_sections WHERE id = ?",
            (sec_id,),
        ).fetchone()
        if nrow is None or srow is None:
            return None
        return dict(nrow), dict(srow)


def _assert_invariant(db_path: Path, sec_id: str) -> None:
    pair = _read_invariant(db_path, sec_id)
    assert pair is not None, f"Missing pair for {sec_id}"
    nrow, srow = pair
    assert nrow["level_1"] == srow["heading"], f"level_1 != heading: {nrow['level_1']!r} vs {srow['heading']!r}"
    assert (nrow["level_2"] or "") == (srow["content"] or ""), (
        f"level_2 != content: {nrow['level_2']!r} vs {srow['content']!r}"
    )
    assert nrow["desc_hash"] == srow["desc_hash"], f"desc_hash mismatch: {nrow['desc_hash']!r} vs {srow['desc_hash']!r}"
    assert nrow["updated_at"] == srow["updated_at"], (
        f"updated_at mismatch: {nrow['updated_at']!r} vs {srow['updated_at']!r}"
    )


# ---------------------------------------------------------------------------
# Tier 1: Helper unit tests
# ---------------------------------------------------------------------------


class TestSyncHelper:
    def test_helper_noop_when_section_absent(self, db_path: Path) -> None:
        """Calling helper for a non-existent section is a silent no-op."""
        with _core._connect(db_path) as conn:
            # Should not raise.
            db_docs._sync_docjson_shadow(conn, "p::nope::nada")
        # Sanity: nothing got inserted.
        with _core._connect(db_path) as conn:
            row = conn.execute("SELECT id FROM nodes WHERE id = ?", ("p::nope::nada",)).fetchone()
        assert row is None

    def test_helper_idempotent(self, db_path: Path) -> None:
        """Calling helper twice produces the same final state."""
        sec = _seed_section(db_path, "p::docs.spec", "p::docs.spec::sec", heading="H", content="C")
        _seed_shadow(db_path, sec)
        with _core._connect(db_path) as conn:
            db_docs._sync_docjson_shadow(conn, sec["id"])
        _assert_invariant(db_path, sec["id"])
        with _core._connect(db_path) as conn:
            db_docs._sync_docjson_shadow(conn, sec["id"])
        _assert_invariant(db_path, sec["id"])

    def test_helper_preserves_code_hash(self, db_path: Path) -> None:
        """The helper writes the four invariant fields only — never `code_hash`."""
        sec = _seed_section(db_path, "p::docs.spec", "p::docs.spec::sec")
        _seed_shadow(db_path, sec)  # seeds code_hash="stale_code_hash"
        with _core._connect(db_path) as conn:
            db_docs._sync_docjson_shadow(conn, sec["id"])
            nrow = conn.execute("SELECT code_hash FROM nodes WHERE id = ?", (sec["id"],)).fetchone()
        assert nrow["code_hash"] == "stale_code_hash"


# ---------------------------------------------------------------------------
# Tier 2: Property tests — every write path syncs
# ---------------------------------------------------------------------------


class TestEveryWritePathSyncs:
    """Tier 2: each doc-section write path leaves the invariant satisfied."""

    def test_upsert_doc_section_syncs_shadow(self, db_path: Path) -> None:
        """After ``upsert_doc_section``, shadow matches canonical."""
        sec = {
            "id": "p::docs.spec::sec_a",
            "doc_id": "p::docs.spec",
            "heading": "Heading A",
            "level": 2,
            "tags": "",
            "content": "New body A",
            "desc_hash": hashlib.sha256(b"A").hexdigest()[:16],
            "parent_id": None,
            "depth": 0,
            "position": 0,
            "updated_at": _now_iso(),
        }
        # Seed a stale shadow that doesn't match.
        _seed_shadow(db_path, sec)
        with _core._connect(db_path) as conn:
            db_docs.upsert_doc(
                conn,
                {
                    "id": "p::docs.spec",
                    "title": "Doc",
                    "tags": "",
                    "file_path": "docs/spec.json",
                    "desc_hash": "tdh",
                    "updated_at": sec["updated_at"],
                },
            )
            db_docs.upsert_doc_section(conn, sec)
        _assert_invariant(db_path, sec["id"])

    def test_index_doc_sections_fts_syncs_shadow_when_missing(self, db_path: Path) -> None:
        """``index_doc_sections_fts`` creates shadow and syncs invariant."""
        _seed_section(db_path, "p::docs.spec", "p::docs.spec::sec_b", heading="H", content="C")
        # No shadow row exists yet.
        db_docs.index_doc_sections_fts(db_path)
        _assert_invariant(db_path, "p::docs.spec::sec_b")

    def test_index_doc_sections_fts_syncs_shadow_when_existing(self, db_path: Path) -> None:
        """``index_doc_sections_fts`` resyncs existing stale shadow."""
        sec = _seed_section(db_path, "p::docs.spec", "p::docs.spec::sec_c", heading="H2", content="C2")
        _seed_shadow(db_path, sec)
        db_docs.index_doc_sections_fts(db_path)
        _assert_invariant(db_path, sec["id"])

    def test_upsert_node_conn_discovery_only_syncs_docjson_shadow(self, db_path: Path) -> None:
        """When ``upsert_node_conn`` runs in discovery_only on a docjson node,
        the helper still syncs the invariant from doc_sections.
        """
        sec = _seed_section(
            db_path,
            "p::docs.spec",
            "p::docs.spec::sec_d",
            heading="Updated Heading",
            content="Updated body",
        )
        _seed_shadow(db_path, sec)

        # Build an AxiomNode that LOOKS like the scanner's representation
        # of this doc-section node (subtype='docjson').
        node = AxiomNode(
            id=sec["id"],
            node_type="atomic_process",
            subtype="docjson",
            title=sec["heading"],
            location=f"docs/{sec['doc_id'].split('::')[-1]}.json",
            level_3_location=None,
            source="docjson",
            code_hash="discovery_hash",
            desc_hash="discovery_desc",
            level_0=sec["heading"],
            level_1="DIFFERENT_HEADING",  # would normally win, but invariant wins
            level_2="DIFFERENT_BODY",
        )

        with _core._connect(db_path) as conn:
            db_nodes.upsert_node_conn(conn, node, discovery_only=True)

        # After upsert, the shadow row's invariant fields must mirror
        # doc_sections (heading/content/desc_hash/updated_at), NOT the
        # AxiomNode's stale level_1/level_2.
        _assert_invariant(db_path, sec["id"])

    def test_mixed_write_sequence_holds_invariant(self, db_path: Path) -> None:
        """Run every write path back-to-back and verify."""
        sec_id = "p::docs.spec::sec_mix"
        sec = _seed_section(db_path, "p::docs.spec", sec_id, heading="H0", content="C0")
        _seed_shadow(db_path, sec)

        # 1. upsert_doc_section refresh.
        sec["heading"] = "H1"
        sec["content"] = "C1"
        sec["desc_hash"] = hashlib.sha256(b"C1").hexdigest()[:16]
        sec["updated_at"] = _now_iso()
        with _core._connect(db_path) as conn:
            db_docs.upsert_doc_section(conn, sec)
        _assert_invariant(db_path, sec_id)

        # 2. index_doc_sections_fts.
        db_docs.index_doc_sections_fts(db_path)
        _assert_invariant(db_path, sec_id)

        # 3. upsert_node_conn discovery-only.
        node = AxiomNode(
            id=sec_id,
            node_type="atomic_process",
            subtype="docjson",
            title="H1",
            location="docs/spec.json",
            source="docjson",
            code_hash="cd",
            desc_hash="dd",
            level_0="H1",
            level_1="DRIFTED",
            level_2="DRIFTED",
        )
        with _core._connect(db_path) as conn:
            db_nodes.upsert_node_conn(conn, node, discovery_only=True)
        _assert_invariant(db_path, sec_id)


# ---------------------------------------------------------------------------
# Tier 2: Verification check (clean + synthesized drift)
# ---------------------------------------------------------------------------


class TestInvariantVerificationCheck:
    """The verification check reports zero on clean DBs and N on drift."""

    def test_clean_db_after_helper_reports_zero(self, db_path: Path) -> None:
        sec = _seed_section(db_path, "p::docs.spec", "p::docs.spec::sec_clean")
        # Ensure shadow exists then sync.
        _seed_shadow(db_path, sec)
        with _core._connect(db_path) as conn:
            db_docs.upsert_doc_section(conn, sec)
        violations = db_docs.find_docjson_shadow_invariant_violations(db_path)
        assert violations == []

    def test_synthesized_drift_is_detected(self, db_path: Path) -> None:
        sec = _seed_section(db_path, "p::docs.spec", "p::docs.spec::sec_drift")
        # Create shadow + sync to baseline.
        _seed_shadow(db_path, sec)
        with _core._connect(db_path) as conn:
            db_docs.upsert_doc_section(conn, sec)
        # Sanity: invariant holds at this point.
        assert db_docs.find_docjson_shadow_invariant_violations(db_path) == []
        # Synthesize drift by mutating doc_sections WITHOUT touching nodes.
        with _core._connect(db_path) as conn:
            conn.execute(
                "UPDATE doc_sections SET content = 'DRIFTED', desc_hash = 'NEWHASH', updated_at = ? WHERE id = ?",
                (_now_iso(), sec["id"]),
            )
        violations = db_docs.find_docjson_shadow_invariant_violations(db_path)
        assert sec["id"] in violations

    def test_no_section_no_drift(self, db_path: Path) -> None:
        """A `subtype='docjson'` shadow row without a canonical row is NOT a violation."""
        # Insert a shadow node with no matching doc_sections row.
        with _core._connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO nodes
                    (id, node_type, subtype, title, location, status, source,
                     code_hash, desc_hash, level_0, level_1, level_2,
                     level_3_location, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "p::docs.lost::sec",
                    "atomic_process",
                    "docjson",
                    "Lost",
                    "docs/lost.json",
                    "active",
                    "docjson",
                    "",
                    "",
                    "Lost",
                    "Lost",
                    "",
                    None,
                    "1970-01-01T00:00:00+00:00",
                ),
            )
        # A missing canonical row is treated as a different category of
        # bug (orphan shadow), NOT an invariant violation.
        violations = db_docs.find_docjson_shadow_invariant_violations(db_path)
        assert "p::docs.lost::sec" not in violations


# ---------------------------------------------------------------------------
# Tier 3: E2E regression through axiom_graph_update_section
# ---------------------------------------------------------------------------


from axiom_annotations import workflow, Step
from axiom_graph.index import builder
from axiom_graph.docjson.api import axiom_graph_update_section
from axiom_graph.index.staleness import _get_linked_stale_ids


@workflow(
    purpose=(
        "Editing a LINKED_STALE doc-section via axiom_graph_update_section "
        "advances all four shadow-row invariant fields in lockstep with "
        "doc_sections.  The pre-existing writer-is-verifier semantic from "
        "cycle pev-2026-05-02 still governs link_status clearance — the "
        "_sync_docjson_shadow helper itself never touches link_status."
    ),
)
def test_update_section_keeps_invariant_after_linked_stale_edit(mini_project: Path, db_path: Path):
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    src_dir = mini_project / "src"
    src_dir.mkdir(exist_ok=True)

    口 = Step(
        step_num=1,
        name="Write code + doc section, full build to set baselines",
        purpose="Establish baseline node_history rows + shadow rows for both sections and the code node",
    )
    code_path = src_dir / "mod.py"
    code_path.write_text("def foo():\n    return 0\n", encoding="utf-8")
    doc_path = docs_dir / "spec.json"
    doc_path.write_text(
        json.dumps(
            {
                "title": "Spec",
                "sections": [
                    {
                        "id": "overview",
                        "heading": "Overview",
                        "content": "Initial content.",
                        "links": [{"node_id": "proj::src.mod::foo"}],
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    口 = Step(
        step_num=2,
        name="Edit code + full build so the section becomes LINKED_STALE",
        purpose="Drift the code so Pass 1 of _get_linked_stale_ids flags the section",
    )
    time.sleep(0.05)
    code_path.write_text("def foo():\n    return 42\n", encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    section_id = "proj::docs.spec::overview"
    ls_before = _get_linked_stale_ids(db_path)
    assert section_id in ls_before, f"Setup failed: expected LS on {section_id}"

    口 = Step(
        step_num=3,
        name="Update section via axiom_graph_update_section",
        purpose="Drives save_and_reindex which now syncs the shadow on every section write",
    )
    time.sleep(0.05)
    result = axiom_graph_update_section(
        project_root=str(mini_project),
        section_id=section_id,
        content="Section content acknowledging code change.",
    )
    assert "ERROR" not in result, result

    口 = Step(
        step_num=4,
        name="Invariant holds AND zero violations DB-wide",
        purpose="Confirm helper sync ran via update_section -> save_and_reindex -> upsert_doc_section",
    )
    _assert_invariant(db_path, section_id)
    violations = db_docs.find_docjson_shadow_invariant_violations(db_path)
    assert violations == [], f"Invariant violations after update_section: {violations}"
