"""Doc-tree corpus factory for doc-ID projection, collision, and migration tests.

Every shape writes a self-contained project under a caller-supplied
directory: an ``axiom-graph.toml`` naming the configured ``docs_dirs``
roots plus the DocJSON files that give the shape its character.  Nothing
here builds an index -- callers decide whether a shape needs one.

The shapes deliberately cover the doc-tree characteristics that doc-ID
derivation is sensitive to:

``clean_multi_root``
    Two configured roots, no overlapping identities.
``class1_collision``
    The same relative path under two roots -- both derive one doc ID.
``class2_collision``
    ``adrs/013-x.json`` beside ``adrs.013-x.json`` in one root -- the
    ``/`` -> ``.`` joiner is not injective, so both derive one doc ID.
``dotted_filenames``
    Filenames carrying extra dots in the stem.
``data_files_beside_docs``
    Ordinary JSON, a corrupt file, and a section-less document sharing a
    docs root with a real one -- the populations a doc-ID signal has to
    tell apart.
``nested_sections``
    Sections nested two levels deep (the scanner's maximum).
``deep_paths``
    A document several directories below its root.
``linked_sections``
    A document whose sections link to another document's *section* and
    to a code node, so link rewriting can be told apart from link
    preservation.
``many_docs``
    An arbitrary number of flat documents, for cost/scaling assertions.
"""

from __future__ import annotations

import json
from pathlib import Path


DEFAULT_PROJECT_ID = "proj"


# ---------------------------------------------------------------------------
# Low-level writers
# ---------------------------------------------------------------------------


def write_toml(
    project_root: Path,
    docs_dirs: list[str],
    *,
    project_id: str = DEFAULT_PROJECT_ID,
    extra: str = "",
) -> Path:
    """Write an ``axiom-graph.toml`` naming *docs_dirs* as the doc roots.

    Args:
        project_root: Directory the file is written into (created if absent).
        docs_dirs: Configured docs roots, as written in the TOML.
        project_id: Project ID prefix for every derived node ID.
        extra: Optional raw TOML appended verbatim.

    Returns:
        Path to the written ``axiom-graph.toml``.
    """
    project_root.mkdir(parents=True, exist_ok=True)
    rendered = ", ".join(json.dumps(d) for d in docs_dirs)
    text = f'[axiom_graph]\nproject_id = "{project_id}"\n\n[axiom_graph.scan]\ndocs_dirs = [{rendered}]\n{extra}'
    path = project_root / "axiom-graph.toml"
    path.write_text(text, encoding="utf-8")
    return path


def write_doc(
    project_root: Path,
    rel_path: str,
    *,
    title: str | None = None,
    sections: list[dict] | None = None,
) -> Path:
    """Write one DocJSON file at *rel_path* under *project_root*.

    Args:
        project_root: Project root the path is relative to.
        rel_path: POSIX-style relative path ending in ``.json``.
        title: Document title.  Defaults to the file stem.
        sections: Section dicts.  Defaults to a single ``overview`` section.

    Returns:
        Absolute path to the written file.
    """
    path = project_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "title": title if title is not None else path.stem,
        "sections": sections
        if sections is not None
        else [{"id": "overview", "heading": "Overview", "content": f"Content of {rel_path}."}],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def write_raw(project_root: Path, rel_path: str, text: str) -> Path:
    """Write raw text at *rel_path* -- for files that are not DocJSON at all.

    Args:
        project_root: Project root the path is relative to.
        rel_path: POSIX-style relative path.
        text: File contents, written verbatim.

    Returns:
        Absolute path to the written file.
    """
    path = project_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_json(project_root: Path, rel_path: str, payload) -> Path:
    """Write an arbitrary JSON payload -- valid JSON, not a DocJSON document.

    Args:
        project_root: Project root the path is relative to.
        rel_path: POSIX-style relative path ending in ``.json``.
        payload: Any JSON-serialisable value.

    Returns:
        Absolute path to the written file.
    """
    return write_raw(project_root, rel_path, json.dumps(payload, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def clean_multi_root(project_root: Path, *, project_id: str = DEFAULT_PROJECT_ID) -> Path:
    """Two configured roots, distinct identities, nested and flat documents."""
    write_toml(project_root, ["docs", "specs"], project_id=project_id)
    write_doc(project_root, "docs/alpha.json")
    write_doc(
        project_root,
        "docs/adrs/013-envelope.json",
        title="ADR 013",
        sections=[
            {"id": "context", "heading": "Context", "content": "Why."},
            {"id": "decision", "heading": "Decision", "content": "What."},
        ],
    )
    write_doc(project_root, "specs/beta.json")
    return project_root


def class1_collision(project_root: Path, *, project_id: str = DEFAULT_PROJECT_ID) -> Path:
    """The same relative path under two roots -- one derived doc ID, two files."""
    write_toml(project_root, ["docs", "specs"], project_id=project_id)
    write_doc(project_root, "docs/collide.json", title="From docs")
    write_doc(project_root, "specs/collide.json", title="From specs")
    write_doc(project_root, "docs/unique.json")
    return project_root


def class2_collision(project_root: Path, *, project_id: str = DEFAULT_PROJECT_ID) -> Path:
    """``adrs/013-x.json`` beside ``adrs.013-x.json`` -- one derived doc ID."""
    write_toml(project_root, ["docs"], project_id=project_id)
    write_doc(project_root, "docs/adrs/013-x.json", title="Nested ADR")
    write_doc(project_root, "docs/adrs.013-x.json", title="Flat ADR")
    write_doc(project_root, "docs/unique.json")
    return project_root


def dotted_filenames(project_root: Path, *, project_id: str = DEFAULT_PROJECT_ID) -> Path:
    """One dotted filename beside an ordinary one, no collision between them."""
    write_toml(project_root, ["docs"], project_id=project_id)
    write_doc(project_root, "docs/release.2.1.notes.json", title="Dotted")
    write_doc(project_root, "docs/plain.json", title="Plain")
    return project_root


def data_files_beside_docs(project_root: Path, *, project_id: str = DEFAULT_PROJECT_ID) -> Path:
    """Ordinary JSON, a corrupt file, and a section-less document in one root.

    Four populations a doc-ID signal has to tell apart:

    - ``docs/real.json`` -- an ordinary document,
    - ``docs/data/chart.json`` and ``docs/data.chart.json`` -- valid JSON
      that is not DocJSON.  They would derive one doc ID between them, and
      one of them carries a dotted stem, so any signal that counts them is
      reporting on files that can never become doc nodes,
    - ``docs/broken.json`` -- not valid JSON at all,
    - ``docs/section-less.json`` -- a real document with an empty
      ``sections`` list.

    Args:
        project_root: Directory the project is written into.
        project_id: Project ID prefix.

    Returns:
        The project root.
    """
    write_toml(project_root, ["docs"], project_id=project_id)
    write_doc(project_root, "docs/real.json", title="Real")
    write_json(project_root, "docs/data/chart.json", {"series": [1, 2, 3]})
    write_json(project_root, "docs/data.chart.json", {"series": [4, 5, 6]})
    write_raw(project_root, "docs/broken.json", '{"title": "Truncated", "sections": [')
    write_doc(project_root, "docs/section-less.json", title="Section-less", sections=[])
    return project_root


def nested_sections(project_root: Path, *, project_id: str = DEFAULT_PROJECT_ID) -> Path:
    """A document with sections nested to the scanner's maximum depth."""
    write_toml(project_root, ["docs"], project_id=project_id)
    write_doc(
        project_root,
        "docs/tree.json",
        title="Tree",
        sections=[
            {
                "id": "root",
                "heading": "Root",
                "content": "Top.",
                "sections": [
                    {
                        "id": "child",
                        "heading": "Child",
                        "content": "Middle.",
                        "sections": [
                            {"id": "grandchild", "heading": "Grandchild", "content": "Leaf."},
                        ],
                    },
                ],
            },
            {"id": "sibling", "heading": "Sibling", "content": "Beside root."},
        ],
    )
    return project_root


def deep_paths(project_root: Path, *, project_id: str = DEFAULT_PROJECT_ID) -> Path:
    """A document several directories below its configured root."""
    write_toml(project_root, ["docs"], project_id=project_id)
    write_doc(project_root, "docs/a/b/c/deep.json", title="Deep")
    write_doc(project_root, "docs/shallow.json", title="Shallow")
    return project_root


def linked_sections(
    project_root: Path,
    *,
    project_id: str = DEFAULT_PROJECT_ID,
    code_node_id: str | None = None,
) -> Path:
    """A source document linking to a target document's *section* and to code.

    The target document is ``docs/target.json`` with section ``payload``;
    the source is ``docs/source.json`` whose first section links to
    ``{project_id}::docs.target::payload`` and to a code node.

    Args:
        project_root: Directory the project is written into.
        project_id: Project ID prefix.
        code_node_id: Code-node link target.  Defaults to
            ``{project_id}::pkg.mod::func``.

    Returns:
        The project root.
    """
    code_id = code_node_id or f"{project_id}::pkg.mod::func"
    write_toml(project_root, ["docs"], project_id=project_id)
    write_doc(
        project_root,
        "docs/target.json",
        title="Target",
        sections=[{"id": "payload", "heading": "Payload", "content": "The linked section."}],
    )
    write_doc(
        project_root,
        "docs/source.json",
        title="Source",
        sections=[
            {
                "id": "refs",
                "heading": "Refs",
                "content": "Points elsewhere.",
                "links": [
                    {"node_id": f"{project_id}::docs.target::payload"},
                    {"node_id": code_id},
                ],
            },
            {
                "id": "envelope-ref",
                "heading": "Envelope Ref",
                "content": "Points at the whole document.",
                "links": [{"node_id": f"{project_id}::docs.target"}],
            },
        ],
    )
    return project_root


def many_docs(project_root: Path, count: int, *, project_id: str = DEFAULT_PROJECT_ID) -> Path:
    """*count* flat documents in one root, each with two sections."""
    write_toml(project_root, ["docs"], project_id=project_id)
    for i in range(count):
        write_doc(
            project_root,
            f"docs/doc-{i:03d}.json",
            title=f"Doc {i}",
            sections=[
                {"id": "one", "heading": "One", "content": f"First of {i}."},
                {"id": "two", "heading": "Two", "content": f"Second of {i}."},
            ],
        )
    return project_root


def prose_references(project_root: Path, *, project_id: str = DEFAULT_PROJECT_ID) -> Path:
    """A tree carrying doc-ID references in section prose and in plain files.

    Produces three distinguishable reference kinds:

    - a ``links[].node_id`` entry (rewritten by the migration),
    - a doc ID written into section ``content`` (not rewritten),
    - a doc ID written into a non-DocJSON file (not rewritten).
    """
    write_toml(project_root, ["docs"], project_id=project_id)
    write_doc(
        project_root,
        "docs/target.json",
        title="Target",
        sections=[{"id": "payload", "heading": "Payload", "content": "Linked."}],
    )
    write_doc(
        project_root,
        "docs/guide.json",
        title="Guide",
        sections=[
            {
                "id": "howto",
                "heading": "How To",
                "content": f"Read {project_id}::docs.target::payload before starting.",
                "links": [{"node_id": f"{project_id}::docs.target::payload"}],
            }
        ],
    )
    (project_root / "README.md").write_text(
        f"See {project_id}::docs.target for the overview.\n",
        encoding="utf-8",
    )
    skill_dir = project_root / "skills"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "usage.md").write_text(
        f"Call read_doc on {project_id}::docs.guide::howto.\n",
        encoding="utf-8",
    )
    return project_root
