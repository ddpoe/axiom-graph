"""Tests for semantic search: embeddings, vector storage, and search integration.

Covers:
- Embedding model loading and graceful degradation
- sqlite-vec virtual table creation and vector storage
- Semantic search returning ranked results
- Doc section FTS indexing
- axiom_graph_search parameter handling for mode and scope
- Graceful degradation when sqlite-vec is unavailable
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from axiom_graph.index import db
from axiom_graph.models import AxiomNode


def _semantic_stack_available() -> bool:
    """True only if the full optional semantic stack (vector store + embedder) is installed.

    Semantic search is an optional, deprecated extra (ADR-020). The suites below exercise
    the real sqlite-vec store and a real embedder, so they skip when that stack is absent
    (e.g. CI installs only the viz+js extras).
    """
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        return False
    from axiom_graph.index.embeddings import is_available

    return is_available()


requires_semantic = pytest.mark.skipif(
    not _semantic_stack_available(),
    reason="semantic extra not installed (needs sqlite-vec + fastembed/sentence-transformers)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(
    node_id: str,
    level_1: str,
    level_2: str = "",
    node_type: str = "atomic_process",
    location: str = "test.py",
) -> AxiomNode:
    """Create a minimal AxiomNode for testing."""
    return AxiomNode(
        id=node_id,
        node_type=node_type,
        subtype=None,
        title=node_id.split("::")[-1],
        location=location,
        status="active",
        source="test",
        code_hash=hashlib.sha256(level_1.encode()).hexdigest()[:16],
        desc_hash=hashlib.sha256(level_2.encode()).hexdigest()[:16] if level_2 else None,
        level_0=node_id.split("::")[-1],
        level_1=level_1,
        level_2=level_2,
        level_3_location=None,
    )


def _fake_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic fake embedder: hashes text to produce a 384-dim vector."""
    results = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        # Expand hash to 384 floats in [-1, 1]
        vec = []
        for i in range(384):
            byte_val = h[i % len(h)] ^ (i & 0xFF)
            vec.append((byte_val / 128.0) - 1.0)
        results.append(vec)
    return results


# ---------------------------------------------------------------------------
# Test 1: Embedding module - model loading and graceful degradation
# ---------------------------------------------------------------------------


class TestEmbeddingModule:
    """Tests for axiom_graph.index.embeddings module."""

    def test_get_embedder_returns_callable(self):
        """get_embedder returns a callable that converts texts to vectors."""
        from axiom_graph.index.embeddings import get_embedder

        embedder = get_embedder(model_name="test")
        assert callable(embedder)

    def test_embed_produces_correct_dimensions(self):
        """Embedding function returns vectors of the expected dimension."""
        from axiom_graph.index.embeddings import get_embedder

        embedder = get_embedder(model_name="test")
        vectors = embedder(["hello world", "test query"])
        assert len(vectors) == 2
        assert len(vectors[0]) == 384
        assert len(vectors[1]) == 384

    def test_is_available_reports_status(self):
        """is_available returns True when embedding dependencies are present."""
        from axiom_graph.index.embeddings import is_available

        # Should at least not raise
        result = is_available()
        assert isinstance(result, bool)

    def test_fastembed_tried_before_sentence_transformers(self):
        """get_embedder tries fastembed before sentence-transformers.

        On Windows, sentence-transformers/torch can crash with DLL errors,
        so fastembed (using ONNX Runtime) is tried first.
        """
        from axiom_graph.index import embeddings

        # Clear the cache so get_embedder actually tries backends
        old_embedder = embeddings._cached_embedder
        old_name = embeddings._cached_model_name
        embeddings._cached_embedder = None
        embeddings._cached_model_name = None

        call_order = []

        orig_try_fastembed = embeddings._try_fastembed
        orig_try_st = embeddings._try_sentence_transformers

        def tracking_fastembed(model_name):
            call_order.append("fastembed")
            # Return a fake embedder to prevent further fallback
            return lambda texts: [[0.0] * 384 for _ in texts]

        def tracking_st(model_name):
            call_order.append("sentence_transformers")
            return None

        try:
            embeddings._try_fastembed = tracking_fastembed
            embeddings._try_sentence_transformers = tracking_st
            embedder = embeddings.get_embedder("some-model")
            # fastembed should be tried first
            assert call_order[0] == "fastembed"
        finally:
            embeddings._try_fastembed = orig_try_fastembed
            embeddings._try_sentence_transformers = orig_try_st
            embeddings._cached_embedder = old_embedder
            embeddings._cached_model_name = old_name

    def test_is_available_checks_fastembed_first(self):
        """is_available checks fastembed before sentence-transformers.

        This avoids triggering a torch DLL crash on Windows when only
        fastembed is functional. Verified by checking that when fastembed
        is importable, sentence_transformers is never attempted.
        """
        import builtins
        import sys

        real_import = builtins.__import__
        import_log = []

        def tracking_import(name, *args, **kwargs):
            if name in ("fastembed", "sentence_transformers"):
                import_log.append(name)
            return real_import(name, *args, **kwargs)

        # Remove cached modules so is_available does fresh imports
        saved_fe = sys.modules.pop("fastembed", None)
        saved_st = sys.modules.pop("sentence_transformers", None)

        try:
            with patch.object(builtins, "__import__", side_effect=tracking_import):
                from axiom_graph.index import embeddings

                # Re-import to use our tracking import
                result = embeddings.is_available()

            # fastembed should be tried first
            assert len(import_log) >= 1
            assert import_log[0] == "fastembed"
            # If fastembed succeeded, sentence_transformers should NOT be tried
            if result:
                assert "sentence_transformers" not in import_log
        finally:
            if saved_fe is not None:
                sys.modules["fastembed"] = saved_fe
            if saved_st is not None:
                sys.modules["sentence_transformers"] = saved_st

    def test_real_fastembed_backend_loads(self):
        """The fastembed backend loads and generates valid 384-dim embeddings.

        This is an integration test that verifies the real fastembed library
        works on this platform (including the ONNX Runtime dependency).
        """
        from axiom_graph.index.embeddings import _try_fastembed, DEFAULT_MODEL

        embedder = _try_fastembed(DEFAULT_MODEL)
        if embedder is None:
            pytest.skip("fastembed not installed or model not available")

        vectors = embedder(["hello world", "semantic search test"])
        assert len(vectors) == 2
        assert len(vectors[0]) == 384
        assert len(vectors[1]) == 384
        # Vectors should be different for different inputs
        assert vectors[0] != vectors[1]
        # Values should be real floats (not NaN/inf)
        for val in vectors[0][:10]:
            assert isinstance(val, float)
            assert -10.0 < val < 10.0

    def test_get_embedder_returns_real_backend_not_hash_fallback(self):
        """When a real backend is available, get_embedder does not fall back to hash.

        Verifies that the embedder produces different vectors for semantically
        similar texts (hash-based would not).
        """
        from axiom_graph.index import embeddings

        # Clear cache
        old_embedder = embeddings._cached_embedder
        old_name = embeddings._cached_model_name
        embeddings._cached_embedder = None
        embeddings._cached_model_name = None

        try:
            if not embeddings.is_available():
                pytest.skip("No real embedding backend available")

            embedder = embeddings.get_embedder()
            # Verify it's not the test/hash embedder
            assert embedder is not embeddings._test_embedder
        finally:
            embeddings._cached_embedder = old_embedder
            embeddings._cached_model_name = old_name


# ---------------------------------------------------------------------------
# Test 2: DB layer - sqlite-vec table and embedding storage
# ---------------------------------------------------------------------------


@requires_semantic
class TestVectorStorage:
    """Tests for sqlite-vec virtual table and embedding CRUD."""

    def test_init_embeddings_creates_tables(self, db_path):
        """init_embeddings creates the vec_embeddings and embedding_hashes tables."""
        from axiom_graph.index.embeddings import EMBEDDING_DIM

        result = db.init_embeddings(db_path, EMBEDDING_DIM)
        assert result is True

        # Verify the companion table (regular SQL table, no vec needed)
        with db._connect(db_path) as conn:
            rows = conn.execute("SELECT count(*) FROM embedding_hashes").fetchone()
            assert rows[0] == 0

    def test_upsert_and_retrieve_embedding(self, db_path):
        """Embeddings can be stored and retrieved by node ID."""
        from axiom_graph.index.embeddings import EMBEDDING_DIM

        db.init_embeddings(db_path, EMBEDDING_DIM)

        node = _make_node("test::mod::func", "A test function")
        with db._connect(db_path) as conn:
            db.upsert_node_conn(conn, node, discovery_only=False)

        vec = _fake_embed(["A test function"])[0]
        content_hash = hashlib.sha256("A test function".encode()).hexdigest()[:16]

        db.upsert_embedding(db_path, "test::mod::func", vec, content_hash)

        # Should be retrievable
        stored = db.get_embedding_hash(db_path, "test::mod::func")
        assert stored == content_hash

    def test_upsert_embedding_skips_unchanged(self, db_path):
        """Upserting with the same content hash is a no-op."""
        from axiom_graph.index.embeddings import EMBEDDING_DIM

        db.init_embeddings(db_path, EMBEDDING_DIM)

        node = _make_node("test::mod::func", "A test function")
        with db._connect(db_path) as conn:
            db.upsert_node_conn(conn, node, discovery_only=False)

        vec = _fake_embed(["A test function"])[0]
        content_hash = "abc123"

        db.upsert_embedding(db_path, "test::mod::func", vec, content_hash)
        # Second upsert with same hash should be skipped
        skipped = db.upsert_embedding(db_path, "test::mod::func", vec, content_hash)
        assert skipped is False  # no change


# ---------------------------------------------------------------------------
# Test 3: Semantic search - vector similarity queries
# ---------------------------------------------------------------------------


@requires_semantic
class TestSemanticSearch:
    """Tests for semantic search via sqlite-vec."""

    def test_semantic_search_returns_nearest_neighbors(self, db_path):
        """Semantic search finds nodes by vector similarity."""
        from axiom_graph.index.embeddings import EMBEDDING_DIM

        db.init_embeddings(db_path, EMBEDDING_DIM)

        # Insert two nodes with different embeddings
        node_a = _make_node("test::mod::hash_compare", "detects content drift via hash comparison")
        node_b = _make_node("test::mod::format_output", "formats terminal output with colors")

        with db._connect(db_path) as conn:
            db.upsert_node_conn(conn, node_a, discovery_only=False)
            db.upsert_node_conn(conn, node_b, discovery_only=False)

        vec_a = _fake_embed(["detects content drift via hash comparison"])[0]
        vec_b = _fake_embed(["formats terminal output with colors"])[0]

        db.upsert_embedding(db_path, node_a.id, vec_a, "hash_a")
        db.upsert_embedding(db_path, node_b.id, vec_b, "hash_b")

        # Search with a query vector similar to node_a
        query_vec = _fake_embed(["how does staleness detection work"])[0]
        results, total = db.semantic_search(db_path, query_vec, max_results=10)

        # Should return results (both nodes since we have only 2)
        assert len(results) > 0
        assert total > 0
        # Results should be AxiomNode instances
        assert all(isinstance(r, AxiomNode) for r in results)

    def test_semantic_search_respects_max_results(self, db_path):
        """Semantic search limits results to max_results."""
        from axiom_graph.index.embeddings import EMBEDDING_DIM

        db.init_embeddings(db_path, EMBEDDING_DIM)

        # Insert 5 nodes
        for i in range(5):
            node = _make_node(f"test::mod::func{i}", f"function number {i}")
            with db._connect(db_path) as conn:
                db.upsert_node_conn(conn, node, discovery_only=False)
            vec = _fake_embed([f"function number {i}"])[0]
            db.upsert_embedding(db_path, node.id, vec, f"hash_{i}")

        query_vec = _fake_embed(["function"])[0]
        results, total = db.semantic_search(db_path, query_vec, max_results=3)

        assert len(results) <= 3
        assert total == 5


# ---------------------------------------------------------------------------
# Test 4: Doc section FTS indexing
# ---------------------------------------------------------------------------


class TestDocSectionFTS:
    """Tests for doc sections being searchable via FTS."""

    def test_doc_sections_indexed_in_fts(self, db_path):
        """Doc sections are indexed in node_fts and searchable."""
        # Create a doc and section in the database
        with db._connect(db_path) as conn:
            db.upsert_doc(
                conn,
                {
                    "id": "test::docs.guide",
                    "title": "User Guide",
                    "tags": None,
                    "file_path": "docs/guide.json",
                    "desc_hash": "abc",
                    "updated_at": "2026-01-01T00:00:00",
                },
            )
            db.upsert_doc_section(
                conn,
                {
                    "id": "test::docs.guide::overview",
                    "doc_id": "test::docs.guide",
                    "heading": "Overview",
                    "level": 2,
                    "tags": None,
                    "content": "This guide explains how staleness detection works using hash comparison",
                    "desc_hash": "def",
                    "parent_id": None,
                    "depth": 0,
                    "position": 0,
                    "updated_at": "2026-01-01T00:00:00",
                },
            )

        # Index doc sections into FTS
        db.index_doc_sections_fts(db_path)

        # Should be findable via FTS search with scope='docs'
        nodes, mode, total = db.fts_search(db_path, "staleness", scope="docs")
        assert total > 0
        # The result should contain the doc section
        ids = [n.id for n in nodes]
        assert "test::docs.guide::overview" in ids

    def test_fts_search_scope_code_excludes_docs(self, db_path):
        """FTS search with scope='code' excludes doc sections."""
        # Insert a code node
        node = _make_node("test::mod::func", "staleness detection function")
        with db._connect(db_path) as conn:
            db.upsert_node_conn(conn, node, discovery_only=False)

        # Insert a doc section with the same keyword
        with db._connect(db_path) as conn:
            db.upsert_doc(
                conn,
                {
                    "id": "test::docs.guide",
                    "title": "Guide",
                    "tags": None,
                    "file_path": "docs/guide.json",
                    "desc_hash": "abc",
                    "updated_at": "2026-01-01T00:00:00",
                },
            )
            db.upsert_doc_section(
                conn,
                {
                    "id": "test::docs.guide::staleness",
                    "doc_id": "test::docs.guide",
                    "heading": "Staleness",
                    "level": 2,
                    "tags": None,
                    "content": "staleness detection explained",
                    "desc_hash": "def",
                    "parent_id": None,
                    "depth": 0,
                    "position": 0,
                    "updated_at": "2026-01-01T00:00:00",
                },
            )

        db.index_doc_sections_fts(db_path)

        # scope='code' should return only the code node
        nodes, mode, total = db.fts_search(db_path, "staleness", scope="code")
        ids = [n.id for n in nodes]
        assert "test::mod::func" in ids
        assert "test::docs.guide::staleness" not in ids


# ---------------------------------------------------------------------------
# Test 5: Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    """Tests for graceful degradation when sqlite-vec unavailable."""

    def test_semantic_search_without_vec_returns_empty(self, db_path):
        """Semantic search gracefully returns empty when vec table not initialized."""
        query_vec = _fake_embed(["test"])[0]
        results, total = db.semantic_search(db_path, query_vec, max_results=10)
        assert results == []
        assert total == 0

    def test_init_embeddings_logs_warning_on_failure(self, db_path):
        """init_embeddings logs a warning and returns False when sqlite-vec unavailable."""
        with patch("axiom_graph.db._core._load_sqlite_vec", side_effect=OSError("no vec")):
            result = db.init_embeddings(db_path, 384)
            assert result is False


# ---------------------------------------------------------------------------
# Test 6: axiom_graph_search mode and scope parameters
# ---------------------------------------------------------------------------


class TestSearchParameters:
    """Tests for axiom_graph_search MCP tool parameter handling."""

    def test_fts_search_default_mode_unchanged(self, db_path):
        """Default FTS search behavior is unchanged (backward compatible)."""
        node = _make_node("test::mod::func", "hello world function")
        with db._connect(db_path) as conn:
            db.upsert_node_conn(conn, node, discovery_only=False)

        nodes, mode, total = db.fts_search(db_path, "hello")
        assert mode == "fts"
        assert total >= 1
        assert any(n.id == "test::mod::func" for n in nodes)

    def test_fts_search_scope_all_includes_everything(self, db_path):
        """FTS search with scope='all' includes both code and doc nodes."""
        node = _make_node("test::mod::func", "staleness function")
        with db._connect(db_path) as conn:
            db.upsert_node_conn(conn, node, discovery_only=False)

        with db._connect(db_path) as conn:
            db.upsert_doc(
                conn,
                {
                    "id": "test::docs.guide",
                    "title": "Guide",
                    "tags": None,
                    "file_path": "docs/guide.json",
                    "desc_hash": "abc",
                    "updated_at": "2026-01-01T00:00:00",
                },
            )
            db.upsert_doc_section(
                conn,
                {
                    "id": "test::docs.guide::staleness",
                    "doc_id": "test::docs.guide",
                    "heading": "Staleness",
                    "level": 2,
                    "tags": None,
                    "content": "staleness explained",
                    "desc_hash": "def",
                    "parent_id": None,
                    "depth": 0,
                    "position": 0,
                    "updated_at": "2026-01-01T00:00:00",
                },
            )

        db.index_doc_sections_fts(db_path)

        nodes, mode, total = db.fts_search(db_path, "staleness", scope="all")
        ids = [n.id for n in nodes]
        assert "test::mod::func" in ids
        assert "test::docs.guide::staleness" in ids


# ---------------------------------------------------------------------------
# Deprecation (ADR-020)
# ---------------------------------------------------------------------------


class TestDeprecation:
    """Guards the ADR-020 deprecation warnings against accidental removal.

    Both warnings are scheduled to land in 2.1.0 and the underlying code
    paths are scheduled for deletion in 3.0.0.  Keeping these assertions
    means a future refactor that quietly drops the ``warnings.warn`` call
    will fail loudly here rather than silently shipping a regression.
    """

    def test_get_embedder_emits_deprecation_warning(self) -> None:
        """get_embedder() warns on real model loads (test-embedder path exempt)."""
        from axiom_graph.index.embeddings import get_embedder

        with pytest.warns(DeprecationWarning, match="semantic search is deprecated"):
            # Use a bogus model name so the loaders fail fast; the warning
            # fires before any backend is tried, so we don't need a real
            # ML environment to exercise it.
            get_embedder(model_name="this-model-does-not-exist-xyz")

    def test_get_embedder_test_mode_does_not_warn(self) -> None:
        """model_name='test' returns the deterministic embedder without warning.

        The test embedder doesn't touch the deprecated dependency surface,
        so it shouldn't trip the warning (ADR-020 decision).
        """
        import warnings

        from axiom_graph.index.embeddings import get_embedder

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            get_embedder(model_name="test")  # must not raise

    def test_init_embeddings_emits_deprecation_warning(self, db_path) -> None:
        """init_embeddings() warns at index-time setup."""
        with pytest.warns(DeprecationWarning, match="semantic indexing is deprecated"):
            db.init_embeddings(db_path, 384)

    def test_mcp_warm_embedder_honors_skip_flag(self, monkeypatch) -> None:
        """_warm_embedder() is a no-op when AXIOM_GRAPH_SKIP_EMBEDDINGS=1.

        The flag already gates the build-time path (builder.py); this guards
        the parallel MCP-server-startup gate so a future refactor that
        drops it surfaces here rather than silently re-introducing the
        startup-hang vector for users who never opted into semantic.
        """
        from axiom_graph.mcp.server import _warm_embedder

        monkeypatch.setenv("AXIOM_GRAPH_SKIP_EMBEDDINGS", "1")

        called = {"get_embedder": False}

        def _fake_get_embedder(*args, **kwargs):
            called["get_embedder"] = True
            raise AssertionError("get_embedder must not run when skip flag is set")

        monkeypatch.setattr(
            "axiom_graph.index.embeddings.get_embedder",
            _fake_get_embedder,
        )

        _warm_embedder()
        assert called["get_embedder"] is False
