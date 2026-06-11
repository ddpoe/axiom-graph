"""Unit tests for the consolidated current-node-hash primitive.

Covers each dispatch branch of
:mod:`axiom_graph.scanners.node_hashing` plus the two regressions
that motivated the consolidation cycle:

* qualified-name lookup so sibling-class methods do not collide;
* envelope nodes (``subtype='workflow'`` or ``'task'``) whose id
  ends in the literal ``@workflow`` suffix even when decorated with
  ``@task``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from axiom_graph.models import AxiomNode, hash16
from axiom_graph.scanners._step_helpers import envelope_code_hash
from axiom_graph.scanners.module_scanner import _split_function
from axiom_graph.scanners.node_hashing import (
    current_node_hash,
    current_node_hashes_for_file,
)


def _make_node(
    node_id: str,
    *,
    node_type: str = "atomic_process",
    subtype: str | None = "function",
    location: str = "src/mod.py",
    code_hash: str = "stored_code",
    desc_hash: str | None = "stored_desc",
) -> AxiomNode:
    return AxiomNode(
        id=node_id,
        node_type=node_type,
        subtype=subtype,
        title=node_id.rsplit("::", 1)[-1],
        location=location,
        source="ast",
        code_hash=code_hash,
        desc_hash=desc_hash,
        level_0=node_id,
        level_1=node_id,
    )


# ---------------------------------------------------------------------------
# Helper: compute the expected hashes of a function in source text.
# ---------------------------------------------------------------------------


def _hashes_for_func(source: str, qualified_name: str) -> tuple[str, str | None]:
    """Walk ``source`` and return the (code, desc) hashes for ``qualified_name``."""
    tree = ast.parse(source)

    def _visit(tree_node: ast.AST, prefix: str) -> tuple[str, str | None] | None:
        for child in ast.iter_child_nodes(tree_node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}" if prefix else child.name
                if qual == qualified_name:
                    code_text, docstring = _split_function(child)
                    return (
                        hash16(code_text),
                        hash16(docstring) if docstring else None,
                    )
                inner = _visit(child, f"{qual}.")
                if inner is not None:
                    return inner
            elif isinstance(child, ast.ClassDef):
                inner = _visit(child, f"{child.name}.")
                if inner is not None:
                    return inner
        return None

    found = _visit(tree, "")
    assert found is not None, f"function {qualified_name!r} not found"
    return found


# ---------------------------------------------------------------------------
# Branch 1: docjson composite (file-level)
# ---------------------------------------------------------------------------


def test_docjson_composite_returns_whole_file_hash(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    json_text = json.dumps({"title": "T", "sections": []})
    (docs_dir / "x.json").write_text(json_text, encoding="utf-8")

    node = _make_node(
        "proj::docs.x",
        node_type="composite_process",
        subtype="docjson",
        location="docs/x.json",
    )
    code, desc = current_node_hash(node, tmp_path)
    expected = hash16(json_text)
    assert code == expected
    assert desc == expected


# ---------------------------------------------------------------------------
# Branch 2: docjson atomic (section)
# ---------------------------------------------------------------------------


def test_docjson_section_lookup(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    doc = {
        "title": "T",
        "sections": [
            {"id": "intro", "heading": "Intro", "content": "Hello world."},
        ],
    }
    (docs_dir / "y.json").write_text(json.dumps(doc), encoding="utf-8")

    node = _make_node(
        "proj::docs.y::intro",
        node_type="atomic_process",
        subtype="docjson",
        location="docs/y.json",
    )
    code, desc = current_node_hash(node, tmp_path)
    # Both code_hash and desc_hash are content_hash for sections, agreeing
    # with the doc_sections shadow row's desc_hash per the four-field
    # invariant.  Heading edits surface at the file-level composite.
    assert code == hash16("Hello world.")
    assert desc == hash16("Hello world.")


def test_docjson_section_miss_returns_stored(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "y.json").write_text(
        json.dumps({"title": "T", "sections": []}),
        encoding="utf-8",
    )

    node = _make_node(
        "proj::docs.y::missing-section",
        node_type="atomic_process",
        subtype="docjson",
        location="docs/y.json",
        code_hash="STORED",
        desc_hash="STORED_DESC",
    )
    code, desc = current_node_hash(node, tmp_path)
    assert code == "STORED"
    assert desc == "STORED_DESC"


# ---------------------------------------------------------------------------
# Branch 3: workflow / task envelope
# ---------------------------------------------------------------------------


def test_envelope_task_with_workflow_suffix_in_id(tmp_path: Path) -> None:
    """Envelopes always carry ``@workflow`` in their id even when subtype='task'.

    This is the regression for ``get_stale_tests@workflow`` /
    ``get_stale_doc_sections@workflow`` whose subtype is ``'task'`` —
    legacy ``compute_current_hashes`` matched on title which had a literal
    space (``"get_stale_tests @task"``) and never found anything.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "mod.py"
    py_file.write_text(
        "from axiom_annotations import task\n"
        "\n"
        "@task(\n"
        '    purpose="do work",\n'
        '    inputs="x",\n'
        '    outputs="y",\n'
        ")\n"
        "def get_stale_tests():\n"
        '    """Doc."""\n'
        "    return 1\n",
        encoding="utf-8",
    )

    node = _make_node(
        "proj::src.mod::get_stale_tests@workflow",
        node_type="composite_process",
        subtype="task",  # decorator is @task even though id ends @workflow
        location="src/mod.py",
        code_hash="OLD_ENV_HASH",
    )
    code, desc = current_node_hash(node, tmp_path)
    expected = envelope_code_hash(
        "task",
        {"purpose": "do work", "inputs": "x", "outputs": "y"},
    )
    assert code == expected
    assert desc is None


def test_envelope_workflow_subtype(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "mod.py"
    py_file.write_text(
        "from axiom_annotations import workflow\n"
        "\n"
        '@workflow(purpose="orchestrate")\n'
        "def big_pipeline():\n"
        "    return 1\n",
        encoding="utf-8",
    )

    node = _make_node(
        "proj::src.mod::big_pipeline@workflow",
        node_type="composite_process",
        subtype="workflow",
        location="src/mod.py",
    )
    code, desc = current_node_hash(node, tmp_path)
    assert code == envelope_code_hash("workflow", {"purpose": "orchestrate"})
    assert desc is None


# ---------------------------------------------------------------------------
# Branch 4: python atomic with sibling-class collision (the headline bug)
# ---------------------------------------------------------------------------


def test_sibling_class_methods_get_distinct_hashes(tmp_path: Path) -> None:
    """``TestA.test_foo`` and ``TestB.test_foo`` resolve independently."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "mod.py"
    py_file.write_text(
        "class TestA:\n"
        "    def test_foo(self):\n"
        "        return 1\n"
        "\n"
        "class TestB:\n"
        "    def test_foo(self):\n"
        "        return 999  # different body\n",
        encoding="utf-8",
    )
    source = py_file.read_text(encoding="utf-8")

    node_a = _make_node(
        "proj::src.mod::TestA.test_foo",
        subtype="test",
        location="src/mod.py",
    )
    node_b = _make_node(
        "proj::src.mod::TestB.test_foo",
        subtype="test",
        location="src/mod.py",
    )
    a_code, _ = current_node_hash(node_a, tmp_path)
    b_code, _ = current_node_hash(node_b, tmp_path)

    expected_a, _ = _hashes_for_func(source, "TestA.test_foo")
    expected_b, _ = _hashes_for_func(source, "TestB.test_foo")

    assert a_code == expected_a
    assert b_code == expected_b
    assert a_code != b_code, "sibling-class collision regression"


def test_top_level_function_resolved_by_short_name(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "mod.py"
    py_file.write_text(
        'def my_func():\n    """A docstring."""\n    return 42\n',
        encoding="utf-8",
    )

    node = _make_node(
        "proj::src.mod::my_func",
        subtype="function",
        location="src/mod.py",
    )
    code, desc = current_node_hash(node, tmp_path)
    expected_code, expected_desc = _hashes_for_func(py_file.read_text(encoding="utf-8"), "my_func")
    assert code == expected_code
    assert desc == expected_desc


# ---------------------------------------------------------------------------
# Branch 5: pass-through subtypes
# ---------------------------------------------------------------------------


def test_pass_through_subtypes_return_stored(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "mod.py").write_text("def foo():\n    return 1\n", encoding="utf-8")

    for subtype in ("step", "autostep", "external_package"):
        n = _make_node(
            f"proj::src.mod::foo::{subtype}-1",
            subtype=subtype,
            location="src/mod.py",
            code_hash="STORED",
            desc_hash="STORED_DESC",
        )
        code, desc = current_node_hash(n, tmp_path)
        assert code == "STORED", f"subtype {subtype} altered code_hash"
        assert desc == "STORED_DESC", f"subtype {subtype} altered desc_hash"


def test_entity_node_returns_stored(tmp_path: Path) -> None:
    n = _make_node(
        "proj::ent",
        node_type="entity",
        subtype=None,
        location="src/mod.py",
        code_hash="ENT",
        desc_hash="ENT_DESC",
    )
    # File doesn't exist — but entity short-circuit returns stored anyway
    # via the abs_path.exists() check first; verify by writing the file.
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")
    code, desc = current_node_hash(n, tmp_path)
    assert code == "ENT"
    assert desc == "ENT_DESC"


# ---------------------------------------------------------------------------
# Fallbacks: missing file, syntax error
# ---------------------------------------------------------------------------


def test_missing_file_returns_stored(tmp_path: Path) -> None:
    n = _make_node(
        "proj::src.mod::foo",
        location="src/does_not_exist.py",
        code_hash="STORED",
    )
    code, desc = current_node_hash(n, tmp_path)
    assert code == "STORED"
    assert desc == "stored_desc"


def test_syntax_error_returns_stored(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "mod.py").write_text("def broken(:\n", encoding="utf-8")

    n = _make_node(
        "proj::src.mod::foo",
        location="src/mod.py",
        code_hash="STORED",
        desc_hash="STORED_DESC",
    )
    code, desc = current_node_hash(n, tmp_path)
    assert code == "STORED"
    assert desc == "STORED_DESC"


def test_lookup_miss_returns_stored(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "mod.py").write_text("def existing():\n    pass\n", encoding="utf-8")

    n = _make_node(
        "proj::src.mod::missing_func",
        location="src/mod.py",
        code_hash="STORED",
    )
    code, desc = current_node_hash(n, tmp_path)
    assert code == "STORED"


# ---------------------------------------------------------------------------
# Batch entry point
# ---------------------------------------------------------------------------


def test_batch_returns_distinct_hashes_for_each_node(tmp_path: Path) -> None:
    """Single AST parse, every node from that file looked up correctly."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "mod.py"
    py_file.write_text(
        "def free():\n"
        "    return 1\n"
        "\n"
        "class TestA:\n"
        "    def test_foo(self):\n"
        "        return 2\n"
        "\n"
        "class TestB:\n"
        "    def test_foo(self):\n"
        "        return 3\n",
        encoding="utf-8",
    )

    free = _make_node("proj::src.mod::free", subtype="function", location="src/mod.py")
    a = _make_node("proj::src.mod::TestA.test_foo", subtype="test", location="src/mod.py")
    b = _make_node("proj::src.mod::TestB.test_foo", subtype="test", location="src/mod.py")

    out = current_node_hashes_for_file(py_file, [free, a, b], tmp_path)

    assert set(out) == {free.id, a.id, b.id}
    assert out[a.id][0] != out[b.id][0]
    assert out[free.id][0] not in (out[a.id][0], out[b.id][0])


def test_batch_handles_envelopes_and_pythons_in_one_file(tmp_path: Path) -> None:
    """Envelope + python function defined in same file each get their own hash."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "mod.py"
    py_file.write_text(
        'from axiom_annotations import task\n\n@task(purpose="do")\ndef f():\n    return 1\n',
        encoding="utf-8",
    )

    func = _make_node("proj::src.mod::f", subtype="function", location="src/mod.py")
    env = _make_node(
        "proj::src.mod::f@workflow",
        subtype="task",
        node_type="composite_process",
        location="src/mod.py",
    )

    out = current_node_hashes_for_file(py_file, [func, env], tmp_path)
    func_code, _ = out[func.id]
    env_code, env_desc = out[env.id]

    expected_env = envelope_code_hash("task", {"purpose": "do"})
    assert env_code == expected_env
    assert env_desc is None
    assert func_code != env_code  # function hash strips decorators


def test_batch_missing_file_omits_all(tmp_path: Path) -> None:
    """When the file is gone, batch returns an empty dict so callers map to NOT_FOUND."""
    n = _make_node(
        "proj::src.mod::foo",
        location="src/missing.py",
        code_hash="STORED",
        desc_hash="STORED_DESC",
    )
    out = current_node_hashes_for_file(tmp_path / "src" / "missing.py", [n], tmp_path)
    assert out == {}


def test_batch_omits_missing_python_function(tmp_path: Path) -> None:
    """Function that has been deleted from the file is absent from the result."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "mod.py"
    py_file.write_text("def existing():\n    pass\n", encoding="utf-8")

    existing = _make_node("proj::src.mod::existing", location="src/mod.py")
    deleted = _make_node("proj::src.mod::deleted", location="src/mod.py")

    out = current_node_hashes_for_file(py_file, [existing, deleted], tmp_path)
    assert existing.id in out
    assert deleted.id not in out


def test_batch_omits_passthrough_subtypes(tmp_path: Path) -> None:
    """step / autostep / external_package skipped entirely (caller pre-handles)."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "mod.py"
    py_file.write_text("def f():\n    pass\n", encoding="utf-8")

    f = _make_node("proj::src.mod::f", location="src/mod.py")
    s = _make_node("proj::src.mod::f::step-1", subtype="step", location="src/mod.py")

    out = current_node_hashes_for_file(py_file, [f, s], tmp_path)
    assert f.id in out
    assert s.id not in out


def test_batch_omits_envelope_with_decorator_removed(tmp_path: Path) -> None:
    """Envelope whose decorator was stripped is absent from result."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    py_file = src_dir / "mod.py"
    # Plain function — no @workflow / @task decorator anymore.
    py_file.write_text("def f():\n    return 1\n", encoding="utf-8")

    env = _make_node(
        "proj::src.mod::f@workflow",
        subtype="workflow",
        node_type="composite_process",
        location="src/mod.py",
    )

    out = current_node_hashes_for_file(py_file, [env], tmp_path)
    assert env.id not in out


# ---------------------------------------------------------------------------
# Branch 6: JS/TS code nodes
#
# The staleness hasher must dispatch on file extension, not subtype, and
# agree with ``js_scanner`` for an unchanged file.  Before the JS/TS branch
# existed, ``ast.parse`` ran on TypeScript, raised ``SyntaxError``, and every
# node was omitted -> ``compute_staleness`` mapped the omission to NOT_FOUND.
# These tests pin the scanner-as-source-of-truth contract.
# ---------------------------------------------------------------------------


def _make_ts_file(tmp_path: Path, code: str, name: str = "mod.ts") -> Path:
    """Write a small TS file (mirrors tests/test_js_scanner.py::_make_ts_file)."""
    import textwrap

    f = tmp_path / name
    f.write_text(textwrap.dedent(code).lstrip(), encoding="utf-8")
    return f


_TS_TWO_FUNCS = """
    export function alpha(): number {
      return 1;
    }

    /** Beta doubles its input. */
    export function beta(x: number): number {
      return x * 2;
    }
"""


def test_batch_ts_function_hashes_match_scanner(tmp_path: Path) -> None:
    """Unchanged .ts: every atomic node resolves with the scanner's hashes.

    This is the missing-branch contract.  Today the hasher returns ``{}``
    for a .ts file (ast.parse fails), so this fails until the dispatch lands.
    """
    pytest.importorskip("tree_sitter_typescript")
    from axiom_graph.scanners import js_scanner

    f = _make_ts_file(tmp_path, _TS_TWO_FUNCS)
    nodes, _ = js_scanner.scan_js_module(f, tmp_path, "test")
    atomic = [n for n in nodes if n.node_type == "atomic_process"]
    assert atomic, "scanner should emit atomic function nodes"

    out = current_node_hashes_for_file(f, nodes, tmp_path)

    for n in atomic:
        assert n.id in out, f"{n.id} omitted -> would become NOT_FOUND"
        assert out[n.id] == (n.code_hash, n.desc_hash)


def test_batch_omits_deleted_ts_function(tmp_path: Path) -> None:
    """A TS function removed from the file is omitted -> correctly NOT_FOUND.

    Parity with ``test_batch_omits_missing_python_function``.
    """
    pytest.importorskip("tree_sitter_typescript")
    from axiom_graph.scanners import js_scanner

    f = _make_ts_file(tmp_path, _TS_TWO_FUNCS)
    nodes, _ = js_scanner.scan_js_module(f, tmp_path, "test")
    alpha = next(n for n in nodes if n.id.endswith("::alpha"))
    beta = next(n for n in nodes if n.id.endswith("::beta"))

    # Rewrite the file with beta deleted.
    _make_ts_file(tmp_path, "export function alpha(): number {\n  return 1;\n}\n")

    out = current_node_hashes_for_file(f, [alpha, beta], tmp_path)
    assert alpha.id in out
    assert beta.id not in out


def test_batch_ts_preserved_when_tree_sitter_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tree-sitter absent: existing TS nodes keep stored hashes, never omitted.

    "Scanner absent != nodes deleted."  Returning the stored hash makes
    ``compute_staleness`` see cur == stored -> VERIFIED rather than NOT_FOUND.
    """
    pytest.importorskip("tree_sitter_typescript")
    from axiom_graph.scanners import js_scanner

    f = _make_ts_file(tmp_path, _TS_TWO_FUNCS)
    nodes, _ = js_scanner.scan_js_module(f, tmp_path, "test")
    atomic = [n for n in nodes if n.node_type == "atomic_process"]

    monkeypatch.setattr(js_scanner, "HAS_TREE_SITTER", False)
    out = current_node_hashes_for_file(f, nodes, tmp_path)

    for n in atomic:
        assert n.id in out, "absent scanner must not strand TS nodes as NOT_FOUND"
        assert out[n.id] == (n.code_hash, n.desc_hash)


def test_single_ts_node_re_derives_from_file(tmp_path: Path) -> None:
    """Singular ``current_node_hash`` re-derives a .ts node from disk.

    Uses a *stale* stored hash so the test fails if the function merely
    echoes the stored value (the pre-fix parse-failure fallback) instead of
    re-scanning the file.
    """
    pytest.importorskip("tree_sitter_typescript")
    from axiom_graph.scanners import js_scanner

    f = _make_ts_file(tmp_path, _TS_TWO_FUNCS)
    nodes, _ = js_scanner.scan_js_module(f, tmp_path, "test")
    beta = next(n for n in nodes if n.id.endswith("::beta"))

    stale = _make_node(
        beta.id,
        location="mod.ts",
        subtype="function",
        code_hash="STALE_CODE",
        desc_hash="STALE_DESC",
    )
    assert current_node_hash(stale, tmp_path) == (beta.code_hash, beta.desc_hash)
