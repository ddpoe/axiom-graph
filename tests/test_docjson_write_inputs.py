"""Unit tests for the docjson write-input helpers: footer stripping, content files, content hash."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from axiom_graph.docjson.api import (
    LINKED_NODES_CLOSE,
    LINKED_NODES_OPEN,
    WriteInputError,
    content_hash,
    read_content_file,
    strip_linked_nodes_footer,
)

_BLOCK = f"{LINKED_NODES_OPEN}\n**Linked nodes:**\n- `p::m::f`\n{LINKED_NODES_CLOSE}"


@pytest.mark.parametrize(
    ("incoming", "expected", "stripped"),
    [
        (None, None, False),
        ("", "", False),
        ("Plain prose.", "Plain prose.", False),
        (f"Top.\n\n{_BLOCK}\n\nBottom.", "Top.\n\nBottom.", True),
        (f"{_BLOCK}\n\nOnly prose after.", "Only prose after.", True),
        (f"Two.\n\n{_BLOCK}\n\n{_BLOCK}\n", "Two.", True),
        (f"Unterminated.\n\n{LINKED_NODES_OPEN}\n- `x`", f"Unterminated.\n\n{LINKED_NODES_OPEN}\n- `x`", False),
        ("Numbered.\n\n**Linked nodes:**\n1. `a`\n2) `b`\n\n", "Numbered.", True),
        ("Only the footer:\n**Linked nodes:**\n", "Only the footer:", True),
        ("**Linked nodes:** is a heading read_doc emits.", "**Linked nodes:** is a heading read_doc emits.", False),
        (
            f"Strips `{LINKED_NODES_OPEN}` … `{LINKED_NODES_CLOSE}` blocks.",
            f"Strips `{LINKED_NODES_OPEN}` … `{LINKED_NODES_CLOSE}` blocks.",
            False,
        ),
        (f"Example:\n\n```\n{_BLOCK}\n```\n", f"Example:\n\n```\n{_BLOCK}\n```\n", False),
        ("Example:\n```\n**Linked nodes:**\n- `x`\n", "Example:\n```\n**Linked nodes:**\n- `x`\n", False),
    ],
    ids=[
        "none",
        "empty",
        "plain",
        "marked-mid",
        "marked-lead",
        "two-marked",
        "unterminated",
        "numbered-list",
        "empty-footer",
        "inline-at-line-start",
        "markers-quoted-in-prose",
        "marked-example-in-fence",
        "unmarked-example-in-open-fence",
    ],
)
def test_strip_linked_nodes_footer(incoming, expected, stripped) -> None:
    assert strip_linked_nodes_footer(incoming) == (expected, stripped)


def test_content_hash_is_sha256_of_utf8_content() -> None:
    assert content_hash("Ä") == hashlib.sha256("Ä".encode()).hexdigest()
    assert content_hash(None) == content_hash("")


def test_read_content_file_resolves_relative_to_root_and_drops_bom(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_bytes(b"\xef\xbb\xbfline\r\nline2")
    assert read_content_file(tmp_path, "notes/a.md") == "line\r\nline2"


def test_read_content_file_rejects_missing_and_non_utf8(tmp_path: Path) -> None:
    with pytest.raises(WriteInputError, match="is not a file"):
        read_content_file(tmp_path, "missing.md")
    (tmp_path / "latin1.md").write_bytes("caf\xe9".encode("latin-1"))
    with pytest.raises(WriteInputError, match="not valid UTF-8"):
        read_content_file(tmp_path, "latin1.md")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths compare case-insensitively")
def test_read_content_file_compares_roots_case_insensitively_on_windows(tmp_path: Path, monkeypatch) -> None:
    import tempfile

    other = tmp_path / "other-temp"
    other.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(other))
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    assert read_content_file(Path(str(tmp_path).upper()), str(tmp_path / "a.md")) == "x"
