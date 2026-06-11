"""Unit tests for Python AST scanner behaviour — 9 coverage gaps.

Calls scan_module / scan_single_json_doc directly without going through
the full build pipeline.

Tier 1: plain pytest (tests 1-7, 9).
Tier 2: @workflow(purpose=...) (test 8 — dFlow decorator detection).
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom_annotations import workflow

from axiom_graph.docjson.parse import scan_single_json_doc
from axiom_graph.scanners.module_scanner import scan_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _node(nodes, node_id):
    return next((n for n in nodes if n.id == node_id), None)


def _edges(edges, edge_type):
    return [e for e in edges if e.edge_type == edge_type]


# ---------------------------------------------------------------------------
# 1. code_hash / desc_hash separation
# ---------------------------------------------------------------------------


def test_code_hash_stable_when_only_docstring_changes(tmp_path):
    """Changing only the docstring changes desc_hash but not code_hash."""
    f = _write(
        tmp_path / "mymod.py",
        '''\
def my_func():
    """Original docstring."""
    return 42
''',
    )
    nodes_v1, _ = scan_module(f, tmp_path, "proj")
    func_v1 = _node(nodes_v1, "proj::mymod::my_func")

    f.write_text('''\
def my_func():
    """Updated docstring — completely different."""
    return 42
''')
    nodes_v2, _ = scan_module(f, tmp_path, "proj")
    func_v2 = _node(nodes_v2, "proj::mymod::my_func")

    assert func_v1 is not None and func_v2 is not None
    assert func_v1.code_hash == func_v2.code_hash
    assert func_v1.desc_hash != func_v2.desc_hash


def test_desc_hash_stable_when_only_body_changes(tmp_path):
    """Changing only the function body changes code_hash but not desc_hash."""
    f = _write(
        tmp_path / "mymod.py",
        '''\
def my_func():
    """Stable docstring."""
    return 42
''',
    )
    nodes_v1, _ = scan_module(f, tmp_path, "proj")
    func_v1 = _node(nodes_v1, "proj::mymod::my_func")

    f.write_text('''\
def my_func():
    """Stable docstring."""
    return 99
''')
    nodes_v2, _ = scan_module(f, tmp_path, "proj")
    func_v2 = _node(nodes_v2, "proj::mymod::my_func")

    assert func_v1 is not None and func_v2 is not None
    assert func_v1.code_hash != func_v2.code_hash
    assert func_v1.desc_hash == func_v2.desc_hash


# ---------------------------------------------------------------------------
# 2. Class methods prefixed with class name
# ---------------------------------------------------------------------------


def test_class_method_node_id_prefixed_with_class_name(tmp_path):
    """class Foo with def bar(self) → node ID proj::mymod::Foo.bar."""
    f = _write(
        tmp_path / "mymod.py",
        """\
class Foo:
    def bar(self):
        pass
""",
    )
    nodes, _ = scan_module(f, tmp_path, "proj")
    assert any(n.id == "proj::mymod::Foo.bar" for n in nodes)


def test_class_method_has_composes_edge_from_module(tmp_path):
    """Class method composes edge originates from the module node."""
    f = _write(
        tmp_path / "mymod.py",
        """\
class Foo:
    def bar(self):
        pass
""",
    )
    _, edges = scan_module(f, tmp_path, "proj")
    composes = _edges(edges, "composes")
    assert any(e.from_id == "proj::mymod" and e.to_id == "proj::mymod::Foo.bar" for e in composes)


# ---------------------------------------------------------------------------
# 3. Nested functions — separate node + parent composes edge
# ---------------------------------------------------------------------------


def test_nested_function_gets_own_node(tmp_path):
    """A function defined inside another function gets its own node."""
    f = _write(
        tmp_path / "mymod.py",
        """\
def outer():
    def inner():
        pass
""",
    )
    nodes, _ = scan_module(f, tmp_path, "proj")
    assert any(n.id == "proj::mymod::outer.inner" for n in nodes)


def test_nested_function_composes_edge_from_outer_function(tmp_path):
    """Nested function's composes edge comes from the outer function, not the module."""
    f = _write(
        tmp_path / "mymod.py",
        """\
def outer():
    def inner():
        pass
""",
    )
    _, edges = scan_module(f, tmp_path, "proj")
    composes = _edges(edges, "composes")
    assert any(e.from_id == "proj::mymod::outer" and e.to_id == "proj::mymod::outer.inner" for e in composes)


# ---------------------------------------------------------------------------
# 4. Async functions detected identically to sync
# ---------------------------------------------------------------------------


def test_async_function_produces_atomic_process_node(tmp_path):
    """async def produces an atomic_process node with code_hash and desc_hash."""
    f = _write(
        tmp_path / "mymod.py",
        '''\
async def fetch(url):
    """Fetch a URL asynchronously."""
    pass
''',
    )
    nodes, _ = scan_module(f, tmp_path, "proj")
    func = _node(nodes, "proj::mymod::fetch")
    assert func is not None
    assert func.node_type == "atomic_process"
    assert func.code_hash is not None
    assert func.desc_hash is not None


def test_async_function_same_node_type_as_sync(tmp_path):
    """sync def and async def both produce atomic_process nodes."""
    sync_file = _write(tmp_path / "sync_mod.py", "def process(x): return x\n")
    async_file = _write(tmp_path / "async_mod.py", "async def process(x): return x\n")

    sync_nodes, _ = scan_module(sync_file, tmp_path, "proj")
    async_nodes, _ = scan_module(async_file, tmp_path, "proj")

    sync_func = _node(sync_nodes, "proj::sync_mod::process")
    async_func = _node(async_nodes, "proj::async_mod::process")

    assert sync_func is not None and async_func is not None
    assert sync_func.node_type == async_func.node_type == "atomic_process"


# ---------------------------------------------------------------------------
# 5. Graceful skip on syntax errors
# ---------------------------------------------------------------------------


def test_syntax_error_does_not_raise(tmp_path):
    """scan_module on a file with a syntax error must not raise an exception."""
    f = _write(tmp_path / "broken.py", "def (\n    this is not valid python!!!\n")
    nodes, edges = scan_module(f, tmp_path, "proj")  # must not raise
    assert len(nodes) == 1
    assert edges == []


def test_syntax_error_stub_node_indicates_parse_failure(tmp_path):
    """The stub node emitted for a syntax-error file contains 'syntax error' in level_1."""
    f = _write(tmp_path / "broken.py", "def (\n    this is not valid python!!!\n")
    nodes, _ = scan_module(f, tmp_path, "proj")
    assert "syntax error" in nodes[0].level_1.lower()


# ---------------------------------------------------------------------------
# 6. documents edges from links arrays
# ---------------------------------------------------------------------------


def test_documents_edges_emitted_for_linked_node_ids(tmp_path):
    """A JSON doc section with a links field produces documents edges to each linked node_id."""
    doc = {
        "title": "Architecture",
        "sections": [
            {
                "id": "overview",
                "heading": "Overview",
                "content": "System overview.",
                "links": [
                    {"node_id": "proj::mymod::func_a"},
                    {"node_id": "proj::mymod::func_b"},
                ],
            }
        ],
    }
    f = _write(tmp_path / "docs" / "arch.json", json.dumps(doc))
    _, edges, _, _ = scan_single_json_doc(f, tmp_path, "proj")
    docs_edges = _edges(edges, "documents")
    to_ids = {e.to_id for e in docs_edges}
    assert "proj::mymod::func_a" in to_ids
    assert "proj::mymod::func_b" in to_ids


# ---------------------------------------------------------------------------
# 7. composes edges (module → function)
# ---------------------------------------------------------------------------


def test_module_has_composes_edge_to_each_top_level_function(tmp_path):
    """After scanning a .py file, a composes edge exists from the module to each function."""
    f = _write(
        tmp_path / "mymod.py",
        """\
def func_a():
    pass

def func_b():
    pass
""",
    )
    _, edges = scan_module(f, tmp_path, "proj")
    composes = _edges(edges, "composes")
    from_module = {e.to_id for e in composes if e.from_id == "proj::mymod"}
    assert "proj::mymod::func_a" in from_module
    assert "proj::mymod::func_b" in from_module


# ---------------------------------------------------------------------------
# 8. dFlow decorator detection stored as dflow_meta  (Tier 2)
# ---------------------------------------------------------------------------


@workflow(purpose="Verify that scan_module detects @workflow decorators and stores purpose in dflow_meta")
def test_workflow_decorator_stored_in_dflow_meta(tmp_path):
    """A function decorated with @workflow(purpose=...) has dflow_meta populated with the purpose."""
    f = _write(
        tmp_path / "mymod.py",
        """\
from axiom_annotations import workflow

@workflow(purpose="Verify this pipeline runs correctly")
def my_pipeline():
    pass
""",
    )
    nodes, _ = scan_module(f, tmp_path, "proj")
    func = _node(nodes, "proj::mymod::my_pipeline")
    assert func is not None
    assert func.dflow_meta is not None
    assert func.dflow_meta.get("purpose") == "Verify this pipeline runs correctly"


# ---------------------------------------------------------------------------
# 9. External package entity nodes
# ---------------------------------------------------------------------------


def test_external_package_import_produces_entity_node(tmp_path):
    """Importing a third-party package produces an entity node with subtype='external_package'."""
    f = _write(
        tmp_path / "mymod.py",
        """\
import requests

def fetch(url):
    return requests.get(url)
""",
    )
    nodes, _ = scan_module(f, tmp_path, "proj")
    ext_nodes = [n for n in nodes if n.node_type == "entity" and n.subtype == "external_package"]
    assert len(ext_nodes) >= 1
    assert any(n.title == "requests" for n in ext_nodes)
