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
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.db import migrations
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

    doc_id = "proj::docs.guide"
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
    assert db_path.with_name("graph.db.pre-v1.bak").exists()
    doc_id = "proj::docs.guide"
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

    assert migrations.run_migrations(db_path) == [1]

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
    assert ("proj::docs.guide::intro", "proj::mod::f") in edges


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
    assert not db_path.with_name("graph.db.pre-v1.bak").exists(), "no backup for a no-op run"

    builder.build(tmp_path)
    assert "doc_sections" not in _table_names(db_path)
    with db._connect(db_path) as conn:
        sub = conn.execute("SELECT subtype FROM nodes WHERE id = 'proj::docs.note::s1'").fetchone()
    assert sub is not None and sub[0] == "docjson_section"
