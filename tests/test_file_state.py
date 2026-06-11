"""Tests for the ``file_unchanged_since`` mtime fast-pass primitive.

Covers the three-way contract (never-indexed, unchanged, changed) and the
explicit-slop policy decided in cycle pev-2026-06-11-staleness-content-hash-gate
(D-B1: slop defaults to 0.0 / exact comparison; no site carries tolerance).
"""

from __future__ import annotations

from axiom_graph.index.file_state import file_unchanged_since


def test_stored_none_is_always_changed():
    """A file with no stored mtime (never indexed) is always treated as changed."""
    assert file_unchanged_since(None, 100.0) is False
    # Even with a generous slop, None short-circuits to "changed".
    assert file_unchanged_since(None, 100.0, slop=1000.0) is False


def test_equal_mtime_is_unchanged():
    """Identical mtimes -> unchanged (the <= boundary is inclusive)."""
    assert file_unchanged_since(100.0, 100.0) is True


def test_older_current_mtime_is_unchanged():
    """Current mtime below the stored value -> unchanged."""
    assert file_unchanged_since(100.0, 50.0) is True


def test_newer_current_mtime_is_changed():
    """Current mtime past the stored value (no slop) -> changed."""
    assert file_unchanged_since(100.0, 100.001) is False
    assert file_unchanged_since(100.0, 200.0) is False


def test_default_slop_is_zero_exact_comparison():
    """Default slop is 0.0: even a tiny advance counts as changed (Option A)."""
    # Without the historical +0.01 tolerance, this is changed.
    assert file_unchanged_since(100.0, 100.005) is False


def test_explicit_slop_widens_the_window():
    """An explicit slop keyword admits mtimes within the tolerance band."""
    assert file_unchanged_since(100.0, 100.005, slop=0.01) is True
    # Just past the band is still changed.
    assert file_unchanged_since(100.0, 100.02, slop=0.01) is False
