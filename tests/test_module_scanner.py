"""Unit tests for module_scanner — name_map enrichment, import resolution and link edges.

Mostly Tier 1: plain pytest exercising internal scanner logic directly, and
expected to change as the scanner evolves.  Import resolution across a
package boundary is a subsystem contract and is annotated Tier 2.
"""

from __future__ import annotations

from pathlib import Path

from axiom_annotations import workflow

from axiom_graph.scanners.module_scanner import scan_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
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


# ---------------------------------------------------------------------------
# Relative imports inside a package's own __init__.py
# ---------------------------------------------------------------------------


@workflow(
    purpose="Relative imports inside a package's __init__.py resolve against the package itself in every scope, never against a same-named module one level up",
)
def test_relative_imports_in_a_package_init_resolve_against_the_package_itself(tmp_path):
    for name, func in (("resolve", "f"), ("core", "g"), ("guarded", "h"), ("lazy", "k")):
        # The decoy one level up carries the same module name as the real one.
        _write(tmp_path / "pkg" / f"{name}.py", f"def {func}(): pass\n")
        _write(tmp_path / "pkg" / "sub" / f"{name}.py", f"def {func}(): pass\n")
    _write(tmp_path / "pkg" / "__init__.py", "")
    init = _write(
        tmp_path / "pkg" / "sub" / "__init__.py",
        """\
from .resolve import f
from .core import *

try:
    from .guarded import h
except ImportError:
    h = None


def load():
    from .lazy import k

    return k()
""",
    )

    _, edges = scan_module(init, tmp_path, "proj")

    module_deps = {e.to_id: e for e in _edges_by_type(edges, "depends_on") if e.from_id == "proj::pkg.sub"}
    for name in ("resolve", "core", "guarded", "lazy"):
        assert f"proj::pkg.sub.{name}" in module_deps, f"missing dependency on pkg.sub.{name}: {sorted(module_deps)}"
        assert f"proj::pkg.{name}" not in module_deps, f"relative import captured by the decoy pkg.{name}"
    assert (module_deps["proj::pkg.sub.core"].meta or {}).get("reexport") == "star"
    # The per-function binding overlay reads the same base.
    assert any(
        e.from_id == "proj::pkg.sub::load" and e.to_id == "proj::pkg.sub.lazy"
        for e in _edges_by_type(edges, "depends_on")
    )


def test_relative_import_in_a_project_root_init_resolves_from_the_root(tmp_path):
    """A project-root ``__init__.py`` has no package above it to anchor on."""
    _write(tmp_path / "helpers.py", "def f(): pass\n")
    init = _write(tmp_path / "__init__.py", "from .helpers import f\n")

    _, edges = scan_module(init, tmp_path, "proj")

    assert any(e.to_id == "proj::helpers" for e in _edges_by_type(edges, "depends_on"))


# ---------------------------------------------------------------------------
# Re-export markers on module-level dependency edges
# ---------------------------------------------------------------------------


def test_module_level_from_imports_record_named_reexport_markers(tmp_path):
    """Each module-level ``from X import name`` records bound → original on the edge to X.

    A star import of the same source shares the edge, a name that is itself
    a submodule is a namespace binding rather than a re-export, and an
    import under ``if TYPE_CHECKING:`` binds nothing at runtime.
    """
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "impl.py", "def func(): pass\ndef real(): pass\n")
    _write(tmp_path / "pkg" / "stars.py", "def s(): pass\n")
    _write(tmp_path / "pkg" / "submod.py", "def m(): pass\n")
    _write(tmp_path / "pkg" / "typed.py", "def t(): pass\n")
    api = _write(
        tmp_path / "pkg" / "api.py",
        """\
from typing import TYPE_CHECKING

from .impl import func, real as alias
from .stars import *
from .stars import s
from pkg import submod

if TYPE_CHECKING:
    from .typed import t
""",
    )

    _, edges = scan_module(api, tmp_path, "proj")

    meta_by_target = {e.to_id: e.meta for e in _edges_by_type(edges, "depends_on") if e.from_id == "proj::pkg.api"}
    assert meta_by_target["proj::pkg.impl"] == {"reexport_names": {"func": "func", "alias": "real"}}
    assert meta_by_target["proj::pkg.stars"] == {"reexport": "star", "reexport_names": {"s": "s"}}
    assert meta_by_target["proj::pkg.submod"] is None
    assert meta_by_target["proj::pkg"] is None
    assert meta_by_target["proj::pkg.typed"] is None


# ---------------------------------------------------------------------------
# Attribute-chain targets the binding does not spell
# ---------------------------------------------------------------------------

_MARKER_MATRIX_IMPORTS = """\
from axiom_annotations import AutoStep, workflow

import pkg
import pkg.sub as aliased_sub
from pkg import mod
from pkg.mod import Thing, f
import nothere
"""

_MARKER_MATRIX_TESTS = (
    _MARKER_MATRIX_IMPORTS
    + """

def test_direct_call():
    f()


def test_attribute_call_on_a_module():
    mod.f()


def test_member_call():
    Thing.method()


def test_chain_through_a_package():
    pkg.sub.func()


def test_chain_through_a_member():
    Thing.attr.method()


def test_chain_through_an_aliased_dotted_import():
    aliased_sub.inner.func()


def test_chain_the_dotted_import_spells():
    import pkg.sub

    pkg.sub.func()


def test_dotted_import_called_off_its_root():
    import pkg.sub

    pkg.func()


def test_same_target_spelled_and_chained():
    pkg.sub.func()
    pkg.func()


def test_unresolvable_module():
    nothere.sub.func()
"""
)

_MARKER_MATRIX_FLOW = (
    _MARKER_MATRIX_IMPORTS
    + """

class Flow:
    @workflow(purpose="Delegate through every receiver shape")
    def run(self):
        口 = AutoStep(step_num=1, name="Direct")
        f()
        口 = AutoStep(step_num=2, name="Module attribute")
        mod.f()
        口 = AutoStep(step_num=3, name="Chain through a package")
        pkg.sub.func()
        口 = AutoStep(step_num=4, name="Sibling method")
        self.helper()
        口 = AutoStep(step_num=5, name="Collaborator method")
        self.sink.flush()

    def helper(self):
        return None
"""
)


def test_attribute_chains_the_binding_does_not_spell_are_marked(tmp_path):
    """A target built by dropping chain components the import never spelled is flagged.

    ``import a.b`` bound as ``a`` spells ``b``; every other binding spells
    nothing.  The mark applies identically to test links and delegate
    links, and a duplicate target reached both ways keeps the unmarked form.
    """
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "sub" / "__init__.py", "")
    _write(tmp_path / "pkg" / "mod.py", "def f(): pass\n\n\nclass Thing:\n    def method(self): pass\n")
    tests_file = _write(tmp_path / "test_matrix.py", _MARKER_MATRIX_TESTS)
    flow_file = _write(tmp_path / "flow.py", _MARKER_MATRIX_FLOW)

    _, test_edges = scan_module(tests_file, tmp_path, "proj")
    _, flow_edges = scan_module(flow_file, tmp_path, "proj")

    def marked(edge):
        return bool((edge.meta or {}).get("unspelled_chain"))

    validates = {e.from_id.rsplit("::", 1)[-1]: (e.to_id, marked(e)) for e in _edges_by_type(test_edges, "validates")}
    assert validates == {
        "test_direct_call": ("proj::pkg.mod::f", False),
        "test_attribute_call_on_a_module": ("proj::pkg.mod::f", False),
        "test_member_call": ("proj::pkg.mod::Thing.method", False),
        "test_chain_through_a_package": ("proj::pkg::func", True),
        "test_chain_through_a_member": ("proj::pkg.mod::Thing.method", True),
        "test_chain_through_an_aliased_dotted_import": ("proj::pkg.sub::func", True),
        "test_chain_the_dotted_import_spells": ("proj::pkg.sub::func", False),
        "test_dotted_import_called_off_its_root": ("proj::pkg.sub::func", True),
        "test_same_target_spelled_and_chained": ("proj::pkg::func", False),
    }

    delegates = {
        e.from_id.rsplit("::", 1)[-1]: (e.to_id, marked(e)) for e in _edges_by_type(flow_edges, "delegates_to")
    }
    assert delegates == {
        "step-1": ("proj::pkg.mod::f", False),
        "step-2": ("proj::pkg.mod::f", False),
        "step-3": ("proj::pkg::func", True),
        "step-4": ("proj::flow::Flow.helper", False),
    }
