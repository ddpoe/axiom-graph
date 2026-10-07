"""Schema versioning + migration runner tests.

Two lifetimes live here deliberately:

- The legacy-upgrade pair (``test_legacy_db_upgrade_e2e``,
  ``test_migration_preserves_history_verification_renames``) exercises the
  v1 legacy->envelope step and is deleted together with that step.
- The runner-safety and fresh-init tests exercise the permanent versioning
  framework (rollback/retry, downgrade guard, version stamping) that future
  migrations rely on.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.db import _core, migrations
from axiom_graph.index import builder, db

PROJ_TOML = '[axiom_graph]\nproject_id = "proj"\n'


def _insert_row(conn: sqlite3.Connection, table: str, **values) -> None:
    """Insert a row supplying only the named columns (others NULL/default)."""
    cols = ", ".join(values)
    ph = ", ".join("?" * len(values))
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", tuple(values.values()))


_LEGACY_DOC_SECTIONS_DDL = """
CREATE TABLE doc_sections (
    id         TEXT PRIMARY KEY,
    doc_id     TEXT NOT NULL,
    heading    TEXT,
    level      INTEGER,
    tags       TEXT,
    content    TEXT,
    desc_hash  TEXT,
    parent_id  TEXT,
    depth      INTEGER,
    position   INTEGER,
    updated_at TEXT
)
"""


def _make_legacy_project(root: Path) -> Path:
    """Create a project whose DB is in the pre-envelope two-table shape.

    The DB carries: a docs row, doc_sections rows (nested section, a
    tombstoned orphan, and a live orphan), shadow node rows for the doc and
    one section, history rows, a verification baseline, and a rename record.
    ``user_version`` is reset to 0 so the runner sees a legacy DB, and the
    ``nodes`` table is stripped of ``doc_position`` / ``doc_level`` (the
    legacy schema never had them) so migration v1's guarded ALTER TABLE
    branch actually executes.
    """
    (root / "axiom-graph.toml").write_text(PROJ_TOML, encoding="utf-8")
    (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    docs_dir = root / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.json").write_text(
        json.dumps(
            {
                "title": "Guide",
                "sections": [
                    {
                        "id": "intro",
                        "heading": "Intro",
                        "content": "intro body",
                        "sections": [{"id": "details", "heading": "Details", "content": "detail body"}],
                    },
                    {"id": "extra", "heading": "Extra", "content": "extra body"},
                ],
            }
        ),
        encoding="utf-8",
    )
    ag = root / ".axiom_graph"
    ag.mkdir()
    db_path = ag / "graph.db"
    db.init_db(db_path)

    doc_id = "proj::docs/guide"
    now = "2026-01-01T00:00:00Z"
    with db._connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 0")
        # Faithful legacy nodes shape: the pre-envelope schema had no
        # doc_position / doc_level columns.
        conn.execute("ALTER TABLE nodes DROP COLUMN doc_position")
        conn.execute("ALTER TABLE nodes DROP COLUMN doc_level")
        conn.execute(_LEGACY_DOC_SECTIONS_DDL)
        # docs metadata row
        _insert_row(
            conn,
            "docs",
            id=doc_id,
            title="Guide",
            tags="[]",
            file_path="docs/guide.json",
            desc_hash="dh",
            updated_at=now,
        )
        # Envelope + one section shadow node (legacy 'docjson' subtype).
        _insert_row(
            conn,
            "nodes",
            id=doc_id,
            node_type="composite_process",
            subtype="docjson",
            title="Guide",
            location="docs/guide.json",
            source="json_doc_scanner",
            code_hash="envbase",
            level_0=doc_id,
            level_1="Guide",
            updated_at=now,
        )
        _insert_row(
            conn,
            "nodes",
            id=f"{doc_id}::intro",
            node_type="atomic_process",
            subtype="docjson",
            title="Intro",
            location="docs/guide.json",
            source="json_doc_scanner",
            code_hash="introbase",
            desc_hash="introdesc",
            level_0=f"{doc_id}::intro",
            level_1="Intro",
            level_2="intro body",
            updated_at=now,
        )
        # A code node so documents edges / history have a real target.
        _insert_row(
            conn,
            "nodes",
            id="proj::mod::f",
            node_type="atomic_process",
            subtype="function",
            title="f",
            location="mod.py",
            source="ast",
            code_hash="codehash",
            level_0="f",
            level_1="f",
            updated_at=now,
        )
        # Legacy canonical section rows.
        sec_rows = [
            (f"{doc_id}::intro", doc_id, "Intro", 2, '["guide"]', "intro body", "introdesc", None, 0, 0, now),
            (
                f"{doc_id}::intro.details",
                doc_id,
                "Details",
                3,
                None,
                "detail body",
                "detdesc",
                f"{doc_id}::intro",
                1,
                0,
                now,
            ),
            (f"{doc_id}::extra", doc_id, "Extra", 2, None, "extra body", "extradesc", None, 0, 1, now),
            # Orphan with a preserved DELETED tombstone: must stay dead.
            (f"{doc_id}::ghost", doc_id, "Ghost", 2, None, "ghost body", "ghostdesc", None, 0, 2, now),
        ]
        for r in sec_rows:
            conn.execute(
                "INSERT INTO doc_sections (id, doc_id, heading, level, tags, content, "
                "desc_hash, parent_id, depth, position, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                r,
            )
        # documents edge from a section to code.
        _insert_row(
            conn,
            "edges",
            id=f"{doc_id}::intro::documents::proj::mod::f",
            edge_type="documents",
            from_id=f"{doc_id}::intro",
            to_id="proj::mod::f",
        )
        # History: a code-change row + the ghost's preserved tombstone.
        _insert_row(
            conn,
            "node_history",
            node_id="proj::mod::f",
            change_type="CONTENT_ONLY",
            scanned_at=now,
        )
        _insert_row(
            conn,
            "node_history",
            node_id=f"{doc_id}::ghost",
            change_type="DELETED",
            preserved=1,
            scanned_at=now,
        )
        # Verification baseline (mark_clean) for the section.
        _insert_row(
            conn,
            "node_verification",
            node_id=f"{doc_id}::intro",
            verified_at=now,
            verified_by="human",
            code_hash_at="introbase",
        )
        # A rename record.
        _insert_row(
            conn,
            "node_renames",
            old_id="proj::old.mod::f",
            new_id="proj::mod::f",
            renamed_at=now,
            file_path="mod.py",
        )
    return db_path


def _snapshot(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]


def _table_names(db_path: Path) -> set[str]:
    with db._connect(db_path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _node_columns(db_path: Path) -> set[str]:
    with db._connect(db_path) as conn:
        return {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}


@workflow(
    purpose="Existing legacy DB upgrades invisibly on a normal build: in-place migration, backup written, never re-runs"
)
def test_legacy_db_upgrade_e2e(tmp_path: Path) -> None:
    """A legacy two-table DB upgrades in place on a normal build, exactly once."""
    口 = Step(
        step_num=1,
        name="Create legacy-schema project",
        purpose="DB with doc_sections, shadow rows, history, verification, renames; user_version=0; nodes lacks doc_position/doc_level",
    )
    db_path = _make_legacy_project(tmp_path)
    assert "doc_sections" in _table_names(db_path)
    assert not {"doc_position", "doc_level"} & _node_columns(db_path)

    口 = Step(
        step_num=2,
        name="Run a normal build",
        purpose="The migration runner fires before scanning — the whole upgrade is one ordinary build",
    )
    builder.build(tmp_path)

    口 = Step(
        step_num=3,
        name="Verify migrated shape",
        purpose="user_version stamped, backup exists, table dropped, ALTER TABLE added the section columns, IDs preserved, tombstoned orphan stays dead",
    )
    with db._connect(db_path) as conn:
        assert migrations.get_user_version(conn) == migrations.CURRENT_SCHEMA_VERSION
        ids = {r[0] for r in conn.execute("SELECT id FROM nodes").fetchall()}
    assert "doc_sections" not in _table_names(db_path)
    assert {"doc_position", "doc_level"} <= _node_columns(db_path)
    assert db_path.with_name(f"graph.db.pre-v{migrations.CURRENT_SCHEMA_VERSION}.bak").exists()
    doc_id = "proj::docs/guide"
    assert f"{doc_id}::intro" in ids
    assert f"{doc_id}::intro.details" in ids
    assert f"{doc_id}::extra" in ids
    assert f"{doc_id}::ghost" not in ids, "tombstoned orphan must not resurrect"

    口 = Step(step_num=4, name="Second build is a no-op", purpose="Migration never re-runs on a current-schema DB")
    assert migrations.run_migrations(db_path) == []
    builder.build(tmp_path)
    assert "doc_sections" not in _table_names(db_path)
    with db._connect(db_path) as conn:
        assert migrations.get_user_version(conn) == migrations.CURRENT_SCHEMA_VERSION


@workflow(
    purpose="Upgrade preserves node_history / node_verification / node_renames byte-identically and keeps section IDs + documents edges stable"
)
def test_migration_preserves_history_verification_renames(tmp_path: Path) -> None:
    db_path = _make_legacy_project(tmp_path)
    with db._connect(db_path) as conn:
        pre_history = _snapshot(conn, "node_history")
        pre_verification = _snapshot(conn, "node_verification")
        pre_renames = _snapshot(conn, "node_renames")

    assert migrations.run_migrations(db_path) == sorted(migrations.MIGRATIONS)

    with db._connect(db_path) as conn:
        post_history = _snapshot(conn, "node_history")
        post_verification = _snapshot(conn, "node_verification")
        post_renames = _snapshot(conn, "node_renames")
        edges = {
            (r[0], r[1])
            for r in conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type='documents'").fetchall()
        }
    # The migration itself must neither mutate nor append rows in any of
    # the three preserved tables.
    assert post_history == pre_history
    assert post_verification == pre_verification
    assert post_renames == pre_renames
    assert ("proj::docs/guide::intro", "proj::mod::f") in edges


@workflow(
    purpose="Runner framework safety: failed step rolls back and retries cleanly; a newer-versioned DB errors instead of being touched"
)
def test_runner_rollback_retry_and_downgrade_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Downgrade guard (before any monkeypatching).
    newer = tmp_path / "newer"
    newer.mkdir()
    newer_db = newer / "graph.db"
    db.init_db(newer_db)
    with db._connect(newer_db) as conn:
        conn.execute(f"PRAGMA user_version = {migrations.CURRENT_SCHEMA_VERSION + 5}")
    with pytest.raises(migrations.SchemaVersionError):
        migrations.run_migrations(newer_db)

    # Synthetic step: writes a marker then fails mid-step.
    target = migrations.CURRENT_SCHEMA_VERSION + 1
    work = tmp_path / "work"
    work.mkdir()
    work_db = work / "graph.db"
    db.init_db(work_db)

    def bad_step(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO tags (node_id, tag) VALUES ('marker', 'attempted')")
        raise RuntimeError("synthetic mid-step failure")

    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", target)
    monkeypatch.setattr(migrations, "MIGRATIONS", {target: bad_step})
    with pytest.raises(RuntimeError):
        migrations.run_migrations(work_db)
    with db._connect(work_db) as conn:
        assert migrations.get_user_version(conn) == target - 1, "failed step must not stamp the version"
        assert conn.execute("SELECT 1 FROM tags WHERE node_id='marker'").fetchone() is None, (
            "failed step's writes must roll back"
        )

    # Retry with a fixed step succeeds and stamps the version.
    def good_step(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO tags (node_id, tag) VALUES ('marker', 'applied')")

    monkeypatch.setattr(migrations, "MIGRATIONS", {target: good_step})
    assert migrations.run_migrations(work_db) == [target]
    with db._connect(work_db) as conn:
        assert migrations.get_user_version(conn) == target
        assert conn.execute("SELECT tag FROM tags WHERE node_id='marker'").fetchone()[0] == "applied"


@workflow(
    purpose="Fresh init stamps the current schema version, the runner no-ops, and the schema has no legacy doc_sections table"
)
def test_fresh_init_stamps_version_and_noop(tmp_path: Path) -> None:
    (tmp_path / "axiom-graph.toml").write_text(PROJ_TOML, encoding="utf-8")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "note.json").write_text(
        json.dumps({"title": "Note", "sections": [{"id": "s1", "heading": "H", "content": "c"}]}),
        encoding="utf-8",
    )
    ag = tmp_path / ".axiom_graph"
    ag.mkdir()
    db_path = ag / "graph.db"
    db.init_db(db_path)

    with db._connect(db_path) as conn:
        assert migrations.get_user_version(conn) == migrations.CURRENT_SCHEMA_VERSION
    assert migrations.run_migrations(db_path) == []
    assert not list(db_path.parent.glob("graph.db.pre-v*.bak")), "no backup for a no-op run"

    builder.build(tmp_path)
    assert "doc_sections" not in _table_names(db_path)
    with db._connect(db_path) as conn:
        sub = conn.execute("SELECT subtype FROM nodes WHERE id = 'proj::docs/note::s1'").fetchone()
    assert sub is not None and sub[0] == "docjson_section"


def test_v3_clears_mtimes_of_python_test_files_only(tmp_path: Path) -> None:
    """The v3 step NULLs the stored mtime of every Python test file and keeps production files' mtimes."""
    (tmp_path / "axiom-graph.toml").write_text(PROJ_TOML, encoding="utf-8")
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("def test_f():\n    assert True\n", encoding="utf-8")
    (tmp_path / "tests" / "mod_test.py").write_text("def test_g():\n    assert True\n", encoding="utf-8")
    db_path = tmp_path / ".axiom_graph" / "graph.db"
    builder.build(tmp_path)

    def stamped() -> dict[str, bool]:
        with db._connect(db_path) as conn:
            rows = conn.execute(
                "SELECT location, MAX(file_mtime IS NOT NULL) FROM nodes WHERE location LIKE '%.py' GROUP BY location"
            ).fetchall()
        return {r[0]: bool(r[1]) for r in rows}

    assert stamped() == {"mod.py": True, "tests/test_mod.py": True, "tests/mod_test.py": True}
    with db._connect(db_path) as conn:
        migrations._migrate_v3_rescan_python_test_files(conn)
    assert stamped() == {"mod.py": True, "tests/test_mod.py": False, "tests/mod_test.py": False}


# ---------------------------------------------------------------------------
# Migration step v2 — DocJSON envelope tag re-sync
# ---------------------------------------------------------------------------


def _tag_rows(db_path: Path, node_id: str) -> set[str]:
    """Return the tag rows the index holds for *node_id*."""
    with db._connect(db_path) as conn:
        return {r[0] for r in conn.execute("SELECT tag FROM tags WHERE node_id = ?", (node_id,)).fetchall()}


def _make_tagged_docs_project(root: Path) -> Path:
    """Build a project with two tagged DocJSON documents and return its db path."""
    (root / "axiom-graph.toml").write_text(PROJ_TOML, encoding="utf-8")
    docs_dir = root / "docs"
    docs_dir.mkdir()
    for slug, title, tags in (("drifted", "Drifted", ["architecture", "v2"]), ("intact", "Intact", ["reference"])):
        (docs_dir / f"{slug}.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "sections": [{"id": "body", "heading": "Body", "content": f"{title} body."}],
                    "tags": tags,
                }
            ),
            encoding="utf-8",
        )
    builder.build(root)
    return root / ".axiom_graph" / "graph.db"


@workflow(
    purpose="Upgrading an index repairs drifted DocJSON envelope tag rows from stored doc tags, leaves undrifted ones alone, "
    "is idempotent, and disturbs no history, verification or staleness state",
)
def test_v2_resyncs_drifted_envelope_tags(tmp_path: Path) -> None:
    db_path = _make_tagged_docs_project(tmp_path)
    drifted_id = "proj::docs/drifted"
    intact_id = "proj::docs/intact"

    # A verification baseline so the preservation claim has something to bite on.
    db.upsert_verification(db_path, drifted_id, verified_by="human", code_hash_at="base", desc_hash_at="base")

    # Drift the envelope's tag rows away from its stored docs.tags, and put the
    # DB back on the previous schema version so the step is pending.
    with db._connect(db_path) as conn:
        conn.execute("DELETE FROM tags WHERE node_id = ?", (drifted_id,))
        conn.execute("INSERT INTO tags (node_id, tag) VALUES (?, ?)", (drifted_id, "obsolete"))
        conn.execute("PRAGMA user_version = 1")
    assert _tag_rows(db_path, drifted_id) == {"obsolete"}

    with db._connect(db_path) as conn:
        pre_nodes = _snapshot(conn, "nodes")
        pre_history = _snapshot(conn, "node_history")
        pre_verification = _snapshot(conn, "node_verification")
        pre_renames = _snapshot(conn, "node_renames")
        pre_docs = _snapshot(conn, "docs")

    assert migrations.run_migrations(db_path) == list(range(2, migrations.CURRENT_SCHEMA_VERSION + 1))

    # The drifted document's rows now agree with its stored tags — in both
    # directions: the tags it lacked were added, the one it should not have
    # was removed.
    assert _tag_rows(db_path, drifted_id) == {"architecture", "v2"}
    # The already-correct document was left exactly as it was.
    assert _tag_rows(db_path, intact_id) == {"reference"}

    # Nothing else moved: no row of nodes/docs changed, so no staleness column,
    # baseline hash or updated_at timestamp advanced, and the three preserved
    # tables are byte-identical.
    with db._connect(db_path) as conn:
        assert _snapshot(conn, "nodes") == pre_nodes
        assert _snapshot(conn, "docs") == pre_docs
        assert _snapshot(conn, "node_history") == pre_history
        assert _snapshot(conn, "node_verification") == pre_verification
        assert _snapshot(conn, "node_renames") == pre_renames

    # Running the upgrade again is harmless: the runner no-ops on version, and
    # the step itself writes nothing when the sets already agree.
    assert migrations.run_migrations(db_path) == []
    with db._connect(db_path) as conn:
        migrations._migrate_v2_resync_doc_envelope_tags(conn)
    assert _tag_rows(db_path, drifted_id) == {"architecture", "v2"}
    assert _tag_rows(db_path, intact_id) == {"reference"}
    with db._connect(db_path) as conn:
        assert _snapshot(conn, "nodes") == pre_nodes
        assert _snapshot(conn, "node_verification") == pre_verification


def test_v2_tolerates_missing_and_malformed_doc_tags(tmp_path: Path) -> None:
    """Unparseable docs.tags is skipped, NULL clears the rows, orphans stay orphaned."""
    db_path = _make_tagged_docs_project(tmp_path)
    drifted_id = "proj::docs/drifted"
    intact_id = "proj::docs/intact"

    with db._connect(db_path) as conn:
        conn.execute("UPDATE docs SET tags = ? WHERE id = ?", ("{not-json", drifted_id))
        conn.execute("UPDATE docs SET tags = NULL WHERE id = ?", (intact_id,))
        # A docs row with no surviving node must not grow tag rows.
        _insert_row(
            conn,
            "docs",
            id="proj::docs/ghost",
            title="Ghost",
            tags='["ghost"]',
            file_path="docs/ghost.json",
            updated_at="2026-01-01T00:00:00Z",
        )
        migrations._migrate_v2_resync_doc_envelope_tags(conn)

    assert _tag_rows(db_path, drifted_id) == {"architecture", "v2"}, "unparseable tags must leave the rows alone"
    assert _tag_rows(db_path, intact_id) == set(), "NULL tags means the document has none"
    assert _tag_rows(db_path, "proj::docs/ghost") == set(), "a docs row with no node must not create tag rows"


# ---------------------------------------------------------------------------
# Migration step v4 — annotation findings store
# ---------------------------------------------------------------------------


@workflow(
    purpose=(
        "An index from before the annotation findings store upgrades on its first check with no init and that "
        "check writes no store rows; the next build rescans every code file once and fills the store, leaving "
        "verification and history untouched"
    )
)
def test_v4_upgrades_on_check_then_build_rescans_everything(tmp_path: Path) -> None:
    from axiom_graph.lifecycle.api import build_index, mark_clean_nodes, read_annotation_findings  # noqa: PLC0415

    (tmp_path / "axiom-graph.toml").write_text(PROJ_TOML, encoding="utf-8")
    (tmp_path / "dup.py").write_text(
        "from axiom_annotations import Step, workflow\n\n\n"
        '@workflow(purpose="duplicate step numbers")\n'
        "def run_demo():\n"
        "    _ = Step(step_num=1, name='one', purpose='first')\n"
        "    _ = Step(step_num=1, name='two', purpose='second')\n",
        encoding="utf-8",
    )
    (tmp_path / "plain.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    db_path = tmp_path / ".axiom_graph" / "graph.db"
    build_index(db_path, tmp_path)
    mark_clean_nodes(db_path, tmp_path, ["proj::plain::g"], reason="reviewed", verified_by="human")
    with db._connect(db_path) as conn:
        conn.execute("DROP TABLE annotation_findings")
        conn.execute("PRAGMA user_version = 3")
        history = _snapshot(conn, "node_history")
        verification = _snapshot(conn, "node_verification")
    assert "annotation_findings" not in _table_names(db_path)

    read = read_annotation_findings(db_path, tmp_path)
    with db._connect(db_path) as conn:
        assert migrations.get_user_version(conn) == migrations.CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM annotation_findings").fetchone()[0] == 0
        assert _snapshot(conn, "node_history") == history
        assert _snapshot(conn, "node_verification") == verification
    assert read.files_rescanned == 2
    assert [f["rule_id"] for f in read.findings] == ["B1"]
    assert read.new == 1

    summary = build_index(db_path, tmp_path)
    assert summary.files_skipped_mtime == 0
    assert summary.annotation_findings_new == 1
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM annotation_findings WHERE kind = 'finding'").fetchone()[0] == 1
        # The rescan may append its own history rows; the existing ones stay as they were.
        assert _snapshot(conn, "node_history")[: len(history)] == history
        assert _snapshot(conn, "node_verification") == verification
    assert read_annotation_findings(db_path, tmp_path).new == 0


# ---------------------------------------------------------------------------
# Migration step v5 — verification pairs + live hashes
# ---------------------------------------------------------------------------


def _downgrade_to_v4(conn: sqlite3.Connection) -> None:
    """Strip the v5 schema from a fresh index, leaving the v4 shape.

    The v5-only scan indexes go first: a v4 index never has them, and one
    of them filters on a column dropped here.
    """
    for name in _core._SCAN_INDEX_NAMES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("DROP TABLE node_verification_targets")
    conn.execute("ALTER TABLE nodes DROP COLUMN live_code_hash")
    conn.execute("ALTER TABLE nodes DROP COLUMN live_desc_hash")
    conn.execute("PRAGMA user_version = 4")


def _pairs(db_path: Path) -> dict[tuple[str, str], tuple[str, str | None]]:
    """Return every stored verification pair keyed by (dependent, target)."""
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT node_id, target_id, code_hash, desc_hash FROM node_verification_targets")
        return {(r[0], r[1]): (r[2], r[3]) for r in rows}


_UTIL_SRC = """\
from axiom_annotations import Step, workflow


def a():
    return 1


class K:
    def m(self):
        return 2


@workflow(purpose="Run a")
def run():
    口 = Step(step_num=1, name="A", purpose="Call a")
    return a()
"""


@workflow(
    purpose=(
        "A v4 index upgrades to v5 keeping every history, verification and rename row, and backfills pairs only "
        "for VERIFIED dependents against VERIFIED targets at their stored baselines; a doc section linking a "
        "module stays VERIFIED after the upgrade's build and check; a fresh index starts at v5"
    )
)
def test_v5_backfills_pairs_only_for_current_verifications(tmp_path: Path) -> None:
    from axiom_graph.index.staleness import _get_linked_stale_ids  # noqa: PLC0415
    from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes  # noqa: PLC0415

    (tmp_path / "axiom-graph.toml").write_text(PROJ_TOML, encoding="utf-8")
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")
    (tmp_path / "util.py").write_text(_UTIL_SRC, encoding="utf-8")
    module_section = "proj::docs/spec::util"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.json").write_text(
        json.dumps(
            {
                "title": "Spec",
                "sections": [
                    {"id": "util", "heading": "Util", "content": "Util.", "links": [{"node_id": "proj::util"}]}
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "test_mod.py").write_text(
        "from mod import f, g\n\n\n"
        "def test_f():\n    assert f() == 1\n\n\n"
        "def test_g():\n    assert g() == 2\n\n\n"
        "def test_fg():\n    assert f() + g() == 3\n",
        encoding="utf-8",
    )
    db_path = tmp_path / ".axiom_graph" / "graph.db"
    build_index(db_path, tmp_path)
    tests = ["proj::test_mod::test_f", "proj::test_mod::test_g"]
    mark_clean_nodes(db_path, tmp_path, [*tests, module_section], reason="reviewed", verified_by="human")
    # g changes: test_g goes LINKED_STALE and g is own-stale; test_fg is
    # re-verified while g is still own-stale.
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2 + 0\n", encoding="utf-8")
    build_index(db_path, tmp_path)
    mark_clean_nodes(db_path, tmp_path, ["proj::test_mod::test_fg"], reason="reviewed", verified_by="human")
    compute_check_summary(db_path, tmp_path)
    with db._connect(db_path) as conn:
        statuses = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, own_status, link_status FROM nodes")}
        f_hash = conn.execute("SELECT code_hash FROM nodes WHERE id = 'proj::mod::f'").fetchone()[0]
        _downgrade_to_v4(conn)
        history = _snapshot(conn, "node_history")
        verification = _snapshot(conn, "node_verification")
        renames = _snapshot(conn, "node_renames")
    assert statuses["proj::test_mod::test_g"][1] == "LINKED_STALE"
    assert statuses["proj::mod::g"][0] == "CONTENT_UPDATED"
    assert statuses["proj::test_mod::test_fg"][1] == "VERIFIED"

    assert migrations.run_migrations(db_path) == [5]

    with db._connect(db_path) as conn:
        assert migrations.get_user_version(conn) == 5
        assert _snapshot(conn, "node_history") == history
        assert _snapshot(conn, "node_verification") == verification
        assert _snapshot(conn, "node_renames") == renames
    pairs = _pairs(db_path)
    assert pairs[("proj::test_mod::test_f", "proj::mod::f")] == (f_hash, None)
    assert pairs[("proj::test_mod::test_fg", "proj::mod::f")] == (f_hash, None)
    assert ("proj::test_mod::test_fg", "proj::mod::g") not in pairs, "an own-stale target gets no pair"
    assert not [key for key in pairs if key[0] == "proj::test_mod::test_g"], "a LINKED_STALE dependent gets none"
    assert (module_section, "proj::util") in pairs, "a module target pairs by its digest"

    # The backfilled digest leaves out the workflow's step rows, as the engine
    # does, so the module-linked section is not flagged by the upgrade.
    for after, run_pass in (("build", build_index), ("check", compute_check_summary)):
        run_pass(db_path, tmp_path)
        with db._connect(db_path) as conn:
            link = conn.execute("SELECT link_status FROM nodes WHERE id = ?", (module_section,)).fetchone()[0]
        assert link == "VERIFIED", after
        assert module_section not in _get_linked_stale_ids(db_path), after

    fresh = tmp_path / "fresh" / "graph.db"
    db.init_db(fresh)
    with db._connect(fresh) as conn:
        assert migrations.get_user_version(conn) == 5
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "node_verification_targets" in tables
        assert {"live_code_hash", "live_desc_hash"} <= {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert migrations.run_migrations(fresh) == []


@workflow(
    purpose=(
        "On an index not yet upgraded to v5, drift_query, mark_clean and reverify work as before, write no pairs and "
        "leave the schema version at 4; the next build upgrades it and backfills pairs for those verifications"
    )
)
def test_v4_index_tools_work_without_pairs_until_a_build_upgrades_it(tmp_path: Path) -> None:
    from axiom_graph.lifecycle.api import build_index, mark_clean_nodes, reverify_nodes  # noqa: PLC0415
    from axiom_graph.query.api import compute_drift_query  # noqa: PLC0415

    f_id, section, test = "proj::mod::f", "proj::docs/spec::f", "proj::test_mod::test_f"
    (tmp_path / "axiom-graph.toml").write_text(PROJ_TOML, encoding="utf-8")
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(
        "from mod import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.json").write_text(
        json.dumps(
            {"title": "Spec", "sections": [{"id": "f", "heading": "F", "content": "F.", "links": [{"node_id": f_id}]}]}
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / ".axiom_graph" / "graph.db"
    build_index(db_path, tmp_path)
    mark_clean_nodes(db_path, tmp_path, [section, test], reason="reviewed", verified_by="human")
    time.sleep(0.02)
    (tmp_path / "mod.py").write_text("def f():\n    return 1 + 0\n", encoding="utf-8")
    build_index(db_path, tmp_path)
    with db._connect(db_path) as conn:
        _downgrade_to_v4(conn)
        stale = {r[0] for r in conn.execute("SELECT id FROM nodes WHERE link_status = 'LINKED_STALE'")}
    assert {section, test} <= stale

    listed = compute_drift_query(db_path, tmp_path, filter="LINKED_STALE", format="ids", limit=100)
    assert section in listed and test in listed
    assert mark_clean_nodes(db_path, tmp_path, [test], reason="reviewed", verified_by="human").marked == [test]
    assert section in reverify_nodes(db_path, tmp_path, [f_id], "reviewed", verified_by="human").cleared

    with db._connect(db_path) as conn:
        assert migrations.get_user_version(conn) == 4
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "node_verification_targets" not in tables, "no pairs are written below v5"
        assert "live_code_hash" not in {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}

    build_index(db_path, tmp_path)
    with db._connect(db_path) as conn:
        assert migrations.get_user_version(conn) == migrations.CURRENT_SCHEMA_VERSION
        f_hash = conn.execute("SELECT code_hash FROM nodes WHERE id = ?", (f_id,)).fetchone()[0]
    pairs = _pairs(db_path)
    assert pairs[(section, f_id)] == (f_hash, None)
    assert pairs[(test, f_id)] == (f_hash, None)
