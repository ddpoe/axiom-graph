<!-- generated from axiom_graph::docs.consumer.examples.multi-target-rendering @ f6b9b220927f; do not edit -->

# Tutorial: Rendering to README, plugin docs, and the guide

## What You'll Build

One DocJSON graph can publish to many destinations at once. The [docs-honesty loop](docs-honesty-loop.md) rendered a single Sphinx guide; this tutorial fans the *same* mesh out to several outputs — a GitHub `README.md`, a plugin's docs folder, and the Sphinx guide — each from the one set of nodes you already maintain.

The mechanism is **render targets**. The original behavior renders a single Sphinx guide (and still does when you declare no targets — a synthetic `guide` target is synthesized into `userdocs/guide`). The configurable-targets model lets you instead declare a list of named destinations under `[[axiom_graph.site.targets]]`, each with its own **output path** and **flavor**:

- **`plain`** — GitHub-friendly Markdown (GFM): bullet-list navigation, no Sphinx scaffolding. Right for a README, or a docs folder a host renders as raw Markdown.
- **`sphinx`** — MyST with `{toctree}` directives, for a Read the Docs / Sphinx HTML site.

The mental model in one line: **one graph → N declared targets × 2 flavors.** This repo dogfoods exactly that. Its `axiom-graph.toml` declares three:

```toml
[[axiom_graph.site.targets]]
name   = "guide"
output = "userdocs/guide"
format = "sphinx"
nav    = "site-nav.yml"

[[axiom_graph.site.targets]]
name      = "readme"
output    = "README.md"
format    = "plain"
doc       = "axiom_graph::docs.consumer.readme"
overwrite = true

[[axiom_graph.site.targets]]
name      = "plugin-pev"
output    = "pev_nexus_agents/pev/docs"
format    = "plain"
nav       = "docs/consumer/plugins/pev/nav.yml"
overwrite = true
```

Three destinations, one source mesh: the `README.md` on GitHub, the `pev_nexus_agents/pev/docs/**` folder, and this very guide all render from `docs/consumer/**` DocJSON. The two recipes below build the first two from scratch. (Substitute your own `project_id` for `axiom_graph::` in the doc-ids.)

If you have not published a single site yet, read [the docs-honesty loop](docs-honesty-loop.md) first — it covers `site-nav.yml`, the publish gate, and the provenance stamp this tutorial assumes.

## Recipe 1: Generate a README From One Doc

A `README.md` is one page, not a site — so it is a **`doc` target**: render a single DocJSON doc straight to a file, with no navigation, no `{toctree}`, and no landing pages.

Author the README as a consumer doc (`docs/consumer/readme.json`), then declare the target:

```toml
[[axiom_graph.site.targets]]
name      = "readme"
output    = "README.md"
format    = "plain"
doc       = "axiom_graph::docs.consumer.readme"
overwrite = true
```

Three fields make this a README rather than a guide page:

- **`doc` (not `nav`)** points at a single doc-id. A target declares *exactly one* of `doc` or `nav` — `doc` is the single-file path (through `render_doc_to_file`); `nav` is the subtree path (through `build_site`).
- **`format = "plain"`** is required here: a `doc` target is always plain. (A `sphinx` target must use a `nav`; declaring `doc` with `sphinx` is rejected at config load.)
- **`overwrite = true`** lets the first render replace your hand-authored `README.md`. After that the generated file carries a provenance stamp and regenerates freely — see [Path Safety and Regeneration](#path-safety-and-regeneration).

Build the index, then render just this target:

```bash
axiom-graph build .
axiom-graph render-site . --target readme
```

```
Rendering targets for /your/project ...
  [guide] skipped
  [readme] plain -> README.md : 1 page(s)
  [plugin-pev] skipped
```

Open the result. The top of this repo's generated `README.md`:

```markdown
<!-- generated from axiom_graph::docs.consumer.readme @ 0638aba261fb; do not edit -->

# axiom-graph

[![PyPI version](...)](...)
```

Two behaviors worth noting:

- **Provenance stamp.** Every generated file opens with `<!-- generated from <doc-id> @ <hash>; do not edit -->`. The hash is the content fingerprint, and the stamp is what marks the file safe to overwrite next time.
- **Title rendered once.** The doc title becomes the `# axiom-graph` H1, and the badges sit directly beneath it with no duplicate `## ...` heading. That works because the README's lead section has an *empty* heading: a headingless section renders its content directly under the title — which is how you place badges, a tagline, or an intro paragraph right below the H1.

Internal doc-id links (anything pointing at a `proj::docs...` node) are flattened to plain text so they never leak into the published file; ordinary relative and external links are left intact.

## Recipe 2: Ship a Per-Plugin Docs Subtree

A plugin ships a *folder* of docs, not one page — so it is a **`nav` target**: a subtree render through the same `build_site` pipeline as the guide, but in `plain` flavor.

Give the subtree its own slim nav (`docs/consumer/plugins/pev/nav.yml`):

```yaml
site_name: pev
site_description: Plan-Execute-Validate agent workflow for Claude Code
root: docs/consumer/plugins/pev

show:
  - readme
  - setup
  - user-guide
```

Then declare the target:

```toml
[[axiom_graph.site.targets]]
name      = "plugin-pev"
output    = "pev_nexus_agents/pev/docs"
format    = "plain"
nav       = "docs/consumer/plugins/pev/nav.yml"
overwrite = true
```

`nav` (a subtree) and `format = "plain"` (GitHub-flavored) are the two choices that distinguish this from the Sphinx `guide` target. Render it:

```bash
axiom-graph render-site . --target plugin-pev
```

```
Rendering targets for /your/project ...
  [guide] skipped
  [readme] skipped
  [plugin-pev] plain -> pev_nexus_agents/pev/docs : 3 page(s)
```

The output folder mirrors the source 1:1, with one twist for plain flavor: the folder's landing page (`pev_nexus_agents/pev/docs/index.md`) is a **bullet list of relative links** instead of a `{toctree}`:

```markdown
# pev

- [pev](readme.md)
- [PEV Setup](setup.md)
- [PEV User Guide](user-guide.md)
```

That is the headline difference between flavors: where `sphinx` emits `{toctree}` directives, `plain` emits Markdown link lists a raw-Markdown host renders correctly.

One more plain-mode behavior to know: **a contentless folder emits no stub.** If a section folder has no landing doc (no `index.json`, no `landing:`), `plain` mode writes no placeholder page for it — the parent expands it inline as a nested heading plus a link list of its children. (In `sphinx` mode the same folder would get a synthetic `# Folder` landing with a toctree.) So plain output never carries empty `# Folder` stub pages.

## Plain vs. Sphinx: Choosing a Flavor

The two flavors are not interchangeable, and the config rules enforce the sane combinations.

| | `plain` | `sphinx` |
|---|---|---|
| Output | GitHub-flavored Markdown | MyST + `{toctree}` |
| Navigation | bullet-list links | `{toctree}` directives |
| Use for | READMEs, plugin docs, any raw-Markdown host | Read the Docs / Sphinx HTML |
| Allowed with | `doc` or `nav` | `nav` only |

The hard constraint: **only a `nav` target may be `sphinx`, and a `doc` target is always `plain`.** A single file has no navigation tree, so `{toctree}` scaffolding would be meaningless on it — declaring `doc` with `format = "sphinx"` is rejected at config load. A subtree, by contrast, can be either: the `guide` target is `nav` + `sphinx` (Read the Docs), while `plugin-pev` is `nav` + `plain` (a Markdown folder).

`sphinx` output is byte-identical to the original single-site build, so adopting targets does not change your existing guide — it just lets you add more destinations beside it.

## Rendering a Subset

With several targets declared, you rarely want to regenerate all of them every time. `render-site` renders **every** configured target by default; narrow it with `--target NAME`, which is **repeatable**:

```bash
# all targets
axiom-graph render-site .

# just the guide
axiom-graph render-site . --target guide

# the README and the plugin docs, not the guide
axiom-graph render-site . --target readme --target plugin-pev
```

That last command renders two targets and reports the third as `skipped` — un-targeted targets are skipped, not silently dropped:

```
Rendering targets for /your/project ...
  [guide] skipped
  [readme] plain -> README.md : 1 page(s)
  [plugin-pev] plain -> pev_nexus_agents/pev/docs : 3 page(s)
```

The same subset control exists on the other two surfaces:

- **API** — `render_targets(project_root, only=["readme"])`
- **MCP** — the `axiom_graph_render_site` tool takes `targets=["readme"]`

To also compile the HTML, add `--build`, which runs `sphinx-build` after generating the pages — but **only for `sphinx`-flavored targets**. Building a `plain` target does nothing extra (there is no Sphinx project to compile), so `--build` on a README or plugin-docs render is a no-op.

Subset renders are safe to interleave: a `doc` target's entry in the central manifest is updated in place, and the entries for targets you *didn't* render this run are preserved, not erased.

## Path Safety and Regeneration

Render targets write real files into your repo — including files you may have hand-authored, like `README.md`. Two guards keep that safe.

**Stamp-presence overwrite guard.** Before writing, the renderer checks whether the existing file carries a provenance stamp:

- **Stamped** (a previous render wrote it) → overwritten freely. Regeneration is always clean.
- **Un-stamped** (you wrote it by hand) → *not* touched unless the target sets `overwrite = true`. Without it, the render warns and skips, so a stray `output` path can never clobber your work silently.

This is why `readme` and `plugin-pev` set `overwrite = true`: the *first* render replaces the hand-authored file, stamps it, and from then on every render sees the stamp and regenerates cleanly. The `overwrite` flag is really only load-bearing on that first run.

**Never writes outside the project root.** Every `output` is resolved to an absolute path and checked to live under the project root; a path that escapes (`../../etc/...`) is rejected before anything is written.

Put together, regeneration is idempotent and contained: re-running `render-site` reproduces every stamped target byte-for-byte, touches nothing outside the repo, and refuses to overwrite an un-stamped file you did not opt to replace.

## Where to Go Next

- [Configuration](../get-started/configuration.md) — the full `[[axiom_graph.site.targets]]` key reference: every field, the validation rules, and implicit-target synthesis.
- [The docs-honesty loop](docs-honesty-loop.md) — the single-site publish step in context: `site-nav.yml`, the publish gate, and how a rendered page rides the staleness mesh.
- [The reporting pipeline](reporting-pipeline.md) — the mesh from the other side: an agent reading it to keep code, tests, and docs in sync.

The through-line: the same DocJSON mesh that keeps your docs honest is the mesh you publish *from* — to as many destinations, in as many flavors, as your project needs.
