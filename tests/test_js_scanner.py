"""Tests for the JS/TS scanner (ADR-006).

Covers: function forms, ESM import resolution, import type filtering,
optional dependency skip, builder integration, and config extension.
"""

from __future__ import annotations

import textwrap
from pathlib import Path


# ---------------------------------------------------------------------------
# 1. Config: js_paths field
# ---------------------------------------------------------------------------


class TestScanConfigJsPaths:
    """ScanConfig gains a js_paths list field."""

    def test_default_js_paths_is_empty(self):
        from axiom_graph.config import ScanConfig

        cfg = ScanConfig()
        assert cfg.js_paths == []

    def test_load_js_paths_from_toml(self, tmp_path: Path):
        """AxiomGraphConfig.load reads js_paths from [axiom_graph.scan]."""
        toml = tmp_path / "axiom-graph.toml"
        toml.write_text('[axiom_graph.scan]\njs_paths = ["viz/static/ts/*.ts"]\n')
        from axiom_graph.config import AxiomGraphConfig

        cfg = AxiomGraphConfig.load(tmp_path)
        assert cfg.scan.js_paths == ["viz/static/ts/*.ts"]


# ---------------------------------------------------------------------------
# 2. Scanner: function extraction (5 forms)
# ---------------------------------------------------------------------------


def _make_ts_file(tmp_path: Path, code: str, name: str = "mod.ts") -> Path:
    """Write a TS file and return its path."""
    f = tmp_path / name
    f.write_text(textwrap.dedent(code), encoding="utf-8")
    return f


class TestFunctionExtraction:
    """scan_js_module extracts all 5 function forms."""

    def _scan(self, tmp_path: Path, code: str, name: str = "mod.ts"):
        from axiom_graph.scanners.js_scanner import scan_js_module

        f = _make_ts_file(tmp_path, code, name)
        return scan_js_module(f, tmp_path, "test")

    def test_named_function_declaration(self, tmp_path: Path):
        nodes, edges = self._scan(
            tmp_path,
            """\
            export function hello(name: string): string {
              return 'hi ' + name;
            }
            """,
        )
        func_nodes = [n for n in nodes if n.node_type == "atomic_process"]
        assert len(func_nodes) == 1
        assert func_nodes[0].title == "hello"
        assert func_nodes[0].level_1.startswith("hello")
        # Module node also present
        mod_nodes = [n for n in nodes if n.node_type == "composite_process"]
        assert len(mod_nodes) == 1

    def test_arrow_function_assigned_to_const(self, tmp_path: Path):
        nodes, edges = self._scan(
            tmp_path,
            """\
            const greet = (x: number): void => {
              console.log(x);
            };
            """,
        )
        func_nodes = [n for n in nodes if n.node_type == "atomic_process"]
        assert len(func_nodes) == 1
        assert func_nodes[0].title == "greet"

    def test_class_methods(self, tmp_path: Path):
        nodes, edges = self._scan(
            tmp_path,
            """\
            class MyClass {
              method1(x: string): void {}
              async method2(): Promise<void> {}
            }
            """,
        )
        func_nodes = [n for n in nodes if n.node_type == "atomic_process"]
        names = {n.title for n in func_nodes}
        assert "MyClass.method1" in names
        assert "MyClass.method2" in names

    def test_object_method_shorthand(self, tmp_path: Path):
        nodes, edges = self._scan(
            tmp_path,
            """\
            const obj = {
              shorthand() { return 1; }
            };
            """,
        )
        func_nodes = [n for n in nodes if n.node_type == "atomic_process"]
        assert len(func_nodes) == 1
        assert func_nodes[0].title == "obj.shorthand"

    def test_hof_wrapper_workflow(self, tmp_path: Path):
        nodes, edges = self._scan(
            tmp_path,
            """\
            const wrapped = workflow({ purpose: 'test' })(function myWorkflow() {});
            """,
        )
        func_nodes = [n for n in nodes if n.node_type == "atomic_process"]
        assert len(func_nodes) == 1
        assert func_nodes[0].title == "wrapped"


# ---------------------------------------------------------------------------
# 3. Import resolution + import type filtering
# ---------------------------------------------------------------------------


class TestImportResolution:
    """ESM imports produce depends_on edges; import type is skipped."""

    def test_esm_import_produces_edge(self, tmp_path: Path):
        from axiom_graph.scanners.js_scanner import scan_js_module

        # Create two files so the import resolves
        (tmp_path / "utils.ts").write_text("export function esc(s: string) { return s; }\n")
        main = tmp_path / "main.ts"
        main.write_text("import { esc } from './utils.js';\nexport function run() { esc('hi'); }\n")
        nodes, edges = scan_js_module(main, tmp_path, "test")
        edge_types = {e.edge_type for e in edges}
        assert "depends_on" in edge_types
        dep_edges = [e for e in edges if e.edge_type == "depends_on"]
        assert any("utils" in e.to_id for e in dep_edges)

    def test_import_type_skipped(self, tmp_path: Path):
        from axiom_graph.scanners.js_scanner import scan_js_module

        (tmp_path / "types.ts").write_text("export interface Foo {}\n")
        main = tmp_path / "main.ts"
        main.write_text("import type { Foo } from './types.js';\nexport function run(): Foo { return {} as Foo; }\n")
        nodes, edges = scan_js_module(main, tmp_path, "test")
        # No depends_on edge for type-only import
        dep_edges = [e for e in edges if e.edge_type == "depends_on"]
        assert not any("types" in e.to_id for e in dep_edges)

    def test_namespace_import(self, tmp_path: Path):
        from axiom_graph.scanners.js_scanner import scan_js_module

        (tmp_path / "graph.ts").write_text("export function render() {}\n")
        main = tmp_path / "main.ts"
        main.write_text("import * as Graph from './graph.js';\nexport function init() { Graph.render(); }\n")
        nodes, edges = scan_js_module(main, tmp_path, "test")
        dep_edges = [e for e in edges if e.edge_type == "depends_on"]
        assert any("graph" in e.to_id for e in dep_edges)


# ---------------------------------------------------------------------------
# 4. Optional dependency — graceful skip
# ---------------------------------------------------------------------------


class TestOptionalDependency:
    """When tree-sitter is not installed, the scanner reports unavailability."""

    def test_has_tree_sitter_flag(self):
        from axiom_graph.scanners import js_scanner

        # Since tree-sitter IS installed in the test env, this should be True
        assert js_scanner.HAS_TREE_SITTER is True


# ---------------------------------------------------------------------------
# 5. Module node properties
# ---------------------------------------------------------------------------


class TestModuleNode:
    """Module-level composite_process node is created correctly."""

    def test_module_node_id_and_location(self, tmp_path: Path):
        from axiom_graph.scanners.js_scanner import scan_js_module

        sub = tmp_path / "src"
        sub.mkdir()
        f = sub / "app.ts"
        f.write_text("export function init() {}\n")
        nodes, _ = scan_js_module(f, tmp_path, "myproj")
        mod = [n for n in nodes if n.node_type == "composite_process"][0]
        assert mod.id == "myproj::src.app"
        assert mod.location == "src/app.ts"
        assert mod.source == "tree_sitter"

    def test_module_node_has_module_subtype(self, tmp_path: Path):
        """The JS/TS module anchor carries subtype='module' so the staleness
        content gate can address it without a `subtype is None` test."""
        from axiom_graph.scanners.js_scanner import scan_js_module

        f = tmp_path / "mod.ts"
        f.write_text("export function foo() { return 1; }\n")
        nodes, _ = scan_js_module(f, tmp_path, "t")
        mod = [n for n in nodes if n.id == "t::mod"][0]
        assert mod.subtype == "module"

    def test_code_hash_changes_on_edit(self, tmp_path: Path):
        from axiom_graph.scanners.js_scanner import scan_js_module

        f = tmp_path / "mod.ts"
        f.write_text("export function foo() { return 1; }\n")
        nodes1, _ = scan_js_module(f, tmp_path, "t")
        hash1 = nodes1[0].code_hash

        f.write_text("export function foo() { return 2; }\n")
        nodes2, _ = scan_js_module(f, tmp_path, "t")
        hash2 = nodes2[0].code_hash

        assert hash1 != hash2


# ---------------------------------------------------------------------------
# 6. Builder integration
# ---------------------------------------------------------------------------


class TestBuilderIntegration:
    """builder.py dispatches JS files to the JS scanner."""

    def test_iter_js_files_respects_js_paths(self, tmp_path: Path):
        """_iter_js_files yields only files matching js_paths globs."""
        from axiom_graph.index.builder import _iter_js_files

        src = tmp_path / "src"
        src.mkdir()
        (src / "app.ts").write_text("export function init() {}\n")
        (src / "utils.ts").write_text("export function esc() {}\n")
        (tmp_path / "other.ts").write_text("export function nope() {}\n")

        js_paths = ["src/*.ts"]
        skip_dirs: frozenset[str] = frozenset()
        files = list(_iter_js_files(tmp_path, js_paths, skip_dirs))
        names = {f.name for f in files}
        assert "app.ts" in names
        assert "utils.ts" in names
        assert "other.ts" not in names


# ---------------------------------------------------------------------------
# 7. Function subtype tagging (parity with Python module_scanner)
# ---------------------------------------------------------------------------


class TestFunctionSubtype:
    """Atomic function nodes carry subtype='function' ('test' in test files)."""

    def _scan(self, tmp_path: Path, code: str, name: str = "mod.ts"):
        from axiom_graph.scanners.js_scanner import scan_js_module

        f = _make_ts_file(tmp_path, code, name)
        return scan_js_module(f, tmp_path, "test")

    def test_ordinary_function_subtype_is_function(self, tmp_path: Path):
        nodes, _ = self._scan(tmp_path, "export function alpha() { return 1; }\n")
        fn = next(n for n in nodes if n.node_type == "atomic_process")
        assert fn.subtype == "function"

    def test_arrow_function_subtype_is_function(self, tmp_path: Path):
        nodes, _ = self._scan(tmp_path, "const beta = () => 2;\n")
        fn = next(n for n in nodes if n.node_type == "atomic_process")
        assert fn.subtype == "function"

    def test_function_in_test_file_subtype_is_test(self, tmp_path: Path):
        """Mirrors Python's basename-based test tagging (test_*/_test, *.test.*)."""
        nodes, _ = self._scan(tmp_path, "export function helper() { return 1; }\n", name="mod.test.ts")
        fn = next(n for n in nodes if n.node_type == "atomic_process")
        assert fn.subtype == "test"

    def test_module_composite_subtype_is_module(self, tmp_path: Path):
        """The module composite carries subtype='module' (parity with Python;
        cycle pev-2026-06-11-staleness-content-hash-gate US-4)."""
        nodes, _ = self._scan(tmp_path, "export function alpha() { return 1; }\n")
        mod = next(n for n in nodes if n.node_type == "composite_process")
        assert mod.subtype == "module"
