<!-- generated from axiom_graph::docs/consumer/concepts/docjson @ d37736aba121; do not edit -->

# DocJSON documents

## What DocJSON is

axiom-graph stores documentation as DocJSON: one JSON file per document in your docs folder (`docs/` by default; `docs_dirs` in [configuration](../get-started/configuration.md) changes it), with each section's text written in Markdown. Every section becomes its own node in [the mesh](the-mesh.md), so you can:

- link a section to the code it describes,
- have that one section flagged when the code changes, and
- read or edit one section without loading the whole document.

Markdown files in the docs folders are indexed too, one node per `##` section, but they cannot hold links to code and the doc tools do not edit them.

## File format

A document is a `.docjson` file with a `title`, a `sections` array and optional `tags`:

```json
{
  "title": "Caching",
  "tags": ["guide"],
  "sections": [
    {
      "id": "overview",
      "heading": "Overview",
      "content": "Responses are cached for five minutes. Call `invalidate()` to clear the cache early.",
      "links": [{"node_id": "myproject::myproject.cache::invalidate"}]
    }
  ]
}
```

| Key | Required | Meaning |
|---|---|---|
| `title` | yes | Page title. |
| `sections` | yes | Array of sections; may be empty. |
| `tags` | no | Document tags, used for filtering and by the `[axiom_graph.staleness]` settings. |

Each section:

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | Lowercase-hyphen slug, unique among its siblings. Becomes part of the section's node id. |
| `heading` | yes | Section heading. |
| `content` | no | Markdown body. |
| `links` | no | `[{"node_id": "..."}]`: the code or doc nodes the section describes. |
| `sections` | no | Nested sections, up to three levels in all. |
| `tags` | no | Section tags. |
| `level` | no | Heading level, 2 to 6. Defaults to 2 for top-level sections and one more for each level of nesting. |

Sections saved by the doc tools also carry an `axiom_stamp` object, which records that a tool wrote them. Leave it as it is.

Files ending in `.json` are read as DocJSON too. The `docs_extensions` setting in [configuration](../get-started/configuration.md) sets which extensions are read; its first entry, `.docjson` by default, is the one new documents get.

If you are upgrading an index from axiom-graph 2.x, doc and section ids change form in 3.0. Follow the migration steps in the [changelog](https://github.com/ddpoe/axiom-graph/blob/master/CHANGELOG.md), which use the `axiom-graph doc-ids` commands, before your first build.

## Document and section ids

A document's id comes from its path, not from anything inside the file: the project id, `::`, then the docs folder and the file's path inside it, without the extension. A section adds `::` and its own id, and a nested section joins its parents' ids with dots.

| What | Node id |
|---|---|
| `docs/architecture.docjson` | `myproject::docs/architecture` |
| Section `overview` in it | `myproject::docs/architecture::overview` |
| Section `tables` nested under `database` | `myproject::docs/architecture::database.tables` |

Code nodes have ids of the form `{project}::{module}::{name}`; see [the mesh](the-mesh.md). You rarely need to build an id yourself: `axiom_graph_read_doc` prints each section's id next to its heading, and `axiom_graph_search` finds ids for code and docs.

Because the extension is not part of the id, `x.json` and `x.docjson` in one folder name the same document. The build warns about the pair and indexes `x.docjson`. To convert a docs tree from `.json` to `.docjson` with every id kept, run `axiom-graph doc-ids rename-extension <PROJECT_ROOT>` (a preview), then again with `--execute`.

## Nested sections

A section can hold its own `sections`. Use this to split a long topic into parts that can each be linked, flagged and read on their own, while keeping them in one document:

```json
{
  "id": "database",
  "heading": "Database",
  "content": "Overview of the storage layer.",
  "sections": [
    {"id": "tables", "heading": "Tables", "content": "..."},
    {"id": "migrations", "heading": "Migrations", "content": "..."}
  ]
}
```

Nesting goes three levels deep: a section, its children and their children. `axiom_graph_add_section` can add a child under a section at any of the first two levels. The doc tools refuse anything deeper, and the build skips a deeper level in a hand-edited file with a warning. If you need more depth, move the topic into its own document.

Each section's dot-path must be unique in its document. The doc tools refuse a write that would give two sections the same one, such as two siblings with the same id. If a hand edit creates such a pair, the build indexes the first, skips the second with a warning, and the tools cannot address either; fix the ids in the file, then accept the edit as described under editing files by hand below.

A parent section is flagged `LINKED_STALE` while any section nested under it is stale, so drift shows at every level above it. See [staleness](staleness.md).

## Linking a section to code

Each entry in a section's `links` array connects the section to one node, usually a function or method:

```json
"links": [{"node_id": "myproject::myproject.cache::invalidate"}]
```

`axiom_graph_read_doc` lists a section's links under it. When linked code changes, that section is flagged `LINKED_STALE`; its sibling sections are not. A link to a whole module flags the section when any function in the module changes, or when one is added or removed. Add links with `axiom_graph_add_link` (several at once with `node_ids`), or include them in the document you pass to `axiom_graph_write_doc`. Both warn when a target id is not in the index. The target goes under `node_id`, as above, or the entry can be the bare id string. Other keys such as `target` or `type` are not read, and every link is a `documents` link. A doc tool refuses any other entry and names the section and the entry. A build skips such an entry in a hand-edited file and names it in a warning.

Link the functions, methods and entry points the section describes: the code whose change would mean the prose needs another look. A quick test: if someone rewrote this function, would a reader need to re-check this section? Skip private helpers the section never mentions and modules cited only for orientation. Every link can flag the section, so extra links add noise.

## Linking to another doc section

A link can also point at another doc section. A common setup is for user guides to link to the developer doc that describes a feature, and for only the developer doc to link to code:

```
code  <--  dev-doc section  <--  user-guide section
```

When the code changes, the dev-doc section is flagged and the flag passes on to the guide sections that link to it. The guides never name a function, and the developer doc is the one place that tracks the code.

The flag only passes to documents whose tags are listed in `transitive_tags`, which is empty by default:

```toml
[axiom_graph.staleness]
transitive_tags = ["consumer"]
```

Only code changes travel along the chain; editing the dev-doc section's text does not flag the guide. [Staleness](staleness.md) covers how the flag spreads and clears, and [the docs-honesty loop](../examples/docs-honesty-loop.md) walks through the setup.

## Editing docs with the MCP tools

Agents edit DocJSON through the `axiom_graph_*` doc tools, one section at a time. Each tool writes the file and updates the index, so no build is needed afterwards.

| To | Use |
|---|---|
| Read a document or some of its sections | `axiom_graph_read_doc` (`outline=true` first, then `section_ids`) |
| Create a document, or replace one whole | `axiom_graph_write_doc` |
| Copy a document to a new one, changing some sections | `axiom_graph_clone_doc` |
| Replace a section's content, heading or id | `axiom_graph_update_section` |
| Append to, prepend to, or change part of a section | `axiom_graph_patch_section` |
| Add sections | `axiom_graph_add_section` |
| Delete a section and everything under it | `axiom_graph_delete_section` |
| Change a document's title or tags | `axiom_graph_update_doc_meta` |
| Delete a document | `axiom_graph_delete_doc` |
| Add or remove links | `axiom_graph_add_link` / `axiom_graph_delete_link` |
| Keep a hand edit as it stands | `axiom_graph_accept_doc_edits` |

A few things to know:

- `update_section`, `patch_section` and `delete_section` take the full section id that `read_doc` prints, such as `myproject::docs/architecture::database.tables`. `add_section` takes the doc id and a new slug, plus `parent_id` (the parent's dot-path) to nest it or `after` (a sibling's id) to place it.
- `patch_section` takes `anchor="$"` to append, `anchor="^"` to prepend, or `old_string` to replace text that appears exactly once. Its result shows the edited lines, so you can check the edit without reading the section again.
- In `write_doc`, a top-level `id` is the file's path under the docs folder (`"guides/caching"` writes `docs/guides/caching.docjson`), not a node id. Without one, the file name comes from the title. `docs_root` picks another configured docs folder.
- To start a document from an existing one, such as a template, use `axiom_graph_clone_doc`. `set_sections` replaces the content of the sections you name, `omit_sections` leaves sections out, and `title` and `tags` replace the source's; everything else, links included, is copied. It refuses a section id the source does not have, and it never overwrites an existing doc:

  ```
  axiom_graph_clone_doc(source_doc_id="myproject::docs/templates/feature", new_id="features/caching",
                        title="Caching", set_sections={"overview": "Responses are cached for five minutes."})
  ```

- To edit several sections in one call, pass `edits=[...]` to `update_section` or `patch_section`: a list of items, each with the keys a single call takes (including `addresses` and `expected_hash`), across any number of docs. Every item is checked first, so one bad item writes nothing. The reply gives each item's `content_hash`.
- `update_section` takes `expected_hash`, the `content_hash` from your last write result, and refuses the write if the section has changed since. `write_doc` takes `expected_hash` too: pass the `doc_hash` its reply (or `clone_doc`'s) ended with, and it refuses, writing nothing, if the file has changed since.
- A save writes the whole file or nothing, so a failed write leaves the previous file intact.
- Renaming a section with `new_id` also re-points the links in other docs that point at it. If one of those docs stays busy with another write, the rename fails with nothing written. A doc it still could not re-point, or a doc file it could not read, is named in a `WARNING:` line of the reply; point those links at the new id with `delete_link` and `add_link`.
- For long content, pass a file: `content_file` on the section tools, `doc_file` on `write_doc`.
- A write that would nest too deep or give two sections one dot-path returns an `ERROR:` message and writes nothing.

The full tool list is in [Connect your agent](../get-started/connect-your-agent.md).

## Saving a section verifies its text

When `write_doc`, `update_section`, `patch_section` or `add_section` saves a section whose content is new or changed, the section's own text is marked verified, with an `AGENT_VERIFIED` entry in its history.

A save does not review the code the section describes, so it never clears `LINKED_STALE`:

- Saving leaves every link status as it was: the section's, its parent's, and every other node's. If the section was `LINKED_STALE` because code it links to changed, it stays so, and the reply ends with `still LINKED_STALE via: ...` naming what is left.
- To clear it, pass `addresses=[node ids]` to `update_section` or `patch_section`, naming the changed nodes the edit reconciles: the ids in `check`'s `via` column or in `axiom_graph_drift_query`. The section clears once every node it was flagged through is named. If the text is already right, pass `addresses` to `update_section` with no new content. Naming a node the section is not flagged through is an error, and nothing is written.
- Or verify the section as it stands with `axiom-graph mark-clean <NODE_ID> <PROJECT_ROOT>` or `axiom_graph_mark_clean`.
- The flag also clears by itself if the code goes back to the version the section was verified against.
- Only the saved section's text is verified. Its parent, its siblings and the code it links to keep their status.
- Changing only the links, the heading or the id, or saving unchanged content, verifies nothing.
- A flag that reached the section through a link to another doc section stays until that section is verified.
- A section saved on another branch keeps this split when it merges: its text arrives verified, and each link arrives verified only where the code is still at the version the save checked it against (see [editing the files by hand](docjson.md#editing-the-files-by-hand)).

[Staleness](staleness.md) covers the other ways to clear drift.

## Editing the files by hand

You can edit a DocJSON file directly, but use the doc tools where you can: axiom-graph cannot tell a hand edit from an accidental one. The next build, or the next doc-tool write to the same document, reads the file and:

- updates the index to match it, including the links, so a link deleted from a `links` array is dropped from the index;
- reports each hand-edited section once as a raw DocJSON edit (`RAW_DOCJSON_EDIT` in its history) and leaves it unverified.

```
2 DocJSON section(s) were edited outside the doc tools (raw DocJSON edits) and are not verified.
```

To settle a flagged section, redo the change with a doc tool, which stamps and verifies it, or accept the file as it stands:

```bash
axiom-graph stamps accept <PROJECT_ROOT> --list   # show flagged sections
axiom-graph stamps accept <PROJECT_ROOT> --all
```

From an agent, `axiom_graph_accept_doc_edits` does the same (`dry_run=true` to list, `all_flagged=true` to accept them all). It accepts all the sections you list or none: if one can't be accepted (unknown id, unreadable file, a doc being written by someone else), it returns one error naming each and writes nothing. `mark-clean` does not clear the flag. To turn detection off, set `raw_docjson_edits = "off"` under `[axiom_graph.docjson]`.

Each section a doc tool saves carries an `axiom_stamp` in the file. Its `hash` is a fingerprint of the section's heading, content and links as the tool saved them, and it is not the `content_hash` a tool prints after a write: that one covers the section's text only and is what `expected_hash` checks. The stamp is how a build tells a tool's save from a hand edit, and it records the version of each linked code node the save was checked against.

The stamp travels with the file through merges and pulls, and a section an agent wrote on another branch arrives in two parts. Its text arrives verified, because the stamp shows a tool wrote exactly that text. Each link arrives verified only if the code it links to is still at the version the stamp records; a link to code that has changed since, or that the save left open, keeps the state your checkout already had for it. So a merged section can read verified for its text and still be `LINKED_STALE` for one link.

The same applies when a section a tool wrote is already flagged as changed because your index has not caught up with the merged file: if its stamp is still valid, the next build or `check` accepts it as a tool write and it reads verified. A section you edited by hand fails its stamp, so it stays flagged. A merged file nobody has built yet is picked up by the build after the merge, or by the second `check`.

With the PEV plugin installed, a hook warns when an agent edits a `.docjson` file directly; set `PEV_DOCJSON_GUARD=block` to deny those edits instead.

## Writing a document, start to finish

1. Find code that needs docs: `axiom_graph_list_undocumented` lists code that no section links to.
2. Write the document with `axiom_graph_write_doc`, with `links` on each section. It is indexed and verified as it is written. A file written by hand also works: run `axiom-graph build <PROJECT_ROOT>`, then `axiom-graph stamps accept <PROJECT_ROOT> --all`.
3. Read it back with `axiom_graph_read_doc` to check the text and the links under each section, and add any you missed with `axiom_graph_add_link`.
4. When code changes, `axiom-graph check <PROJECT_ROOT>` (or `axiom_graph_drift_query`) lists the sections that are `LINKED_STALE` (linked code changed) or `BROKEN_LINK` (linked node no longer exists).
5. Fix each flagged section with `update_section` or `patch_section`. The save verifies it.

[The docs-honesty loop](../examples/docs-honesty-loop.md) runs this cycle end to end and publishes the result as a site.
