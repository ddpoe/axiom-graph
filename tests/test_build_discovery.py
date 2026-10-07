"""Build's discovery walk: a file is parsed when its content or its mtime moved, never otherwise."""

from __future__ import annotations

import os
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.index import db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index
from tests.fixtures.full_recompute import assert_matches_full_recompute


def _project(root: Path, extra: int = 0) -> Path:
    pkg = root / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("x = 1\n\n\ndef f():\n    return 1\n", encoding="utf-8")
    (pkg / "other.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    for i in range(extra):
        (pkg / f"extra_{i}.py").write_text(f"def h{i}():\n    return {i}\n", encoding="utf-8")
    return root


def _build(root: Path):
    return build_index(_db_path(str(root)), root)


def _records(root: Path) -> dict:
    with db._connect(_db_path(str(root))) as conn:
        return db.get_file_records_conn(conn)


def _module_hash(root: Path, location: str) -> str | None:
    with db._connect(_db_path(str(root))) as conn:
        row = conn.execute(
            "SELECT code_hash FROM nodes WHERE location = ? AND subtype = 'module'", (location,)
        ).fetchone()
    return row[0] if row else None


@workflow(
    purpose="Build notices content, not timestamps: a restore is parsed, a touch parses one file, records seed and clear"
)
def test_build_parses_on_content_or_mtime_only(tmp_path: Path) -> None:
    from axiom_graph.models import hash16

    root = _project(tmp_path)
    _build(root)
    _build(root)
    mod = root / "pkg" / "mod.py"

    口 = Step(
        step_num=1, name="Restore keeping the old mtime", purpose="Same size, old mtime, new bytes: build parses it"
    )
    old = os.stat(mod)
    mod.write_text("x = 2\n\n\ndef f():\n    return 1\n", encoding="utf-8")
    os.utime(mod, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert os.stat(mod).st_size == old.st_size
    restored = _build(root)
    assert restored.files_scanned == 1
    with db._connect(_db_path(str(root))) as conn:
        (live,) = conn.execute(
            "SELECT live_code_hash FROM nodes WHERE location = 'pkg/mod.py' AND subtype = 'module'"
        ).fetchone()
    assert live == hash16(mod.read_text(encoding="utf-8"))
    assert_matches_full_recompute(_db_path(str(root)), root)

    口 = Step(step_num=2, name="Touch one file", purpose="Only its mtime moves: build re-parses that file alone")
    st = os.stat(root / "pkg" / "other.py")
    os.utime(root / "pkg" / "other.py", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    touched = _build(root)
    assert touched.files_scanned == 1
    assert_matches_full_recompute(_db_path(str(root)), root)

    口 = Step(step_num=3, name="No-op build", purpose="Nothing moved: nothing is parsed")
    assert _build(root).files_scanned == 0

    口 = Step(
        step_num=4, name="Upgrade seed", purpose="With no records the mtime rule decides once, and records exist after"
    )
    with db._connect(_db_path(str(root))) as conn:
        conn.execute("DELETE FROM file_state")
    seeded = _build(root)
    assert seeded.files_scanned == 0
    records = _records(root)
    assert {"pkg/mod.py", "pkg/other.py", "pkg/__init__.py"} <= set(records)
    assert all(records[loc].parsed_fp for loc in ("pkg/mod.py", "pkg/other.py"))

    口 = Step(step_num=5, name="Clear the records", purpose="The next build parses every file, baselines kept")
    before = _module_hash(root, "pkg/mod.py")
    with db._connect(_db_path(str(root))) as conn:
        db.clear_file_records_conn(conn)
    cleared = _build(root)
    assert cleared.files_scanned == 3
    assert _module_hash(root, "pkg/mod.py") == before
    assert_matches_full_recompute(_db_path(str(root)), root)


def test_a_no_op_build_parses_nothing_however_many_files(tmp_path: Path) -> None:
    small = _project(tmp_path / "small")
    large = _project(tmp_path / "large", extra=6)
    for root in (small, large):
        _build(root)
        assert _build(root).files_scanned == 0
        mod = root / "pkg" / "mod.py"
        mod.write_text(mod.read_text(encoding="utf-8") + "\n\ndef k():\n    return 3\n", encoding="utf-8")
        assert _build(root).files_scanned == 1


def test_a_staleness_record_without_a_parse_record_is_seeded_not_reparsed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _build(root)
    with db._connect(_db_path(str(root))) as conn:
        conn.execute("UPDATE file_state SET parsed_fp = NULL")
    assert _build(root).files_scanned == 0
    assert all(rec.parsed_fp for rec in _records(root).values())
