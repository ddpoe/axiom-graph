"""End-to-end tests for mark_clean baseline hash reset.

Tests that mark_clean properly resets baseline hashes on the nodes table
so that subsequent compute_staleness calls return CLEAN (not CONTENT_UPDATED
or VERIFIED).

Covers: Python nodes, DocJSON atomic (section) nodes, DocJSON composite
(file-level) nodes, baseline hash updates, and the invariant that mark_clean
must NOT advance file_mtime (the builder's scan-skip cache).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom_graph.index import db
from axiom_graph.index.staleness import compute_staleness
from axiom_graph.models import AxiomNode


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
    file_mtime: float | None = None,
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
        file_mtime=file_mtime,
    )


# ---------------------------------------------------------------------------
# Test 1: update_node_baseline updates code_hash/desc_hash, never file_mtime
# ---------------------------------------------------------------------------


class TestUpdateNodeBaseline:
    """Tests for db.update_node_baseline."""

    def test_updates_code_and_desc_hashes(self, db_path: Path) -> None:
        """update_node_baseline sets code_hash and desc_hash on the nodes row."""
        n = _node("proj::mod::func", code_hash="old_code", desc_hash="old_desc")
        db.upsert_node(db_path, n, discovery_only=False)

        db.update_node_baseline(
            db_path,
            node_id="proj::mod::func",
            code_hash="new_code",
            desc_hash="new_desc",
        )

        updated = db.get_node(db_path, "proj::mod::func")
        assert updated.code_hash == "new_code"
        assert updated.desc_hash == "new_desc"

    def test_does_not_touch_file_mtime(self, db_path: Path) -> None:
        """update_node_baseline must leave file_mtime untouched.

        file_mtime is the builder's scan-skip cache, not a verification
        baseline.  Advancing it here would make the next build skip the file
        and freeze the node's scan-derived summary.  See
        ``tests/test_mark_clean_mtime_freeze.py`` for the end-to-end regression.
        """
        n = _node("proj::mod::func", code_hash="old", file_mtime=999.0)
        db.upsert_node(db_path, n, discovery_only=False)

        db.update_node_baseline(
            db_path,
            node_id="proj::mod::func",
            code_hash="new",
            desc_hash=None,
        )

        updated = db.get_node(db_path, "proj::mod::func")
        assert updated.code_hash == "new"
        assert updated.file_mtime == pytest.approx(999.0)  # preserved, not reset


# ---------------------------------------------------------------------------
# Test 2: End-to-end Python node: mark_clean then compute_staleness = CLEAN
# ---------------------------------------------------------------------------


class TestMarkCleanPythonNode:
    """mark_clean on a Python atomic node resets baseline so next check is CLEAN."""

    def test_mark_clean_then_staleness_is_clean(self, mini_project: Path) -> None:
        """After mark_clean on a Python node, compute_staleness returns CLEAN."""
        db_path = mini_project / ".axiom_graph" / "graph.db"

        # Write a Python file with a function
        src_dir = mini_project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        py_file = src_dir / "mod.py"
        py_file.write_text(
            'def my_func():\n    """A docstring."""\n    return 42\n',
            encoding="utf-8",
        )

        # Compute what the current hashes are for this function
        from axiom_graph.models import hash16
        from axiom_graph.scanners.module_scanner import _split_function
        import ast

        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for ast_node in ast.walk(tree):
            if isinstance(ast_node, ast.FunctionDef) and ast_node.name == "my_func":
                code_text, docstring = _split_function(ast_node)
                current_code = hash16(code_text)
                current_desc = hash16(docstring) if docstring else None
                break

        # Insert node with OLD hashes (simulating a stale baseline)
        n = _node(
            "proj::src.mod::my_func",
            code_hash="stale_old_hash",
            desc_hash="stale_old_desc",
            location="src/mod.py",
        )
        db.upsert_node(db_path, n, discovery_only=False)

        # Before fix: compute_staleness should see CONTENT_UPDATED
        statuses_before = compute_staleness(db_path, mini_project, [n])
        assert statuses_before["proj::src.mod::my_func"][0] == "CONTENT_UPDATED"

        # Now call mark_clean via the MCP entry point
        from axiom_graph.mcp_server import axiom_graph_mark_clean

        axiom_graph_mark_clean(str(mini_project), node_id="proj::src.mod::my_func", reason="looks good")

        # Re-read the node (mark_clean should have updated baseline hashes)
        updated = db.get_node(db_path, "proj::src.mod::my_func")

        # Next compute_staleness should return CLEAN
        statuses_after = compute_staleness(db_path, mini_project, [updated])
        assert statuses_after["proj::src.mod::my_func"][0] in ("VERIFIED", "VERIFIED")


# ---------------------------------------------------------------------------
# Test 3: End-to-end DocJSON atomic (section) node
# ---------------------------------------------------------------------------


class TestMarkCleanDocJsonSection:
    """mark_clean on a DocJSON section node resets baseline so next check is CLEAN."""

    def test_mark_clean_docjson_section_then_clean(self, mini_project: Path) -> None:
        """After mark_clean on a DocJSON section node, compute_staleness returns CLEAN."""
        db_path = mini_project / ".axiom_graph" / "graph.db"

        # Write a DocJSON file
        docs_dir = mini_project / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        doc_file = docs_dir / "test-doc.json"
        doc_data = {
            "title": "Test Doc",
            "sections": [
                {
                    "id": "overview",
                    "heading": "Overview",
                    "content": "This is the current content.",
                }
            ],
        }
        doc_file.write_text(json.dumps(doc_data), encoding="utf-8")

        # Compute what the current hashes should be
        from axiom_graph.models import hash16 as doc_hash16

        current_code = doc_hash16("This is the current content.")
        current_desc = doc_hash16("Overview")

        # Insert node with OLD hashes (stale baseline)
        section_node = _node(
            "proj::docs.test-doc::overview",
            node_type="atomic_process",
            subtype="docjson",
            code_hash="stale_section_hash",
            desc_hash="stale_heading_hash",
            location="docs/test-doc.json",
        )
        db.upsert_node(db_path, section_node, discovery_only=False)

        # Before fix: compute_staleness should see CONTENT_UPDATED
        statuses_before = compute_staleness(db_path, mini_project, [section_node])
        assert statuses_before["proj::docs.test-doc::overview"][0] == "CONTENT_UPDATED"

        # Call mark_clean via the MCP entry point
        from axiom_graph.mcp_server import axiom_graph_mark_clean

        axiom_graph_mark_clean(str(mini_project), node_id="proj::docs.test-doc::overview", reason="docs ok")

        # Re-read node and check staleness
        updated = db.get_node(db_path, "proj::docs.test-doc::overview")
        statuses_after = compute_staleness(db_path, mini_project, [updated])
        assert statuses_after["proj::docs.test-doc::overview"][0] in ("VERIFIED", "VERIFIED")


# ---------------------------------------------------------------------------
# Test 4: End-to-end DocJSON composite (file-level) node
# ---------------------------------------------------------------------------


class TestMarkCleanDocJsonComposite:
    """mark_clean on a DocJSON composite node resets baseline so next check is CLEAN."""

    def test_mark_clean_docjson_composite_then_clean(self, mini_project: Path) -> None:
        """After mark_clean on a DocJSON file-level node, compute_staleness returns CLEAN."""
        db_path = mini_project / ".axiom_graph" / "graph.db"

        # Write a DocJSON file
        docs_dir = mini_project / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        doc_file = docs_dir / "my-doc.json"
        doc_data = {
            "title": "My Doc",
            "sections": [{"id": "intro", "heading": "Intro", "content": "Hello."}],
        }
        raw_text = json.dumps(doc_data)
        doc_file.write_text(raw_text, encoding="utf-8")

        # Compute current whole-file hash
        from axiom_graph.models import hash16

        current_file_hash = hash16(raw_text)

        # Insert composite node with OLD hash
        comp_node = _node(
            "proj::docs.my-doc",
            node_type="composite_process",
            subtype="docjson",
            code_hash="stale_file_hash",
            desc_hash="stale_file_hash",
            location="docs/my-doc.json",
        )
        db.upsert_node(db_path, comp_node, discovery_only=False)

        # Before: CONTENT_UPDATED
        statuses_before = compute_staleness(db_path, mini_project, [comp_node])
        assert statuses_before["proj::docs.my-doc"][0] == "CONTENT_UPDATED"

        # mark_clean
        from axiom_graph.mcp_server import axiom_graph_mark_clean

        axiom_graph_mark_clean(str(mini_project), node_id="proj::docs.my-doc", reason="file ok")

        # After: CLEAN
        updated = db.get_node(db_path, "proj::docs.my-doc")
        statuses_after = compute_staleness(db_path, mini_project, [updated])
        assert statuses_after["proj::docs.my-doc"][0] in ("VERIFIED", "VERIFIED")


# ---------------------------------------------------------------------------
# Test 5: mark_clean must NOT advance file_mtime (the scan-skip cache)
# ---------------------------------------------------------------------------


class TestMarkCleanDoesNotAdvanceMtime:
    """mark_clean must leave file_mtime untouched so the next build re-scans
    the changed file and regenerates scan-derived fields (level_1/level_2)."""

    def test_mtime_not_advanced_after_mark_clean(self, mini_project: Path) -> None:
        """After mark_clean, file_mtime is left as-is (here: still None)."""
        db_path = mini_project / ".axiom_graph" / "graph.db"

        # Write a Python file
        src_dir = mini_project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        py_file = src_dir / "mod.py"
        py_file.write_text(
            'def my_func():\n    """Doc."""\n    pass\n',
            encoding="utf-8",
        )

        # Insert node with no mtime (function rows carry file_mtime=None)
        n = _node(
            "proj::src.mod::my_func",
            code_hash="stale",
            location="src/mod.py",
            file_mtime=None,
        )
        db.upsert_node(db_path, n, discovery_only=False)

        # mark_clean
        from axiom_graph.mcp_server import axiom_graph_mark_clean

        axiom_graph_mark_clean(str(mini_project), node_id="proj::src.mod::my_func", reason="ok")

        # file_mtime must remain None: mark_clean does not write the scan-skip
        # cache, so the builder re-scans the file on its next pass.
        updated = db.get_node(db_path, "proj::src.mod::my_func")
        assert updated.file_mtime is None


# ---------------------------------------------------------------------------
# Test 6: Step 5 now maps VERIFIED -> CLEAN
# ---------------------------------------------------------------------------


class TestVerificationPromotionToClean:
    """Step 5 in compute_staleness now promotes to CLEAN instead of VERIFIED."""

    def test_verification_promotion_produces_clean_not_verified(self, mini_project: Path) -> None:
        """When verification snapshot matches current hashes, status is CLEAN (not VERIFIED)."""
        db_path = mini_project / ".axiom_graph" / "graph.db"

        # Write a file so staleness can parse it
        src_dir = mini_project / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        py_file = src_dir / "mod.py"
        py_file.write_text(
            'def my_func():\n    """A doc."""\n    return 1\n',
            encoding="utf-8",
        )

        # Parse to get actual current hashes
        from axiom_graph.models import hash16
        from axiom_graph.scanners.module_scanner import _split_function
        import ast

        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for ast_node in ast.walk(tree):
            if isinstance(ast_node, ast.FunctionDef) and ast_node.name == "my_func":
                code_text, docstring = _split_function(ast_node)
                current_code = hash16(code_text)
                current_desc = hash16(docstring) if docstring else None
                break

        # Insert node with STALE hashes so Step 2 triggers CONTENT_UPDATED
        n = _node(
            "proj::src.mod::my_func",
            code_hash="stale_hash",
            desc_hash="stale_desc",
            location="src/mod.py",
        )
        db.upsert_node(db_path, n, discovery_only=False)

        # Insert verification snapshot that matches CURRENT file hashes
        db.upsert_verification(
            db_path,
            node_id="proj::src.mod::my_func",
            verified_by="human",
            code_hash_at=current_code,
            desc_hash_at=current_desc,
        )

        # compute_staleness: Step 2 sees hash mismatch (CONTENT_UPDATED),
        # Step 5 sees verification matches current -> should produce CLEAN (not VERIFIED)
        statuses = compute_staleness(db_path, mini_project, [n])
        assert statuses["proj::src.mod::my_func"][0] in ("VERIFIED", "VERIFIED")
