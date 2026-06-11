"""Sphinx configuration for the Axiom-Graph documentation site.

Two public surfaces are documented:

* **CLI** -- rendered by ``sphinx-click`` from the Click command group, split
  into one page per command group.
* **MCP tools** -- rendered by ``sphinx.ext.autodoc`` from
  ``axiom_graph.mcp.server`` (the ``@mcp.tool()`` functions stay introspectable
  after decoration), as a single flat page.

The user-facing narrative pages (generated from DocJSON by the consumer
renderer) are wired in alongside these later.
"""

from __future__ import annotations

import os
import sys

# Make the package importable for autodoc / sphinx-click even when it is not
# installed into the build environment.
sys.path.insert(0, os.path.abspath(".."))

# -- Project information ------------------------------------------------------

project = "Axiom-Graph"
author = "ddpoe"
copyright = "2026, ddpoe"  # noqa: A001 - Sphinx-mandated config name

# -- General configuration ----------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",  # MCP tool reference (imports the module)
    "sphinx.ext.autosummary",  # one page per MCP tool + summary tables
    "sphinx.ext.napoleon",  # Google-style docstrings
    # Render typed parameter fields as a per-parameter definition list
    # (scanpy-style blocks) instead of one compact field list.
    "scanpydoc.definition_list_typed_field",
    "sphinx_click",  # CLI reference from the Click command group
    "myst_parser",  # Markdown (MyST) source support
    "sphinxcontrib.mermaid",  # mermaid diagrams
]

# Generate the per-tool stub pages referenced by ``.. autosummary:: :toctree:``
# blocks in the MCP group pages. The stubs use ``_templates/autosummary/``.
autosummary_generate = True
templates_path = ["_templates"]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

# Generate anchor slugs for headings h1-h4 so intra-page and cross-page
# ``#anchor`` links in the generated consumer pages (e.g.
# ``staleness.md#propagation-by-edge-type``) resolve.
myst_heading_anchors = 4

root_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Napoleon (Google-style docstrings) --------------------------------------

napoleon_google_docstring = True
napoleon_numpy_docstring = False

# -- autodoc ------------------------------------------------------------------

# Heavy / deprecated optional extras are never installed in the docs build
# (RTD installs the base package only). Mock them so importing the MCP module
# for autodoc never fails on a missing optional dependency.
autodoc_mock_imports = [
    "tree_sitter",
    "tree_sitter_javascript",
    "tree_sitter_typescript",
    "fastembed",
    "onnxruntime",
    "sentence_transformers",
    "sqlite_vec",
    "fastapi",
    "uvicorn",
]
autodoc_member_order = "bysource"
# Move type annotations out of the (long) signature and into the per-parameter
# descriptions, so the signature reads as just names + defaults.
autodoc_typehints = "description"

# Each MCP tool lives on its own generated page, so the documented object should
# not also register a second "On this page" TOC entry under the page title
# (that produced the doubly-nested right-hand TOC).
toc_object_entries = False

# -- HTML output --------------------------------------------------------------

html_theme = "furo"
html_title = "Axiom-Graph"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_favicon = "_static/favicon.png"
html_theme_options = {
    # Wordless graph mark; furo shows it above the project title in the sidebar.
    # Separate light/dark variants because the navy edges vanish on dark mode.
    "light_logo": "logo-light.png",
    "dark_logo": "logo-dark.png",
}
