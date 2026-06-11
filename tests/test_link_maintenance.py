"""Tests for link_maintenance -- patch_doc_links with nested sections.

Tier 1 -- plain pytest:
    _patch_sections_recursive() unit tests for flat, nested, and deeply nested sections.
    patch_doc_links() integration test with nested DocJSON on disk.
"""

from __future__ import annotations

import json


from axiom_graph.index.link_maintenance import _patch_sections_recursive, patch_doc_links


# ---------------------------------------------------------------------------
# Tier 1 -- _patch_sections_recursive unit tests
# ---------------------------------------------------------------------------


def test_patch_sections_recursive_flat_link():
    """Patches a link in a top-level section."""
    sections = [
        {"id": "s1", "links": [{"node_id": "old::id", "label": "ref"}]},
    ]
    result = _patch_sections_recursive(sections, "old::id", "new::id")
    assert result is True
    assert sections[0]["links"][0]["node_id"] == "new::id"


def test_patch_sections_recursive_nested_link():
    """Patches a link inside a nested child section."""
    sections = [
        {
            "id": "s1",
            "links": [],
            "sections": [
                {
                    "id": "s1.1",
                    "links": [{"node_id": "old::id", "label": "nested ref"}],
                }
            ],
        }
    ]
    result = _patch_sections_recursive(sections, "old::id", "new::id")
    assert result is True
    assert sections[0]["sections"][0]["links"][0]["node_id"] == "new::id"


def test_patch_sections_recursive_deeply_nested():
    """Patches a link three levels deep."""
    sections = [
        {
            "id": "s1",
            "sections": [
                {
                    "id": "s1.1",
                    "sections": [
                        {
                            "id": "s1.1.1",
                            "links": [{"node_id": "old::deep"}],
                        }
                    ],
                }
            ],
        }
    ]
    result = _patch_sections_recursive(sections, "old::deep", "new::deep")
    assert result is True
    assert sections[0]["sections"][0]["sections"][0]["links"][0]["node_id"] == "new::deep"


def test_patch_sections_recursive_no_match():
    """Returns False when no links match the old_id."""
    sections = [
        {"id": "s1", "links": [{"node_id": "other::id"}]},
        {"id": "s2", "sections": [{"id": "s2.1", "links": [{"node_id": "other::id2"}]}]},
    ]
    result = _patch_sections_recursive(sections, "old::id", "new::id")
    assert result is False


def test_patch_sections_recursive_mixed_levels():
    """Patches links at both top-level and nested levels in one pass."""
    sections = [
        {
            "id": "s1",
            "links": [{"node_id": "old::id"}],
            "sections": [
                {"id": "s1.1", "links": [{"node_id": "old::id"}]},
            ],
        }
    ]
    result = _patch_sections_recursive(sections, "old::id", "new::id")
    assert result is True
    assert sections[0]["links"][0]["node_id"] == "new::id"
    assert sections[0]["sections"][0]["links"][0]["node_id"] == "new::id"


# ---------------------------------------------------------------------------
# Tier 1 -- patch_doc_links integration with nested DocJSON on disk
# ---------------------------------------------------------------------------


def test_patch_doc_links_patches_nested_sections_on_disk(tmp_path):
    """patch_doc_links should patch links inside nested sections in DocJSON files."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    doc = {
        "title": "Test Doc",
        "sections": [
            {
                "id": "overview",
                "heading": "Overview",
                "content": "Top level",
                "links": [{"node_id": "proj::mod::old_func", "label": "top ref"}],
                "sections": [
                    {
                        "id": "details",
                        "heading": "Details",
                        "content": "Nested",
                        "links": [{"node_id": "proj::mod::old_func", "label": "nested ref"}],
                    }
                ],
            }
        ],
    }
    (docs_dir / "test.json").write_text(json.dumps(doc))

    db_path = tmp_path / ".axiom_graph" / "graph.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    count = patch_doc_links(tmp_path, db_path, "proj::mod::old_func", "proj::mod::new_func")
    assert count == 1

    patched = json.loads((docs_dir / "test.json").read_text())
    # Top-level link patched
    assert patched["sections"][0]["links"][0]["node_id"] == "proj::mod::new_func"
    # Nested link patched
    assert patched["sections"][0]["sections"][0]["links"][0]["node_id"] == "proj::mod::new_func"
