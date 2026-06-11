"""Consumer documentation renderer and site build pipeline.

Converts DocJSON section data to clean MyST Markdown for the Sphinx
``userdocs`` site.  Unlike the agent-facing renderer in
``axiom_graph.mcp._helpers._render_doc_markdown``, this module produces output
free of section-ID comments, linked-node blocks, and other annotations
meant for AI agents.

The core ``build_site`` function is the shared pipeline used by both the
CLI command (``axiom-graph render-site``) and the MCP tool (``axiom_graph_render_site``).

Functions
---------
render_doc_consumer(title, sections)  -- render a single doc to clean Markdown
load_site_nav(nav_path)               -- load and parse the slim site-nav.yml
parse_show(show, project_id, prefix)  -- parse the recursive ``show:`` list
validate_site_nav(nav_data, ...)      -- validate the slim nav against index + disk
generate_toctree_index(entries, ...)  -- build the root ``{toctree}`` index.md
build_site(project_root, ...)         -- full MyST site build pipeline (nested)

The site structure is folder-defined: ``docs/consumer/**`` (the source tree)
maps 1:1 to ``userdocs/guide/**`` (the output tree).  ``site-nav.yml`` slims
to a recursive ``show:`` list expressing only inclusion + order + optional
landing; titles, output paths, and doc-ids derive from the docs/paths.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


# Matches a Markdown inline link: [text](target)
_INLINE_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def _strip_docid_links(markdown: str) -> str:
    """Reduce links that target an internal doc-id to plain text.

    A doc-id target contains ``::`` (e.g.
    ``axiom_graph::docs.pev-requests.foo``). Such links are internal
    references that must not leak into the public consumer site, so the
    link is collapsed to its visible text. Every other link -- relative
    page links, external URLs, intra-page anchors -- is left untouched.

    Args:
        markdown: Markdown source possibly containing inline links.

    Returns:
        Markdown with doc-id links flattened to their link text.
    """

    def _replace(match: re.Match[str]) -> str:
        text, target = match.group(1), match.group(2)
        return text if "::" in target else match.group(0)

    return _INLINE_LINK_RE.sub(_replace, markdown)


# ---------------------------------------------------------------------------
# Consumer Markdown renderer
# ---------------------------------------------------------------------------


def render_doc_consumer(title: str, sections: list[dict]) -> str:
    """Render DocJSON sections to clean consumer-facing Markdown.

    Produces headings and content only -- no section-ID HTML comments,
    no ``**Linked nodes:**`` blocks, no staleness badges.

    Args:
        title: Document title (rendered as ``# title``).
        sections: List of section dicts.  Each dict should have keys:
            ``heading`` (str), ``content`` (str | None), ``level`` (int,
            default 2), and optionally ``sections`` (list of child dicts).

    Returns:
        Clean Markdown string.
    """
    lines: list[str] = [f"# {title}", ""]
    _render_sections(sections, lines)
    return "\n".join(lines)


def _render_sections(sections: list[dict], lines: list[str]) -> None:
    """Recursively render sections into the lines list.

    Args:
        sections: List of section dicts to render.
        lines: Accumulator list of output lines (mutated in place).
    """
    for sec in sections:
        heading = sec.get("heading", "")
        content = sec.get("content") or ""
        level = sec.get("level") or 2
        # Clamp level to valid Markdown heading range
        level = max(2, min(level, 6))
        prefix = "#" * level

        # A section with an empty heading renders content-only (no heading
        # line) -- lets a ported README/landing doc place lead content (badges,
        # tagline, intro paragraph) directly under the doc title without a
        # duplicate ``## Title`` heading.
        if heading:
            lines.append(f"{prefix} {heading}")
            lines.append("")
        if content:
            lines.append(content)
            lines.append("")

        # Recurse into nested sections
        children = sec.get("sections")
        if children:
            _render_sections(children, lines)


# ---------------------------------------------------------------------------
# Site navigation config loading and validation
# ---------------------------------------------------------------------------


def load_site_nav(nav_path: Path) -> dict:
    """Load and parse a site navigation YAML file.

    Args:
        nav_path: Path to the ``site-nav.yml`` file.

    Returns:
        Parsed nav data as a dict.

    Raises:
        FileNotFoundError: If the nav file does not exist.
        yaml.YAMLError: If the file is not valid YAML.
    """
    if not nav_path.exists():
        raise FileNotFoundError(f"Nav file not found: {nav_path}")
    with nav_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Nav file must contain a YAML mapping, got {type(data).__name__}")
    return data


# ---------------------------------------------------------------------------
# Slim nav resolution model
# ---------------------------------------------------------------------------
#
# The v2 slim ``site-nav.yml`` shape::
#
#     site_name: Axiom-Graph
#     root: docs/consumer          # source publish boundary
#     show:                        # presence = published; list order = order
#       - getting-started          # bare string = leaf doc
#       - features:                # single-key mapping = section folder
#           landing: overview      # optional: named landing doc
#           show:
#             - staleness
#             - docs-system
#
# The folder structure under ``root`` IS the site structure; output paths and
# doc-ids derive from each entry's path.  ``show:`` is the explicit publish
# gate (a doc on disk but absent from ``show:`` is not published) and its list
# order is the display/toctree order.


@dataclass
class NavLeaf:
    """A leaf doc page in the slim nav.

    Attributes:
        stem: The path stem of this entry relative to its folder (e.g.
            ``staleness``).
        rel_path: POSIX path relative to the source root, sans ``.json``
            (e.g. ``features/staleness``).
        doc_id: Resolved index doc-id for this entry.
        output_path: POSIX ``.md`` output path relative to the output dir
            (e.g. ``features/staleness.md``).
    """

    stem: str
    rel_path: str
    doc_id: str
    output_path: str


@dataclass
class NavFolder:
    """A section folder in the slim nav.

    Attributes:
        stem: The folder's name relative to its parent (e.g. ``features``).
        rel_path: POSIX folder path relative to the source root.
        landing: Optional explicit landing stem from ``landing:``.
        children: Ordered list of child entries (leaves and sub-folders).
    """

    stem: str
    rel_path: str
    landing: str | None
    children: list


def _root_to_prefix(root: str) -> str:
    """Derive the doc-id dotted prefix from the nav ``root`` source path.

    ``docs/consumer`` -> ``consumer`` (the leading ``docs`` segment is the
    scanner's docs dir and is consumed by the ``::docs.`` id prefix).

    Args:
        root: The nav ``root`` value, a project-relative POSIX path.

    Returns:
        Dotted prefix for doc-ids under this publish boundary.
    """
    parts = [p for p in root.replace("\\", "/").split("/") if p]
    if parts and parts[0] == "docs":
        parts = parts[1:]
    return ".".join(parts)


def parse_show(show: list, project_id: str, prefix: str, parent_rel: str = "") -> list:
    """Recursively parse a slim ``show:`` list into NavLeaf / NavFolder entries.

    Args:
        show: The ``show:`` list (strings and single-key mappings).
        project_id: The project identifier for doc-id derivation.
        prefix: The doc-id dotted prefix derived from the nav ``root``.
        parent_rel: POSIX path of the parent folder relative to the source
            root (``""`` at top level).

    Returns:
        Ordered list of :class:`NavLeaf` and :class:`NavFolder` entries.

    Raises:
        ValueError: If an entry is neither a string nor a single-key mapping.
    """
    entries: list = []
    for raw in show or []:
        if isinstance(raw, str):
            stem = raw
            rel_path = f"{parent_rel}/{stem}" if parent_rel else stem
            doc_id = f"{project_id}::docs.{prefix}.{rel_path.replace('/', '.')}"
            entries.append(
                NavLeaf(
                    stem=stem,
                    rel_path=rel_path,
                    doc_id=doc_id,
                    output_path=f"{rel_path}.md",
                )
            )
        elif isinstance(raw, dict):
            if len(raw) != 1:
                raise ValueError(f"Section folder entry must be a single-key mapping, got keys {list(raw)}")
            ((stem, body),) = raw.items()
            body = body or {}
            rel_path = f"{parent_rel}/{stem}" if parent_rel else stem
            child_show = body.get("show", []) if isinstance(body, dict) else []
            landing = body.get("landing") if isinstance(body, dict) else None
            children = parse_show(child_show, project_id, prefix, rel_path)
            entries.append(NavFolder(stem=stem, rel_path=rel_path, landing=landing, children=children))
        else:
            raise ValueError(f"Nav entry must be a string or single-key mapping, got {type(raw).__name__}")
    return entries


def validate_site_nav(nav_data: dict, db_path: Path | None = None, source_root: Path | None = None) -> list[str]:
    """Validate the structure of parsed slim site nav data.

    Checks the v2 slim schema: required top-level keys (``site_name``,
    ``root``, ``show``); each ``show`` entry is a leaf string or a single-key
    section-folder mapping; section folders may not carry ``landing:`` when an
    ``index.json`` exists in the folder on disk; and every leaf stem resolves
    to an indexed doc-id.

    Args:
        nav_data: Parsed nav data dict from :func:`load_site_nav`.
        db_path: Optional path to the index DB.  When provided, leaf stems are
            checked for resolvability against indexed doc-ids.
        source_root: Optional absolute path to the source publish root (the
            ``root:`` folder).  When provided, ``index.json``/``landing:``
            conflicts are detected on disk.

    Returns:
        List of error strings.  Empty list means valid.
    """
    from axiom_graph.index import db as _db

    errors: list[str] = []

    if "site_name" not in nav_data:
        errors.append("Missing required key: site_name")
    if "root" not in nav_data:
        errors.append("Missing required key: root")
    if "show" not in nav_data:
        errors.append("Missing required key: show")
        return errors

    show = nav_data["show"]
    if not isinstance(show, list):
        errors.append("'show' must be a list")
        return errors

    prefix = _root_to_prefix(str(nav_data.get("root", "docs/consumer")))
    project_id = nav_data.get("_project_id", "axiom_graph")

    try:
        entries = parse_show(show, project_id, prefix)
    except ValueError as exc:
        errors.append(str(exc))
        return errors

    def _walk(items: list, folder_path: Path | None) -> None:
        for entry in items:
            if isinstance(entry, NavLeaf):
                if db_path is not None and _db.get_node(db_path, entry.doc_id) is None:
                    errors.append(f"Unresolvable stem '{entry.rel_path}': no indexed doc with id {entry.doc_id}")
            elif isinstance(entry, NavFolder):
                sub_dir = (folder_path / entry.stem) if folder_path is not None else None
                if entry.landing is not None and sub_dir is not None and (sub_dir / "index.json").exists():
                    errors.append(
                        f"Section folder '{entry.rel_path}' has both index.json and "
                        f"landing: {entry.landing} -- ambiguous landing"
                    )
                _walk(entry.children, sub_dir)

    _walk(entries, source_root)
    return errors


# ---------------------------------------------------------------------------
# MyST toctree index + provenance
# ---------------------------------------------------------------------------


def _page_stem(output: str) -> str:
    """Return a page's toctree stem (filename without the ``.md`` suffix)."""
    return output[:-3] if output.endswith(".md") else output


def _entry_toctree_stem(entry) -> str:
    """Return the top-level toctree stem for a parsed nav entry.

    A leaf points at its own page; a section folder points at the folder's
    landing page (``<folder>/index``), which itself carries a nested toctree
    of the folder's direct children.

    Args:
        entry: A :class:`NavLeaf` or :class:`NavFolder`.

    Returns:
        The toctree stem (POSIX, no ``.md`` suffix).
    """
    if isinstance(entry, NavLeaf):
        return _page_stem(entry.output_path)
    return f"{entry.rel_path}/index"


def generate_toctree_index(entries: list, site_name: str = "Guide") -> str:
    """Generate the root ``index.md`` for the guide from parsed slim-nav entries.

    Emits a single ``{toctree}`` listing the top-level ``show`` entries in
    order.  Leaf entries list their page stem; section folders list their
    landing page (``<folder>/index``), which carries its own nested toctree of
    direct children.  This index page is itself wired into the hand-authored
    ``userdocs/index.md`` toctree.

    Args:
        entries: Top-level parsed nav entries (NavLeaf / NavFolder).
        site_name: Heading title for the index page.

    Returns:
        MyST Markdown for the generated ``index.md``.
    """
    lines: list[str] = ["# Guide", ""]
    if entries:
        lines.append("```{toctree}")
        lines.append(":maxdepth: 2")
        lines.append("")
        lines.extend(_entry_toctree_stem(e) for e in entries)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _provenance_stamp(doc_id: str, content_hash: str) -> str:
    """Return the HTML-comment provenance line prepended to a generated page."""
    return f"<!-- generated from {doc_id} @ {content_hash}; do not edit -->"


#: Marker line that flags a file as generated by this pipeline.  Any file whose
#: text begins with the provenance comment prefix is safe to overwrite; an
#: existing file lacking it is hand-authored and guarded (see path safety).
_STAMP_PREFIX = "<!-- generated from "


def _is_stamped(text: str) -> bool:
    """Return True iff *text* opens with a render-pipeline provenance stamp."""
    return text.lstrip().startswith(_STAMP_PREFIX)


# ---------------------------------------------------------------------------
# Site build result
# ---------------------------------------------------------------------------


@dataclass
class SiteBuildResult:
    """Summary of a site build run."""

    pages_rendered: int = 0
    warnings: list[str] = field(default_factory=list)
    output_dir: str = ""
    #: For single-file renders (:func:`render_doc_to_file`): the central
    #: manifest entry ``{doc_id, hash}`` for the written file, else ``None``.
    manifest_entry: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Core site build pipeline
# ---------------------------------------------------------------------------


def build_site(
    project_root: Path,
    nav_path: Path | None = None,
    output_dir: Path | None = None,
    run_sphinx_build: bool = False,
    fmt: str = "sphinx",
    overwrite: bool = False,
) -> SiteBuildResult:
    """Render the consumer narrative into committed MyST pages for Sphinx.

    Loads the slim nav config and walks its recursive ``show:`` tree.  For
    each referenced doc it renders clean consumer Markdown, strips internal
    doc-id links, prepends a provenance stamp, and writes the page to its
    mirrored nested output path under *output_dir* (so the output tree
    mirrors the ``docs/consumer/**`` source tree 1:1).  Each section folder
    gets a landing page (``index.json`` convention / ``landing:`` override /
    synthetic ``# <Folder Name>``) with an appended ``{toctree}`` of its
    direct children.  Also generates the top-level ``index.md`` and a
    ``.render-manifest.json`` (nested output path -> doc_id + content hash)
    so the publish pipeline can detect drift.  This is the shared function
    called by both the CLI and MCP tool.

    Because output mirrors source 1:1, inter-page relative ``.md`` links are
    authored-correct for their page's location (a root page links down into
    ``features/...``; a feature page links up via ``../``) and resolve as
    Sphinx documents.

    Args:
        project_root: Absolute path to the project root.
        nav_path: Path to the site nav YAML file.  Defaults to
            ``{project_root}/{cfg.site.nav_file}``.
        output_dir: Directory the MyST pages are written to.  Defaults to
            ``{project_root}/userdocs/guide``.
        run_sphinx_build: If True, run ``sphinx-build`` over the parent
            ``userdocs`` directory after generating the pages.  Only honored
            when ``fmt == "sphinx"``.
        fmt: Output flavor.  ``"sphinx"`` (the default) emits MyST/toctree
            scaffolding byte-identical to the historical guide build.
            ``"plain"`` emits GitHub-friendly Markdown — folder landings carry
            bullet lists of relative ``.md`` links instead of ``{toctree}``
            directives, and a contentless section folder (synthetic-landing
            case) emits no stub: its children render inline under a nested
            heading + link list on the parent.
        overwrite: When ``True``, an existing un-stamped (hand-authored) file
            at an output path is replaced.  When ``False`` (the default) such
            a file is warn-and-skipped (path safety).  Generated files always
            carry a provenance stamp and are overwritten freely.

    Returns:
        SiteBuildResult with counts and warnings.
    """
    from axiom_graph.index import db as _db

    from axiom_graph.config import db_path_for

    from axiom_graph.config import AxiomGraphConfig

    root = Path(project_root).resolve()
    db_path = db_path_for(root)
    cfg = AxiomGraphConfig.load(root)
    project_id = cfg.project_id or root.name

    if nav_path is None:
        nav_path = root / cfg.site.nav_file
    if output_dir is None:
        output_dir = root / "userdocs" / "guide"

    result = SiteBuildResult(output_dir=str(output_dir))

    # Load and validate nav (slim v2 schema)
    nav_data = load_site_nav(nav_path)
    nav_data["_project_id"] = project_id

    root_rel = str(nav_data.get("root", "docs/consumer"))
    source_root = (root / root_rel).resolve()

    errors = validate_site_nav(nav_data, db_path=db_path, source_root=source_root)
    if errors:
        result.warnings.extend(f"Nav validation: {e}" for e in errors)
        return result

    output_dir.mkdir(parents=True, exist_ok=True)

    prefix = _root_to_prefix(root_rel)
    entries = parse_show(nav_data.get("show", []), project_id, prefix)

    manifest: dict[str, dict[str, str]] = {}

    def _render_leaf(leaf: NavLeaf, title_override: str | None = None) -> bool:
        """Render one leaf doc to its mirrored nested output path.

        Args:
            leaf: The leaf entry to render.
            title_override: Optional title override (unused for normal leaves;
                landings reuse the doc's own title).

        Returns:
            True if the page was rendered, False if the doc was missing.
        """
        doc_node = _db.get_node(db_path, leaf.doc_id)
        if doc_node is None:
            result.warnings.append(f"Doc not found in index: {leaf.doc_id}")
            return False
        sections = _db.get_doc_sections(db_path, leaf.doc_id)
        title = title_override or doc_node.title
        body = _strip_docid_links(render_doc_consumer(title, sections))
        return _write_page(leaf.output_path, leaf.doc_id, body, extra_lines=None)

    def _write_page(output_path: str, doc_id: str, body: str, extra_lines: list[str] | None) -> bool:
        """Write a provenance-stamped page (optionally appending toctree/links)."""
        if extra_lines:
            body = body.rstrip() + "\n\n" + "\n".join(extra_lines) + "\n"
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        stamped = f"{_provenance_stamp(doc_id, content_hash)}\n\n{body}"
        out_path = output_dir / output_path
        # Path safety: refuse to overwrite a hand-authored (un-stamped) file
        # unless the caller opted in.  Generated files always carry the stamp.
        if out_path.exists() and not overwrite and not _is_stamped(out_path.read_text(encoding="utf-8")):
            result.warnings.append(f"Refusing to overwrite un-stamped file (set overwrite): {output_path}")
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(stamped, encoding="utf-8")
        manifest[output_path] = {"doc_id": doc_id, "hash": content_hash}
        result.pages_rendered += 1
        return True

    def _leaf_title(leaf: NavLeaf) -> str:
        """Return a display title for a leaf (doc title or humanised stem)."""
        node = _db.get_node(db_path, leaf.doc_id)
        if node is not None and node.title:
            return node.title
        return leaf.stem.replace("-", " ").replace("_", " ").title()

    def _folder_is_contentless(folder: NavFolder) -> bool:
        """True when a folder has no landing doc (synthetic-landing case c)."""
        folder_dir = source_root / folder.rel_path
        return not (folder_dir / "index.json").exists() and not folder.landing

    def _child_toctree(folder: NavFolder) -> list[str]:
        """Build a ``{toctree}`` listing a folder's DIRECT children only.

        Stems are relative to the landing page (which lives at
        ``<folder>/index.md``), so direct-child stems are bare (no ``<folder>/``
        prefix, no grandchildren).
        """
        lines = ["```{toctree}", ":maxdepth: 1", ""]
        for child in folder.children:
            if isinstance(child, NavLeaf):
                lines.append(child.stem)
            else:  # NavFolder -> its own landing index
                lines.append(f"{child.stem}/index")
        lines.append("```")
        return lines

    def _child_linklist(folder: NavFolder, level: int, link_prefix: str = "") -> list[str]:
        """Build a plain-Markdown link list of a folder's DIRECT children.

        Links are relative to *link_prefix* (empty when the list lives on the
        folder's own ``index.md``; the folder stem when expanded inline on an
        ancestor page).  A leaf child links to ``<prefix><stem>.md`` and a
        sub-folder with a landing links to ``<prefix><stem>/index.md``.  A
        *contentless* sub-folder (no landing doc) has no stub to link to: it is
        expanded inline as a nested heading + its own child link list,
        recursively, with the prefix extended by the sub-folder stem.

        Args:
            folder: The folder whose children to list.
            level: Markdown heading level for inline-expanded sub-folders.
            link_prefix: POSIX path prefix prepended to each child link target.
        """
        lines: list[str] = []
        for child in folder.children:
            if isinstance(child, NavLeaf):
                lines.append(f"- [{_leaf_title(child)}]({link_prefix}{child.stem}.md)")
            elif _folder_is_contentless(child):
                # No stub file to link to -- expand inline.
                heading = "#" * min(level, 6)
                folder_title = child.stem.replace("-", " ").replace("_", " ").title()
                lines.append("")
                lines.append(f"{heading} {folder_title}")
                lines.append("")
                lines.extend(_child_linklist(child, level + 1, f"{link_prefix}{child.stem}/"))
            else:
                folder_title = child.stem.replace("-", " ").replace("_", " ").title()
                lines.append(f"- [{folder_title}]({link_prefix}{child.stem}/index.md)")
        return lines

    def _folder_extra(folder: NavFolder, level: int = 3) -> list[str]:
        """Return the trailing block (toctree or link list) for a folder landing."""
        if fmt == "plain":
            return _child_linklist(folder, level)
        return _child_toctree(folder)

    def _render_folder(folder: NavFolder) -> None:
        """Render a section folder's landing page + nested child listing.

        Landing precedence: (a) ``index.json`` in the folder -> render that
        doc; else (b) ``landing: <stem>`` -> render that named doc; else (c)
        a synthetic ``# <Folder Name>`` page.  In sphinx mode a ``{toctree}``
        of direct children is appended in all cases.  In plain mode (a)/(b)
        append a Markdown link list, and (c) emits NO stub file -- the parent
        renders the folder inline (see :func:`_child_linklist`).
        """
        folder_dir = source_root / folder.rel_path
        landing_output = f"{folder.rel_path}/index.md"

        index_doc_id = f"{project_id}::docs.{prefix}.{folder.rel_path.replace('/', '.')}.index"
        if (folder_dir / "index.json").exists():
            # (a) index.json convention
            doc_node = _db.get_node(db_path, index_doc_id)
            if doc_node is not None:
                sections = _db.get_doc_sections(db_path, index_doc_id)
                body = _strip_docid_links(render_doc_consumer(doc_node.title, sections))
                _write_page(landing_output, index_doc_id, body, extra_lines=_folder_extra(folder))
            else:
                result.warnings.append(f"Doc not found in index: {index_doc_id}")
        elif folder.landing:
            # (b) named landing doc
            landing_rel = f"{folder.rel_path}/{folder.landing}"
            landing_doc_id = f"{project_id}::docs.{prefix}.{landing_rel.replace('/', '.')}"
            doc_node = _db.get_node(db_path, landing_doc_id)
            if doc_node is not None:
                sections = _db.get_doc_sections(db_path, landing_doc_id)
                body = _strip_docid_links(render_doc_consumer(doc_node.title, sections))
                _write_page(landing_output, landing_doc_id, body, extra_lines=_folder_extra(folder))
            else:
                result.warnings.append(f"Doc not found in index: {landing_doc_id}")
        elif fmt == "plain":
            # (c) contentless folder in plain mode -> NO stub file.  The parent
            # link list expands this folder inline; only its children render.
            pass
        else:
            # (c) synthetic landing (sphinx) -> # <Folder Name> + toctree
            folder_title = folder.stem.replace("-", " ").replace("_", " ").title()
            body = f"# {folder_title}\n"
            _write_page(
                landing_output,
                f"{project_id}::docs.{prefix}.{folder.rel_path.replace('/', '.')}",
                body,
                extra_lines=_child_toctree(folder),
            )

        for child in folder.children:
            if isinstance(child, NavLeaf):
                _render_leaf(child)
            else:
                _render_folder(child)

    # A root-level ``index`` leaf (docs/consumer/index.json) is the guide's own
    # landing page.  Render it as the page body with the nav toctree appended --
    # mirroring the folder index.json convention -- rather than letting the
    # generated toctree clobber the authored thesis page.
    root_index = next(
        (e for e in entries if isinstance(e, NavLeaf) and e.rel_path == "index"),
        None,
    )
    body_entries = [e for e in entries if e is not root_index]

    for entry in body_entries:
        if isinstance(entry, NavLeaf):
            _render_leaf(entry)
        else:
            _render_folder(entry)

    def _top_level_block() -> list[str]:
        """Build the top-level navigation block (toctree or plain link list)."""
        if fmt == "plain":
            lines: list[str] = []
            for e in body_entries:
                if isinstance(e, NavLeaf):
                    lines.append(f"- [{_leaf_title(e)}]({e.output_path})")
                elif _folder_is_contentless(e):
                    heading_title = e.stem.replace("-", " ").replace("_", " ").title()
                    lines.append("")
                    lines.append(f"## {heading_title}")
                    lines.append("")
                    # children are one level deeper relative to the root index,
                    # so prefix the folder path on each child link.
                    lines.extend(_child_linklist(e, 3, f"{e.stem}/"))
                else:
                    folder_title = e.stem.replace("-", " ").replace("_", " ").title()
                    lines.append(f"- [{folder_title}]({e.rel_path}/index.md)")
            return lines
        toctree = ["```{toctree}", ":maxdepth: 2", ""]
        toctree.extend(_entry_toctree_stem(e) for e in body_entries)
        toctree.append("```")
        return toctree

    root_index_node = _db.get_node(db_path, root_index.doc_id) if root_index is not None else None
    if root_index is not None and root_index_node is not None:
        # Thesis landing: authored index.json body + appended nav block of the
        # remaining top-level entries (no self-reference to index).
        sections = _db.get_doc_sections(db_path, root_index.doc_id)
        body = _strip_docid_links(render_doc_consumer(root_index_node.title, sections))
        _write_page(root_index.output_path, root_index.doc_id, body, extra_lines=_top_level_block())
    else:
        if root_index is not None:
            result.warnings.append(f"Doc not found in index: {root_index.doc_id}")
        # Generated top-level index that nests the pages under the nav.
        site_name = nav_data.get("site_name", "Guide")
        if fmt == "plain":
            index_lines = [f"# {site_name}", ""]
            index_lines.extend(_top_level_block())
            index_body = "\n".join(index_lines).rstrip() + "\n"
        else:
            index_body = generate_toctree_index(body_entries, site_name)
        (output_dir / "index.md").write_text(index_body, encoding="utf-8")

    # Write the provenance manifest for drift detection in the sync pipeline
    (output_dir / ".render-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # Optionally run sphinx-build over the parent userdocs directory
    if run_sphinx_build and fmt == "sphinx":
        import subprocess

        userdocs_dir = output_dir.parent
        html_dir = userdocs_dir / "_build" / "html"
        try:
            subprocess.run(
                ["sphinx-build", "-b", "html", str(userdocs_dir), str(html_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError:
            result.warnings.append("sphinx-build not found -- install with: pip install sphinx")
        except subprocess.CalledProcessError as exc:
            result.warnings.append(f"sphinx-build failed: {exc.stderr}")
        except subprocess.TimeoutExpired:
            result.warnings.append("sphinx-build timed out after 600s")

    return result


# ---------------------------------------------------------------------------
# Single-doc renderer
# ---------------------------------------------------------------------------


def _resolve_under_root(project_root: Path, output: str) -> Path:
    """Resolve *output* to an absolute path guaranteed to live under root.

    Args:
        project_root: Absolute project root.
        output: Output path relative to the project root (or absolute).

    Returns:
        The resolved absolute output path.

    Raises:
        ValueError: If the resolved path escapes *project_root*.
    """
    root = Path(project_root).resolve()
    candidate = Path(output)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"Output path escapes project root: {output}")
    return resolved


def render_doc_to_file(
    project_root: Path,
    doc_id: str,
    output_path: Path | str,
    *,
    fmt: str = "plain",
    stamp: bool = True,
    overwrite: bool = False,
) -> SiteBuildResult:
    """Render a single indexed DocJSON doc to one output file.

    Loads the doc node, renders clean consumer Markdown, strips internal
    doc-id links, optionally prepends a provenance stamp, and writes the file.
    Unlike :func:`build_site`, this emits no landing page, no ``{toctree}``,
    and no per-directory manifest -- it is the single-file render path used for
    targets such as the repo-root ``README.md``.

    A doc-id that is not in the index is warn-and-skipped (no file written, no
    crash).  Path safety is enforced: the output must resolve under
    *project_root*, and an existing un-stamped file is warn-and-skipped unless
    *overwrite* is True.

    Args:
        project_root: Absolute path to the project root.
        doc_id: The DocJSON doc-id to render.
        output_path: Output file path, relative to *project_root* or absolute.
        fmt: Output flavor (currently both ``plain`` and ``sphinx`` produce the
            same single-file body; reserved for parity with :func:`build_site`).
        stamp: When True (the default), prepend the provenance stamp.
        overwrite: When True, replace an existing un-stamped file.

    Returns:
        :class:`SiteBuildResult` with ``pages_rendered`` 0 or 1 and any
        warnings.  Also records a central-manifest entry (handled by the
        orchestrator) via the returned result's output_dir.
    """
    from axiom_graph.config import db_path_for
    from axiom_graph.index import db as _db

    root = Path(project_root).resolve()
    db_path = db_path_for(root)
    out_path = _resolve_under_root(root, str(output_path))
    rel_output = out_path.relative_to(root).as_posix()
    result = SiteBuildResult(output_dir=rel_output)

    doc_node = _db.get_node(db_path, doc_id)
    if doc_node is None:
        result.warnings.append(f"Doc not found in index: {doc_id}")
        return result

    sections = _db.get_doc_sections(db_path, doc_id)
    body = _strip_docid_links(render_doc_consumer(doc_node.title, sections))
    body = body.rstrip() + "\n"
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]

    if stamp:
        text = f"{_provenance_stamp(doc_id, content_hash)}\n\n{body}"
    else:
        text = body

    if out_path.exists() and not overwrite and not _is_stamped(out_path.read_text(encoding="utf-8")):
        result.warnings.append(f"Refusing to overwrite un-stamped file (set overwrite): {rel_output}")
        return result

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    result.pages_rendered = 1
    # Stash provenance for the central manifest (consumed by render_targets).
    result.manifest_entry = {"doc_id": doc_id, "hash": content_hash}
    return result


# ---------------------------------------------------------------------------
# Multi-target orchestrator
# ---------------------------------------------------------------------------


def resolve_targets(project_root: Path):
    """Resolve the effective render targets for a project.

    Reads ``[[axiom_graph.site.targets]]`` from ``axiom-graph.toml``.  When the
    list is non-empty it is authoritative and returned verbatim.  When it is
    empty (or absent) a single implicit ``guide`` target is synthesised:
    ``SiteTarget(name="guide", output="userdocs/guide", format="sphinx",
    nav=<cfg.site.nav_file>)`` -- note this resolves to ``userdocs/guide``
    (the historical guide location), NOT ``cfg.site.output_dir``.

    Args:
        project_root: Absolute path to the project root.

    Returns:
        Ordered list of :class:`axiom_graph.config.SiteTarget`.
    """
    from axiom_graph.config import AxiomGraphConfig, SiteTarget

    root = Path(project_root).resolve()
    cfg = AxiomGraphConfig.load(root)
    if cfg.site.targets:
        return list(cfg.site.targets)
    return [
        SiteTarget(
            name="guide",
            output="userdocs/guide",
            format="sphinx",
            nav=cfg.site.nav_file,
        )
    ]


@dataclass
class RenderTargetResult:
    """Outcome of rendering one :class:`~axiom_graph.config.SiteTarget`."""

    name: str
    format: str
    output: str
    pages_rendered: int = 0
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False


def render_targets(
    project_root: Path,
    *,
    only: list[str] | None = None,
    run_sphinx_build: bool = False,
) -> list[RenderTargetResult]:
    """Render every configured target (or a named subset).

    Resolves the targets via :func:`resolve_targets`, filters to *only* when
    given, then dispatches each: ``doc`` targets go through
    :func:`render_doc_to_file`; ``nav`` targets through :func:`build_site` with
    the target's ``format``.  ``sphinx-build`` fires only for sphinx-format
    targets (and only when *run_sphinx_build* is set).

    Single-file (``doc``) targets are recorded in a central manifest at
    ``{project_root}/.axiom_graph/render-manifest.json`` keyed by repo-relative
    output path -> ``{doc_id, hash, target, fmt}``.  Subtree (``nav``) targets
    keep their own co-located ``.render-manifest.json`` (unchanged).

    Args:
        project_root: Absolute path to the project root.
        only: Optional list of target names to render; others are skipped.
        run_sphinx_build: When True, run ``sphinx-build`` for sphinx targets.

    Returns:
        One :class:`RenderTargetResult` per resolved target (including skipped
        ones, flagged ``skipped=True``).
    """
    root = Path(project_root).resolve()
    targets = resolve_targets(root)
    only_set = set(only) if only else None

    results: list[RenderTargetResult] = []
    central: dict[str, dict[str, str]] = {}

    for target in targets:
        if only_set is not None and target.name not in only_set:
            results.append(
                RenderTargetResult(name=target.name, format=target.format, output=target.output, skipped=True)
            )
            continue

        if target.doc is not None:
            sub = render_doc_to_file(
                root,
                target.doc,
                target.output,
                fmt=target.format,
                overwrite=target.overwrite,
            )
            entry = getattr(sub, "manifest_entry", None)
            if entry is not None:
                rel = Path(sub.output_dir).as_posix()
                central[rel] = {**entry, "target": target.name, "fmt": target.format}
            results.append(
                RenderTargetResult(
                    name=target.name,
                    format=target.format,
                    output=target.output,
                    pages_rendered=sub.pages_rendered,
                    warnings=list(sub.warnings),
                )
            )
        else:
            nav_path = _resolve_under_root(root, target.nav)
            output_dir = _resolve_under_root(root, target.output)
            sub = build_site(
                root,
                nav_path=nav_path,
                output_dir=output_dir,
                run_sphinx_build=run_sphinx_build,
                fmt=target.format,
                overwrite=target.overwrite,
            )
            results.append(
                RenderTargetResult(
                    name=target.name,
                    format=target.format,
                    output=target.output,
                    pages_rendered=sub.pages_rendered,
                    warnings=list(sub.warnings),
                )
            )

    # Merge single-file entries into the central manifest (preserve entries for
    # targets not rendered this run).
    if central:
        manifest_path = root / ".axiom_graph" / "render-manifest.json"
        existing: dict[str, dict[str, str]] = {}
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "central render manifest unreadable, resetting to empty: %s: %s",
                    manifest_path,
                    exc,
                )
                existing = {}
        existing.update(central)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")

    return results
