"""Embedding model abstraction for semantic search.

Provides a lazy-loading embedding function that converts text to dense vectors.
Supports fastembed (preferred) and sentence-transformers backends, with graceful
degradation when neither is available.

Backend priority: fastembed is tried first because it uses ONNX Runtime (which
has broad platform compatibility) while sentence-transformers depends on PyTorch
(which can fail with DLL loading errors on some Windows configurations).

Entry points:
    get_embedder(model_name) -> Callable[[list[str]], list[list[float]]]
    is_available() -> bool
"""

from __future__ import annotations

import hashlib
import logging
import warnings
from typing import Callable

logger = logging.getLogger(__name__)

# Embedding dimension for all-MiniLM-L6-v2 (and compatible models)
EMBEDDING_DIM = 384

# Default model name
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Cached model instance (lazy loaded)
_cached_embedder: Callable[[list[str]], list[list[float]]] | None = None
_cached_model_name: str | None = None


def _test_embedder(texts: list[str]) -> list[list[float]]:
    """Deterministic test embedder: hashes text to produce a 384-dim vector.

    Used when model_name='test' or when no real model is available.
    Produces consistent vectors for the same input text.
    """
    results = []
    for text in texts:
        h = hashlib.sha256(text.encode()).digest()
        vec = []
        for i in range(EMBEDDING_DIM):
            byte_val = h[i % len(h)] ^ (i & 0xFF)
            vec.append((byte_val / 128.0) - 1.0)
        results.append(vec)
    return results


def _try_sentence_transformers(model_name: str) -> Callable[[list[str]], list[list[float]]] | None:
    """Try to load a sentence-transformers model.

    Returns:
        Embedding function, or None if sentence-transformers is not available.
    """
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]

        logger.info("sentence-transformers: loading model %s (may download on first use)", model_name)
        model = SentenceTransformer(model_name)
        logger.info("sentence-transformers: model ready")

        def embed(texts: list[str]) -> list[list[float]]:
            arr = model.encode(texts, show_progress_bar=False)
            return arr.tolist()

        return embed
    except Exception as exc:
        logger.debug("sentence-transformers unavailable: %s", exc)
        return None


def _try_fastembed(model_name: str) -> Callable[[list[str]], list[list[float]]] | None:
    """Try to load a fastembed model.

    Returns:
        Embedding function, or None if fastembed is not available.
    """
    try:
        from fastembed import TextEmbedding  # type: ignore[import-untyped]

        logger.info("fastembed: loading model %s (may download on first use)", model_name)
        model = TextEmbedding(model_name)
        logger.info("fastembed: model ready")

        def embed(texts: list[str]) -> list[list[float]]:
            return [v.tolist() for v in model.embed(texts)]

        return embed
    except Exception as exc:
        logger.debug("fastembed unavailable: %s", exc)
        return None


def get_embedder(
    model_name: str = DEFAULT_MODEL,
) -> Callable[[list[str]], list[list[float]]]:
    """Return an embedding function that converts texts to dense vectors.

    Tries fastembed first (ONNX Runtime, best cross-platform compatibility),
    then sentence-transformers (PyTorch), then falls back to a deterministic
    hash-based embedder (for testing or when no ML backend is available).

    The model is lazily loaded on first call and cached for subsequent calls.
    If model_name is 'test', always returns the deterministic test embedder.

    Args:
        model_name: Model identifier. Use 'test' for deterministic test vectors.

    Returns:
        A callable that takes a list of strings and returns a list of float vectors.
    """
    global _cached_embedder, _cached_model_name

    if model_name == "test":
        return _test_embedder

    warnings.warn(
        "axiom-graph semantic search is deprecated and scheduled for removal in 3.0. "
        "Use keyword search (mode='keyword') or layer external semantic tooling "
        "against the exported graph DB. See ADR-020.",
        DeprecationWarning,
        stacklevel=2,
    )

    if _cached_embedder is not None and _cached_model_name == model_name:
        return _cached_embedder

    # Try real backends in order of preference:
    # 1. fastembed (ONNX Runtime) - best cross-platform support, no PyTorch DLL issues
    # 2. sentence-transformers (PyTorch) - may fail on some Windows configurations
    logger.debug("get_embedder: trying fastembed backend")
    embedder = _try_fastembed(model_name)
    if embedder is None:
        logger.debug("get_embedder: fastembed unavailable, trying sentence-transformers")
        embedder = _try_sentence_transformers(model_name)

    if embedder is None:
        logger.warning(
            "No embedding backend available (fastembed, sentence-transformers). "
            "Semantic search will use hash-based fallback vectors."
        )
        embedder = _test_embedder

    _cached_embedder = embedder
    _cached_model_name = model_name
    return embedder


def is_available() -> bool:
    """Check if a real embedding backend is available.

    Checks fastembed first (ONNX Runtime) to avoid triggering PyTorch DLL
    loading issues on Windows when sentence-transformers is installed but
    its torch dependency is broken.

    Returns:
        True if fastembed or sentence-transformers can be imported successfully.
    """
    try:
        import fastembed  # type: ignore[import-untyped]  # noqa: F401

        return True
    except Exception:
        pass
    try:
        import sentence_transformers  # type: ignore[import-untyped]  # noqa: F401

        return True
    except Exception:
        pass
    return False


def content_hash_for_embedding(level_1: str, level_2: str | None = None) -> str:
    """Compute a content hash for embedding skip-detection.

    The hash covers the text that gets embedded (level_1 + level_2 for code nodes,
    heading + content for doc sections). If this hash matches the stored hash,
    the embedding can be skipped.

    Args:
        level_1: Primary text (summary or heading).
        level_2: Secondary text (docstring or content), may be None.

    Returns:
        A hex digest string.
    """
    combined = (level_1 or "") + "\n" + (level_2 or "")
    return hashlib.sha256(combined.encode()).hexdigest()[:16]
