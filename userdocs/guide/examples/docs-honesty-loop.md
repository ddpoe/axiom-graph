<!-- generated from axiom_graph::docs.consumer.examples.docs-honesty-loop @ f10d512d9293; do not edit -->

# Tutorial: The Docs-Honesty Loop

## What You'll Build

Published documentation rots the moment the code it describes changes. The usual fix is discipline: remember to update the docs, hope a reviewer catches the ones you forgot. axiom-graph replaces that hope with a signal.

This tutorial walks the full loop end to end, the same loop that produced the site you are reading right now:

1. **Author** a consumer doc in DocJSON, linked through a dev-doc proxy to the capability it describes.
2. **Change the code** behind that capability.
3. **Watch staleness flag the consumer doc** `LINKED_STALE` automatically, even though it never linked to the code directly.
4. **Update** the doc through the viz Doc Manager or the MCP doc tools, then re-verify.
5. **Republish** the corrected public site with `render-site`.

The payoff is a documentation set that tells you when it has drifted instead of silently lying — because linking docs to code and detecting that those docs went stale are the [two reads](../concepts/the-mesh.md#two-reads-one-mesh) against one mesh.

This is the companion to the [reporting-pipeline tutorial](reporting-pipeline.md). That one shows an agent consuming the mesh to do work; this one shows the mesh keeping its own documentation honest.

## The Proxy-Linking Architecture

Before the steps, the one idea that makes the whole loop work: **consumer docs link through a dev-doc proxy, not at raw code.**

It is tempting to point a user guide straight at the function it describes. Don't. Function and symbol names change constantly; a consumer page wired to `compute_staleness` breaks the instant someone renames it, and the page itself rarely talks about that symbol by name anyway. Consumer docs describe capabilities, not symbols.

So the provenance chain has three layers, connected by `documents` edges in the [mesh](../concepts/the-mesh.md):

```
Code node  <--documents--  Dev-doc section  <--documents--  Consumer-doc section
 (the symbol)               (PRD / design / ADR)            (this guide)
```

The **dev-doc layer** (a feature PRD, an interface spec, a design section, or an ADR) binds tightly to the code: it links to the actual functions and classes. The **consumer layer** binds only to that dev-doc section. The consumer page therefore *rides the chain*: it inherits a staleness signal whenever the code changes, but it is insulated from the code's churn. Rename the function and the dev-doc's link updates; the consumer page never notices, because it was never pointing at the symbol.

This is the difference between linking *at* code and linking *through* a stable description of code. Pick the dev-doc section that documents the capability, link to that, and your page stays stable under refactors while still hearing about real changes. The same architecture is described from the engine's side in [staleness](../concepts/staleness.md) and from the authoring side in [DocJSON](../concepts/docjson.md).

## Step 1: Author the Consumer Doc

Suppose you want to publish a user-facing page about how staleness detection works. The capability is real code, but your page describes the concept, not the implementation.

First, find the dev-doc section that already documents that capability and binds to the code. That dev-doc section is your proxy target. You can discover it with search:

```bash
axiom-graph list /path/to/project --tag design
```

or from an agent, `axiom_graph_search(project_root, "staleness design")`.

Now author the consumer doc as DocJSON. Each section is its own node, so you document at section granularity, not whole-file granularity. The `links` array on the section points at the **dev-doc proxy section**, never the raw function:

```json
{
  "id": "how-it-works",
  "heading": "How Staleness Works",
  "content": "axiom-graph compares a code hash...",
  "links": [
    { "node_id": "axiom_graph::docs.features.staleness.design::architecture" }
  ]
}
```

Tag the document `consumer`, the tag that opts it into transitive propagation (Step 3 explains why that matters). Then index it:

```bash
axiom-graph build /path/to/project
```

Agents do the same thing through MCP: `axiom_graph_write_doc` to create the file and register it in one step, or `axiom_graph_add_link` to attach the proxy link afterward. `axiom_graph_read_doc` then shows the linked dev-doc summary under your section, confirming the chain is wired.

The granularity here is deliberate. A section is the unit a reader loads and the unit that goes stale, so an agent can read exactly this section instead of the whole page, and the staleness engine can flag exactly this section instead of the whole document.

Indexing makes the page real in the mesh, but it does not yet publish it to the static site. Listing it in `site-nav.yml` does that, as Step 5 shows.

## Step 2: Change the Code

Now play the part of the developer who refactors the underlying capability. Someone edits the staleness computation, the function your *dev-doc* section links to, and rebuilds the index:

```bash
axiom-graph build /path/to/project
```

`build` is discovery-only: it inserts new nodes and notices changed ones, but it does not overwrite existing staleness signals. It simply records that the code node's content hash no longer matches the hash captured when the dev-doc was last verified.

Nothing about your consumer page changed. You did not touch its file. Yet, as the next step shows, it is about to be flagged, because the mesh knows your page is two `documents` hops downstream of the code that just moved. That is the whole point: drift is detected structurally, by following edges, not by anyone remembering to look.

## Step 3: Transitive Staleness Flags the Doc

Run a check:

```bash
axiom-graph check /path/to/project
```

The code change ripples outward through the chain. The dev-doc section goes `LINKED_STALE` because the code it links to changed. And because your consumer page links to that dev-doc section, *it* goes `LINKED_STALE` too, transitively:

```
own: 1 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 2 LINKED_STALE / 0 BROKEN_LINK · 47 VERIFIED

NODE                                                  OWN_STATUS       LINK_STATUS
axiom_graph::...index.staleness::compute_staleness    CONTENT_UPDATED  VERIFIED
docs.features.staleness.design::architecture          VERIFIED         LINKED_STALE  via ...::compute_staleness
docs.consumer.staleness::how-it-works                 VERIFIED         LINKED_STALE  via docs.features.staleness.design::architecture
```

Read the `via` breadcrumbs bottom to top: the consumer page is stale *via* the design spec, which is stale *via* the function that changed. You can trace the entire path back to the root cause without grepping.

### Why the consumer page hears about it

Direct doc-to-code staleness is a single hop. Transitive propagation is what carries the signal across the doc-to-doc `documents` edge to the consumer layer, and it is **opt-in and tag-gated**. axiom-graph only propagates through documents that carry a tag listed in `transitive_tags`:

```toml
[axiom_graph.staleness]
transitive_tags = ["consumer"]
```

Because your page is tagged `consumer`, it participates. Developer specs that link to other specs do not pick up transitive signals unless their own tag is listed, so the noise stays where it belongs. The propagation loop walks doc-to-doc edges until the stale set stabilizes (usually one or two passes), with a visited-set guard so cycles can't spin. See [staleness](../concepts/staleness.md) for the full status model and [configuration](../get-started/configuration.md) for the tag knobs, including `frozen_tags` for historical docs like ADRs that should *not* chase every edit.

This is the loop's keystone: published, user-facing prose stays honest even though it never touches code, because staleness is a read on the same mesh the docs live in.

## Step 4: Update and Re-Verify

A `LINKED_STALE` flag is an invitation to review, not a verdict that the prose is wrong. Sometimes the code change does not affect what your page says; sometimes it does. Follow the breadcrumb to find out.

**Review the chain.** Read the dev-doc section that the page links to (and, through it, the code that moved) to see whether your user-facing description is still accurate.

**Edit if needed.** Two paths, same mesh:

- *Viz Doc Manager.* Open the [viz dashboard](../viz.md), go to the Docs tab, and edit the section in the rich-text editor. Edits save straight back to the DocJSON file on disk, and the link picker lets you re-point the proxy link if the dev-doc section itself was renamed or restructured.
- *MCP doc tools.* An [agent connected over MCP](../get-started/connect-your-agent.md) calls `axiom_graph_update_section` to patch the content, and `axiom_graph_add_link` / `axiom_graph_delete_link` to fix proxy links.

**Re-verify, and mind the sticky rule.** `LINKED_STALE` is *sticky*. It does not clear because you edited the prose, ran another check, or because someone upstream marked the code clean. The only thing that clears it is a fresh verification snapshot on the doc section itself:

```bash
axiom-graph mark-clean docs.consumer.staleness::how-it-works /path/to/project \
  --reason "Reviewed after staleness refactor; user-facing behavior unchanged"
```

Note a sharp edge: the CLI `mark-clean` clears own-status drift (CONTENT_UPDATED / DESC_UPDATED), but to clear `LINKED_STALE` on a doc section, save it through `axiom_graph_update_section`, which auto-records a verification snapshot, or call `mark_clean` over MCP with the doc section's node ID. Either way the snapshot captures the current hashes, so the next check promotes the section back to `VERIFIED`, and if the code drifts again later, the snapshot invalidates and the flag returns. That hash-anchored snapshot is the audit trail: a durable record of who reviewed which section against which version of the code.

While you are in here, also watch for `BROKEN_LINK`. Every `build` and `check` runs a consistency pass that flags any edge pointing at a node ID that no longer exists, for instance, if the dev-doc proxy section was deleted out from under you. Repair it with `axiom_graph_delete_link` plus a new link to the correct target. Unlike a rename (which the proxy architecture absorbs silently), a deletion is a structural break that always wants human eyes.

## Step 5: Publish to the Site

With the section reviewed and back to `VERIFIED`, publish the corrected page to the static site.

### The publish gate: `site-nav.yml`

`render-site` does not publish every DocJSON file under `docs/consumer/`. It publishes exactly the docs listed in **`site-nav.yml`**, a small manifest at the project root. Presence in `show:` is the explicit publish gate; list order is the display / toctree order. The folder tree under `root:` *is* the site tree -- `docs/consumer/**` maps one-to-one to `userdocs/guide/**`.

```yaml
site_name: axiom-graph
site_description: Local-first code intelligence for AI agents
root: docs/consumer            # source publish boundary

show:                          # presence = published; order = display order
  - index                      # a bare string is a leaf page
  - concepts:                  # a single-key mapping is a section folder
      show:                    # ...with its own ordered list of children
        - the-mesh
        - staleness
  - get-started:
      show:
        - connect-your-agent
        - configuration
  - viz
```

**To publish a new page:** drop its DocJSON at `docs/consumer/<folder>/<stem>.json`, then add `<stem>` to the matching folder's `show:` list (or the top-level `show:` for a root page). A doc that exists on disk but is absent from `show:` is simply not published -- that is the gate. A section folder can name its landing page with `landing: <stem>` or carry its own `<folder>/index.json`; if it does neither, `render-site` synthesizes a landing page with a table of contents for free.

### Render

```bash
axiom-graph render-site /path/to/project
```

`render-site` walks the nav, renders each listed DocJSON section to clean Markdown (stripping the internal node-id links), prepends a provenance stamp, and writes the nested pages into `userdocs/guide/` for a static site generator (Sphinx/MyST) to build into HTML. Because the output tree mirrors the source tree one-to-one, the relative links between pages resolve without any per-page configuration. Commit the generated `userdocs/guide/**` alongside your DocJSON. The nav file location and render defaults live under `[axiom_graph.site]` -- see [configuration](../get-started/configuration.md).

The corrected page is now live, and the loop is closed: code changed, the mesh flagged the downstream consumer doc, you reviewed and re-verified it, and the republished site reflects reality.

## This Site Is Built This Way

None of this is hypothetical. The documentation you are reading is itself a set of DocJSON nodes in the axiom-graph mesh. Each consumer page links through dev-doc proxies, ADRs, PRDs, and design sections, that bind to the code. Because `consumer` is a transitive tag, these pages inherit `LINKED_STALE` whenever the underlying code drifts. That flag is the maintainer's cue to review; `mark-clean` re-verifies; `render-site` republishes the public mirror.

The full set of consumer guides, the recursive nav manifest, the `render-site` pipeline, and transitive `LINKED_STALE` are all shipping capabilities, not aspirations.

The takeaway is the loop itself. Documentation in axiom-graph is not a static artifact you periodically remember to update; it is a node in a typed mesh that tells you when it has drifted. The same edges that let an agent pull exactly the context it needs are the edges that carry a staleness signal from a changed function all the way out to the published page describing it. Honesty is not a process you impose on the docs; it is a query the docs answer about themselves.
