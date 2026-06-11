"""Unit tests for module_scanner — name_map enrichment and validates edges.

Tier 1: plain pytest. These tests exercise internal scanner logic directly
and are expected to change as the scanner evolves.
"""

from __future__ import annotations

from pathlib import Path


from axiom_graph.scanners.module_scanner import scan_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _edges_by_type(edges, edge_type):
    return [e for e in edges if e.edge_type == edge_type]


# ---------------------------------------------------------------------------
# name_map: import style binding tests
# ---------------------------------------------------------------------------


def test_name_map_whole_module_binding_produces_no_validates(tmp_path):
    """import mod → name_map has (module_id, None); attribute calls produce validates edges."""
    _write(tmp_path / "mymod.py", "def my_func(): pass\n")
    test_src = """\
import mymod

def test_x():
    mymod.my_func()
"""
    test_file = _write(tmp_path / "test_foo.py", test_src)
    _, edges = scan_module(test_file, tmp_path, "proj")
    validates = _edges_by_type(edges, "validates")
    # attribute call: mymod.my_func() → proj::mymod::my_func
    assert any(e.to_id == "proj::mymod::my_func" for e in validates)


def test_name_map_from_import_binding_produces_validates(tmp_path):
    """from mod import func → direct call func() produces validates edge to mod::func."""
    _write(tmp_path / "mymod.py", "def my_func(): pass\n")
    test_src = """\
from mymod import my_func

def test_x():
    my_func()
"""
    test_file = _write(tmp_path / "test_foo.py", test_src)
    _, edges = scan_module(test_file, tmp_path, "proj")
    validates = _edges_by_type(edges, "validates")
    assert any(e.to_id == "proj::mymod::my_func" for e in validates)


def test_name_map_alias_preserves_original_name(tmp_path):
    """from mod import func as f → f() resolves to mod::func, not mod::f."""
    _write(tmp_path / "mymod.py", "def my_func(): pass\n")
    test_src = """\
from mymod import my_func as f

def test_x():
    f()
"""
    test_file = _write(tmp_path / "test_foo.py", test_src)
    _, edges = scan_module(test_file, tmp_path, "proj")
    validates = _edges_by_type(edges, "validates")
    assert any(e.to_id == "proj::mymod::my_func" for e in validates)
    assert not any(e.to_id == "proj::mymod::f" for e in validates)


# ---------------------------------------------------------------------------
# validates edges: emission rules
# ---------------------------------------------------------------------------


def test_validates_not_emitted_for_non_test_function(tmp_path):
    """A non-test function calling an indexed function must NOT produce validates edges."""
    _write(tmp_path / "mymod.py", "def my_func(): pass\n")
    src = """\
from mymod import my_func

def helper():
    my_func()
"""
    f = _write(tmp_path / "utils.py", src)
    _, edges = scan_module(f, tmp_path, "proj")
    assert not _edges_by_type(edges, "validates")


def test_validates_not_emitted_for_stdlib_calls(tmp_path):
    """Calls to stdlib (os, json, etc.) must never produce validates edges."""
    src = """\
import os

def test_x():
    os.path.join("a", "b")
"""
    f = _write(tmp_path / "test_foo.py", src)
    _, edges = scan_module(f, tmp_path, "proj")
    assert not _edges_by_type(edges, "validates")


def test_validates_not_emitted_for_third_party_calls(tmp_path):
    """Calls to third-party packages not under project_root must not produce edges."""
    src = """\
import pytest

def test_x():
    pytest.raises(ValueError)
"""
    f = _write(tmp_path / "test_foo.py", src)
    _, edges = scan_module(f, tmp_path, "proj")
    assert not _edges_by_type(edges, "validates")


def test_validates_from_test_file_convention(tmp_path):
    """Functions in test_*.py files are tagged test and get validates edges."""
    _write(tmp_path / "mymod.py", "def target(): pass\n")
    src = """\
from mymod import target

def test_calls_target():
    target()
"""
    f = _write(tmp_path / "test_foo.py", src)
    _, edges = scan_module(f, tmp_path, "proj")
    validates = _edges_by_type(edges, "validates")
    assert any(e.from_id == "proj::test_foo::test_calls_target" and e.to_id == "proj::mymod::target" for e in validates)


def test_validates_multiple_calls_in_one_test(tmp_path):
    """A test calling two different indexed functions gets two validates edges."""
    _write(tmp_path / "mymod.py", "def func_a(): pass\ndef func_b(): pass\n")
    src = """\
from mymod import func_a, func_b

def test_both():
    func_a()
    func_b()
"""
    f = _write(tmp_path / "test_foo.py", src)
    _, edges = scan_module(f, tmp_path, "proj")
    validates = _edges_by_type(edges, "validates")
    to_ids = {e.to_id for e in validates}
    assert "proj::mymod::func_a" in to_ids
    assert "proj::mymod::func_b" in to_ids


def test_depends_on_still_emitted_alongside_validates(tmp_path):
    """Enriching name_map must not break existing depends_on edge emission."""
    _write(tmp_path / "mymod.py", "def my_func(): pass\n")
    src = """\
from mymod import my_func

def test_x():
    my_func()
"""
    f = _write(tmp_path / "test_foo.py", src)
    _, edges = scan_module(f, tmp_path, "proj")
    depends = _edges_by_type(edges, "depends_on")
    # module-level depends_on: test_foo → mymod
    assert any(e.from_id == "proj::test_foo" and e.to_id == "proj::mymod" for e in depends)
