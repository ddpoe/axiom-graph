<!-- generated from axiom_graph::docs.consumer.concepts.docjson @ 35c65fb44be5; do not edit -->

# DocJSON: structured docs where a section is its own node

## A section is its own node

axiom-graph stores documentation as structured JSON ("DocJSON"), one `.json` file per document in your project's `docs/` directory. The content inside a section is still plain markdown. The JSON is just the envelope that gives axiom-graph the structure it needs to do the one thing plain markdown can't: **treat each section as its own node in the graph.**

That single design choice is what this whole page is about. When a section is a node, axiom-graph can:

- **Link that section** (not the whole document) to the exact code it describes.
- **Detect drift** at section granularity (when that code changes, *that* section goes stale, not the whole file).
- **Return that section alone** when an agent or human asks for it.

This is the core of axiom-graph's value: shrinking what anyone has to load. An agent reads one section, not the whole document, the same way reading source returns a function by line range instead of the whole file. A doc section is a first-class node in [the mesh](the-mesh.md) (see also [the ontology](ontology.md)), so the [two reads](the-mesh.md#two-reads-one-mesh) — retrieval and drift — apply to it like any other node.

This page covers the shape of a DocJSON file, how sections nest, how a section binds to code, and how that binding keeps consumer docs honest through a proxy chain.

## The shape of a document

A DocJSON file has three top-level keys:

| Key | Required | Description |
|---|---|---|
| `title` | yes | Human-readable title, rendered as the page heading |
| `sections` | yes | Array of section objects (may be empty) |
| `tags` | no | String array for filtering (e.g. `"consumer"`, `"guide"`) |

A minimal document:

```json
{
  "title": "Data Model",
  "tags": ["database", "schema"],
  "sections": [
    {
      "id": "nodes-table",
      "heading": "Nodes Table",
      "content": "Every indexed entity is a row in the `nodes` table. Each node has a unique `id`, a `node_type`, and summary text fields."
    },
    {
      "id": "edges-table",
      "heading": "Edges Table",
      "content": "Relationships are stored in the `edges` table. Each edge has a `from_id`, `to_id`, and `edge_type`."
    }
  ]
}
```

Each section object:

| Key | Required | Description |
|---|---|---|
| `id` | yes | Lowercase-hyphen slug (e.g. `"nodes-table"`). Becomes part of the section's node ID. |
| `heading` | yes | Display heading for the section |
| `content` | no | Markdown body. Stored in the index as the node's `level_2` field. |
| `level` | no | Heading depth 2–6. Defaults to `depth + 2` (top-level `##`, child `###`, grandchild `####`). |
| `tags` | no | Section-level tags (e.g. `["deprecated"]`), independent of document tags. |
| `links` | no | Array of `{"node_id": "..."}` objects connecting the section to code or doc nodes. |
| `sections` | no | Nested child sections (up to 3 levels deep). |

Note what is **not** a key: the document's identity. A document's node ID comes from its **file path** relative to `docs/`, not from any field in the JSON. `docs/concepts/docjson.json` becomes the node ID `myproject::docs.concepts.docjson`. You never set node IDs by hand.

## How node IDs work

Every node in the graph has a unique ID derived from its location. Understanding the format helps when you add links or query the graph.

**Code nodes** follow `{project}::{dotpath}::{name}`:

| What | Node ID |
|---|---|
| Module `axiom_graph/db/nodes.py` | `axiom_graph::axiom_graph.db.nodes` |
| Function `upsert_node` in it | `axiom_graph::axiom_graph.db.nodes::upsert_node` |

**Doc nodes** use the file path relative to `docs/`, with separators replaced by dots:

| What | Node ID |
|---|---|
| Doc file `docs/architecture.json` | `axiom_graph::docs.architecture` |
| Section `overview` in it | `axiom_graph::docs.architecture::overview` |
| Nested child section | `axiom_graph::docs.architecture::overview.subsection` |

The part after `::` is the section path. For a top-level section it's just the slug; for nested sections it's a dot-path (covered next). To discover existing node IDs, use `axiom_graph_search` or `axiom_graph_list` — never invent them.

## Nested sections (depth up to 3)

A section can recursively contain sub-sections via an optional `sections` key. This lets you break a large topic into focused, individually addressable pieces *without* fragmenting it into a separate document — keeping related content together while still giving each piece its own node.

```json
{
  "id": "database-layer",
  "heading": "Database Layer",
  "content": "Overview of the DB design.",
  "sections": [
    {"id": "tables", "heading": "Tables", "content": "Core table definitions..."},
    {"id": "migrations", "heading": "Migrations", "content": "How migrations work..."}
  ]
}
```

**Dot-path node IDs.** Nested sections use dot-separated paths after the `::` separator, mirroring how axiom-graph names hierarchical code nodes (`module.class.method`):

| What | Node ID | Depth |
|---|---|---|
| Top-level section | `axiom_graph::docs.architecture::database-layer` | 0 |
| Child section | `axiom_graph::docs.architecture::database-layer.tables` | 1 |
| Grandchild section | `axiom_graph::docs.architecture::database-layer.tables.nodes-table` | 2 |

**Depth limit: 3 levels** (depth 0, 1, 2). The scanner warns and ignores a `sections` key on a depth-2 node. If you need to go deeper, that's the signal to split into a separate document.

**Containment becomes graph edges.** Nesting emits parent-to-child `composes` edges, so the hierarchy is queryable like any other relationship — depth is just traversal distance. Heading level auto-maps to depth (`##` / `###` / `####`), and an explicit `level` still overrides.

**Backward compatible.** A section with no `sections` key is a leaf, exactly like the original flat format. Existing documents work with zero changes — nested sections are a strict superset.

**Staleness flows down the tree.** A parent section is marked `LINKED_STALE` when any child is stale, so you can trace drift from a document down to the exact sub-section that needs attention. See [staleness](staleness.md).

## Linking a section to code

The point of a section being a node is that it can be wired to the exact code it describes. Each entry in a section's `links` array creates a `documents` edge from the section node to a code node:

```json
{
  "id": "staleness-engine",
  "heading": "Staleness Engine",
  "content": "The staleness engine compares code hashes to detect when docs are out of date.",
  "links": [
    {"node_id": "axiom_graph::axiom_graph.index.staleness::compute_staleness"}
  ]
}
```

That one edge type, `documents`, does double duty — it is what `axiom_graph_read_doc` follows to show linked code summaries beneath a section, and it is what the staleness engine follows to flag the section when that code changes. One mesh, two reads. (Edge types are defined in [the ontology](ontology.md).)

The payoff of section-granular linking is **precision in both directions**:

- **Precise context.** An agent that needs to understand staleness reads the one section linked to `compute_staleness`, not the entire architecture document.
- **Precise staleness.** When `compute_staleness` changes, *that* section is flagged — not the whole file, not its siblings. You know exactly which prose to re-check.

**What to link.** Link public functions, classes, decorators, and entry points the section explicitly describes — anything whose contract a code change could invalidate. The quick test: imagine someone rewrites the linked function; would a reader need to re-check this section? If yes, link it. If no, skip it. Do **not** link private helpers the section never mentions, or whole modules cited only for orientation — every link is a staleness trigger, and over-linking creates noise.

## Proxy linking: consumer docs link to docs, not raw code

Consumer-facing pages like this one describe capabilities, not individual functions. So instead of linking straight to code, they link to a **dev-doc section** that documents the capability — and that dev-doc section is what binds to the code. This forms a chain:

```
code function  <--documents--  dev-doc section  <--documents--  consumer-doc section
```

The same `documents` edge is used at every hop. Because axiom-graph propagates staleness transitively along these edges, the consumer page inherits drift without ever naming a function:

```
compute_staleness            (changed)
  ↑ documents
staleness design::architecture   LINKED_STALE  via ...::compute_staleness
  ↑ documents
concepts/staleness::how-it-works LINKED_STALE  via staleness design::architecture
```

Two things fall out of this:

- **Stability under renames.** When a symbol is renamed or moved, the dev-doc layer absorbs the change. The consumer page rides the chain and keeps pointing at a stable doc target instead of a node ID that just moved.
- **Honesty without coupling.** Consumer prose stays correct even though it never references code directly — the dev-doc proxy is the binding surface.

This is exactly how the page you're reading is wired. The full mechanism — transitive propagation, the dev-doc proxy, and the publish loop — is the subject of [the docs-honesty loop](../examples/docs-honesty-loop.md#the-proxy-linking-architecture).

## Editing sections through MCP

Because each section is a node, you patch one section at a time rather than rewriting a file. The MCP server is the primary surface for this — agents (and humans using an MCP client) manage docs without hand-editing JSON.

| Tool | What it does |
|---|---|
| `axiom_graph_read_doc` | Render a document (or one `section`) as markdown, with node IDs annotated in comments. |
| `axiom_graph_write_doc` | Create a new DocJSON file and index it in one step. |
| `axiom_graph_update_section` | Whole-replace one section's content, heading, or ID. Only that section is touched. |
| `axiom_graph_patch_section` | Edit part of a section's content without re-sending the whole body: append (`anchor="$"`), prepend (`anchor="^"`), or `Edit`-style unique-match replace (`old_string`). |
| `axiom_graph_add_section` | Append a section to an existing doc — optionally nested under a `parent_id` or positioned `after` a sibling — without rewriting the file. |
| `axiom_graph_delete_section` | Remove a section and everything nested under it. |
| `axiom_graph_add_link` / `axiom_graph_delete_link` | Add or remove `documents` edges from a section to code nodes (batch-capable). |

A few behaviors worth knowing:

- **Dot-path targeting.** Pass a dot-path section ID (`database-layer.tables.indexes`) to `update_section` and the nested target resolves correctly. The fully qualified form (`axiom_graph::docs.foo::database-layer.tables.indexes`) works anywhere a section ID is accepted.
- **Renames cascade.** Renaming a section in-place via `new_id` re-paths every child whose ID was prefixed by the old slug.
- **Auto-slug.** If you omit a section's `id` in `write_doc`, axiom-graph derives a slug from the heading (`"Database Layer"` → `database-layer`). Set an explicit `id` only when you want a particular slug for cross-references.
- **Partial edits over whole-replace.** `update_section` always replaces the whole section body; `patch_section` appends, prepends, or spot-replaces. The append/prepend modes skip the read-modify-write round trip — handy (and clobber-safe) for accreting sections like changelogs and ledgers. The `^`/`$` anchors are out-of-band parameters, so a body full of `$VAR`, `$x^2$`, or `Ctrl-^` is never mis-parsed.

The human path (the CLI and viz) is secondary; for hand-authoring the raw format, see the [configuration](../get-started/configuration.md) and [CLI](../get-started/use-the-cli.md) guides.

## Saving a section verifies it

Saving a section through the MCP write tools (`update_section`, `add_section`, `write_doc`, the link tools) does more than rewrite JSON — it records a **verification snapshot** for any existing section node whose content or heading actually changed. axiom-graph compares each section's stored hashes before and after the write; for every existing node whose `code_hash` (prose body) or `desc_hash` (heading) differs, it emits an `AGENT_VERIFIED` history row at the new hash.

The practical effect: **the writer is the verifier.** If the section you just edited was `LINKED_STALE`, that flag clears as a side effect of the save, because the new snapshot is now newer than the linked code's last change. You don't run a separate "mark clean" step for your own edit.

The scope is deliberately narrow:

- Only **existing** nodes that actually changed are candidates. Brand-new sections (created by `write_doc`) are default-clean via the normal first-index path — no spurious verification.
- Only the **saved section** clears. Parents, siblings, and the linked code nodes keep their own status; they get their own snapshots only when verified separately. (This preserves the sticky-LINKED_STALE invariant for everything you didn't touch — see [staleness](staleness.md).)

This is the mechanism behind the docs loop: edit the stale section, the save clears it, re-render the site. Clearing is a deliberate verification act, not an accidental side effect of any file write.

## Build reconciles edges to the JSON

The `links` array in the JSON is the source of truth for a section's `documents` edges. But JSON gets edited outside the MCP tools too — a raw editor, a bulk find-and-replace, a merge. To keep the graph from drifting away from the files, **`axiom_graph_build` reconciles `documents` edges against the JSON `links` on every build.**

For each section walked during a build, axiom-graph enforces that the DB's set of `documents` edges for that section equals the section's JSON `links` set exactly — including the empty set. Orphan edges left behind by external edits are deleted (recorded in history as `LINK_REMOVED`), and missing edges are added. The reconciler runs after stale-node purging and before broken-link detection, so a build leaves the mesh matching what's actually on disk.

The takeaway for authors: **the JSON is canonical.** Hand-edit `links` if you like; the next build makes the graph agree. You don't have to manually clean up edges after editing files directly.

## Authoring a document, start to finish

Putting it together, the loop for writing and maintaining a DocJSON document:

1. **Create** a `.json` file in `docs/` (lowercase-hyphen names; subdirectories are fine). At minimum supply `title` and `sections`. Or create it in one step with `axiom_graph_write_doc`.
2. **Build** with `axiom_graph_build` so axiom-graph indexes the new sections as nodes.
3. **Read back** with `axiom_graph_read_doc` to confirm the rendered output and that linked node summaries appear under each section.
4. **Find gaps** with `axiom_graph_list_undocumented` — code nodes with no inbound `documents` edge from any section.
5. **Link** sections to the code (or dev-doc) they describe, via `links` in the JSON or `axiom_graph_add_link`.
6. **Check** with `axiom_graph_check` to see which sections are `LINKED_STALE` (linked node changed) or `BROKEN_LINK` (linked node gone), then update the affected sections — which, by the writer-is-verifier rule above, clears them.

That last step is the steady state: code drifts, `check` surfaces exactly the sections that drifted with it, you fix the prose, the save clears the flag. The mesh stays trustworthy because staleness is the read that keeps it that way. To see the full publish-and-republish cycle for a site built on these docs, see [the docs-honesty loop](../examples/docs-honesty-loop.md).
