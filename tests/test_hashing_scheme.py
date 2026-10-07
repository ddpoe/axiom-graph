"""Golden hashes pinning the output of the current-node hasher to ``HASHING_SCHEME``.

The staleness refresh skips a file whose fingerprint it has already hashed.
That is only sound while :func:`current_node_hashes_for_file` returns the
same hashes for the same bytes.  Any change to that output must come with a
bump of :data:`axiom_graph.scanners.node_hashing.HASHING_SCHEME`, which makes
the next ``check`` re-hash every file once.

These tests hash small fixed fixtures and compare the result with literal
hashes recorded under the current scheme.  When one fails, the hash output
changed: bump ``HASHING_SCHEME`` and update the literals here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom_graph.models import AxiomNode
from axiom_graph.scanners.node_hashing import HASHING_SCHEME, current_node_hashes_for_file

#: The scheme the literals below were recorded under.
RECORDED_SCHEME = "3"

BUMP_MESSAGE = (
    "hash output changed: bump HASHING_SCHEME (forces one full check) and update these literals "
    "(and RECORDED_SCHEME) in tests/test_hashing_scheme.py"
)

PYTHON_FIXTURE = (
    '"""Fixture module."""\n'
    "\n"
    "from axiom_annotations import workflow\n"
    "\n"
    "\n"
    "def add(a, b):\n"
    '    """Return the sum of a and b."""\n'
    "    return a + b\n"
    "\n"
    "\n"
    "class Counter:\n"
    "    def bump(self, n):\n"
    "        self.n = n + 1\n"
    "        return self.n\n"
    "\n"
    "\n"
    '@workflow(purpose="Run the fixture")\n'
    "def run():\n"
    "    return add(1, 2)\n"
)

DOCJSON_FIXTURE = (
    '{"title": "Fixture doc", "sections": ['
    '{"id": "intro", "heading": "Intro", "content": "The fixture introduction."}, '
    '{"id": "usage", "heading": "Usage", "content": "Call **add** with two numbers."}'
    "]}\n"
)

MARKDOWN_FIXTURE = (
    "# Fixture notes\n\n## Intro\n\nThe fixture introduction.\n\n```\nadd(1, 2)\n```\n\n"
    "## Usage\n\nCall **add** with two numbers.\n"
)

TS_FIXTURE = (
    "export function alpha(): number {\n"
    "  return 1;\n"
    "}\n"
    "\n"
    "/** Beta doubles its input. */\n"
    "export function beta(x: number): number {\n"
    "  return x * 2;\n"
    "}\n"
)


def _node(node_id: str, node_type: str, subtype: str, location: str) -> AxiomNode:
    """Build an indexed-node stand-in whose stored hashes never match a real hash.

    Args:
        node_id: Full node id.
        node_type: ``atomic_process`` or ``composite_process``.
        subtype: Node subtype, which picks the hasher's branch.
        location: Repo-relative path of the node's file.

    Returns:
        The node.
    """
    return AxiomNode(
        id=node_id,
        node_type=node_type,
        subtype=subtype,
        title=node_id.rsplit("::", 1)[-1],
        location=location,
        source="ast",
        code_hash="stored",
        desc_hash="stored",
        level_0=node_id,
        level_1=node_id,
    )


def _write(root: Path, location: str, text: str) -> Path:
    """Write *text* at *location* under *root* with LF line endings.

    Args:
        root: Project root.
        location: Repo-relative path.
        text: File content.

    Returns:
        The absolute path written.
    """
    path = root / location
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _assert_golden(actual: dict, expected: dict) -> None:
    """Assert the scheme and the hashes both match what was recorded.

    Args:
        actual: ``current_node_hashes_for_file`` output.
        expected: The recorded ``{node_id: (code_hash, desc_hash)}``.
    """
    assert HASHING_SCHEME == RECORDED_SCHEME, BUMP_MESSAGE
    assert actual == expected, BUMP_MESSAGE


def test_python_function_method_and_envelope_hashes_are_pinned(tmp_path: Path) -> None:
    """A documented function, a method, a decorated function and its envelope hash to fixed values."""
    path = _write(tmp_path, "pkg/fixture.py", PYTHON_FIXTURE)
    nodes = [
        _node("proj::pkg.fixture::add", "atomic_process", "function", "pkg/fixture.py"),
        _node("proj::pkg.fixture::Counter.bump", "atomic_process", "function", "pkg/fixture.py"),
        _node("proj::pkg.fixture::run", "atomic_process", "function", "pkg/fixture.py"),
        _node("proj::pkg.fixture::run@workflow", "composite_process", "workflow", "pkg/fixture.py"),
    ]

    _assert_golden(
        current_node_hashes_for_file(path, nodes, tmp_path),
        {
            "proj::pkg.fixture::add": ("8f75a68646c879fd", "5d2c1ddb0de4977b"),
            "proj::pkg.fixture::Counter.bump": ("8a975b5ba8ed48f6", None),
            "proj::pkg.fixture::run": ("9f6a3615bd08c851", None),
            "proj::pkg.fixture::run@workflow": ("7fa1a566bc91ca56", None),
        },
    )


def test_docjson_doc_and_section_hashes_are_pinned(tmp_path: Path) -> None:
    """A DocJSON doc and its two sections hash to fixed values."""
    path = _write(tmp_path, "docs/fixture.docjson", DOCJSON_FIXTURE)
    nodes = [
        _node("proj::docs/fixture", "composite_process", "docjson_doc", "docs/fixture.docjson"),
        _node("proj::docs/fixture::intro", "atomic_process", "docjson_section", "docs/fixture.docjson"),
        _node("proj::docs/fixture::usage", "atomic_process", "docjson_section", "docs/fixture.docjson"),
    ]

    _assert_golden(
        current_node_hashes_for_file(path, nodes, tmp_path),
        {
            "proj::docs/fixture": ("5d015e2974b7a20f", "5d015e2974b7a20f"),
            "proj::docs/fixture::intro": ("348a6dc8b74a399b", "348a6dc8b74a399b"),
            "proj::docs/fixture::usage": ("6f8b56a74fd3cca3", "6f8b56a74fd3cca3"),
        },
    )


def test_markdown_doc_and_section_hashes_are_pinned(tmp_path: Path) -> None:
    """A Markdown doc and its two H2 sections hash to fixed values."""
    path = _write(tmp_path, "docs/notes.md", MARKDOWN_FIXTURE)
    nodes = [
        _node("proj::docs/notes.md", "composite_process", "docjson", "docs/notes.md"),
        _node("proj::docs/notes.md#intro", "atomic_process", "docjson", "docs/notes.md"),
        _node("proj::docs/notes.md#usage", "atomic_process", "docjson", "docs/notes.md"),
    ]

    _assert_golden(
        current_node_hashes_for_file(path, nodes, tmp_path),
        {
            "proj::docs/notes.md": ("46aeb9a85658cf06", "46aeb9a85658cf06"),
            "proj::docs/notes.md#intro": ("5411c748bf34e1a2", "24601bcaae6e170b"),
            "proj::docs/notes.md#usage": ("6f8b56a74fd3cca3", "8d59829c1e15afe1"),
        },
    )


def test_typescript_function_hashes_are_pinned(tmp_path: Path) -> None:
    """A plain and a documented TypeScript function hash to fixed values."""
    pytest.importorskip("tree_sitter_typescript")
    from axiom_graph.scanners import js_scanner

    if not js_scanner.HAS_TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    path = _write(tmp_path, "web/fixture.ts", TS_FIXTURE)
    nodes = [
        _node("proj::web.fixture::alpha", "atomic_process", "function", "web/fixture.ts"),
        _node("proj::web.fixture::beta", "atomic_process", "function", "web/fixture.ts"),
    ]

    _assert_golden(
        current_node_hashes_for_file(path, nodes, tmp_path),
        {
            "proj::web.fixture::alpha": ("634bb9f58465badd", None),
            "proj::web.fixture::beta": ("2c5342d5cbd7cd9e", "86864fb9aabdbca4"),
        },
    )
