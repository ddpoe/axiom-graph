"""Markdown H2 section splitting in the doc scanner."""

from __future__ import annotations

from axiom_graph.scanners.doc_scanner import _split_h2_sections, markdown_section_slugs

_FENCED = (
    "# Title\n\n"
    "## Alpha\n\nIntro.\n\n```python\n## not a heading\ncode()\n```\n\nAlpha tail.\n\n"
    "## Beta\n\nBeta body.\n\n"
    "## Gamma\n\nGamma body.\n"
)


def test_sections_after_a_code_fence_keep_exactly_their_own_text():
    """A fence in one section shifts no later section, and a ``## `` line inside it does not split."""
    assert _split_h2_sections(_FENCED) == [
        {"heading": "Alpha", "body": "Intro.\n\n```python\n## not a heading\ncode()\n```\n\nAlpha tail."},
        {"heading": "Beta", "body": "Beta body."},
        {"heading": "Gamma", "body": "Gamma body."},
    ]
    assert markdown_section_slugs(_FENCED) == ["alpha", "beta", "gamma"]
