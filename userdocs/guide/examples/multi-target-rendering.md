<!-- generated from axiom_graph::docs/consumer/examples/multi-target-rendering @ 336fac5e7511; do not edit -->

# Tutorial: Render docs to a README, plugin docs and a site

## What you'll build

axiom-graph can render the same DocJSON docs to several places. This tutorial sets up three, the same three this repository uses: a `README.md` for GitHub and PyPI, a folder of plugin docs, and a Sphinx user guide.

Each destination is a **render target**, declared in `axiom-graph.toml` under `[[axiom_graph.site.targets]]`. A target has a `name`, an `output` path, a `format` (`plain` or `sphinx`), and either a `doc` (one doc rendered to one file) or a `nav` (a nav file that lists a folder of docs). This repository declares:

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
doc       = "axiom_graph::docs/consumer/readme"
overwrite = true

[[axiom_graph.site.targets]]
name      = "plugin-pev"
output    = "pev_nexus_agents/pev/docs"
format    = "plain"
nav       = "docs/consumer/plugins/pev/nav.yml"
overwrite = true
```

With no targets declared, `render-site` renders a single target named `guide`: the docs listed in `site-nav.yml`, as Sphinx pages, into `userdocs/guide`.

To follow along you need an indexed project (`axiom-graph build .`) with the docs you publish in one folder, here `docs/consumer/`. The doc ids below start with `axiom_graph::`; use your own project id. Every target key is listed in [Configuration](../get-started/configuration.md).

## Recipe 1: Render a README from one doc

Write the README as an ordinary DocJSON doc, for example `docs/consumer/readme.docjson`. The doc's title becomes the `#` heading. To put badges or an intro paragraph directly under it, give the first section an empty heading: a section with an empty heading renders its content with no heading line.

Declare a `doc` target:

```toml
[[axiom_graph.site.targets]]
name      = "readme"
output    = "README.md"
format    = "plain"
doc       = "axiom_graph::docs/consumer/readme"
overwrite = true
```

`doc` names one doc id and `output` is the file to write. A `doc` target must use `format = "plain"`. `overwrite = true` lets the first render replace a README you wrote by hand (see [Overwriting and regeneration](#overwriting-and-regeneration)).

Render just this target:

```bash
axiom-graph render-site . --target readme
```

```
Rendering targets for /your/project ...
  [guide] skipped
  [readme] plain -> README.md : 1 page(s)
  [plugin-pev] skipped
```

The file starts with a provenance stamp, then the title and the lead section:

```markdown
<!-- generated from axiom_graph::docs/consumer/readme @ 03bdbb958b33; do not edit -->

# axiom-graph

[![PyPI version](https://img.shields.io/pypi/v/axiom-graph.svg)](https://pypi.org/project/axiom-graph/)
```

A link whose target is a doc id (anything containing `::`) is reduced to its text. Relative and external links are kept.

## Recipe 2: Render a folder of plugin docs

A plugin ships several pages, so it uses a `nav` target. Write a nav file for the folder, here `docs/consumer/plugins/pev/nav.yml`:

```yaml
site_name: pev
root: docs/consumer/plugins/pev

show:
  - readme
  - setup
  - user-guide
```

Declare the target. For a `nav` target, `output` is a folder:

```toml
[[axiom_graph.site.targets]]
name      = "plugin-pev"
output    = "pev_nexus_agents/pev/docs"
format    = "plain"
nav       = "docs/consumer/plugins/pev/nav.yml"
overwrite = true
```

Render it:

```bash
axiom-graph render-site . --target plugin-pev
```

```
Rendering targets for /your/project ...
  [guide] skipped
  [readme] skipped
  [plugin-pev] plain -> pev_nexus_agents/pev/docs : 3 page(s)
```

The output folder mirrors the source folder: `readme.md`, `setup.md` and `user-guide.md`, plus an `index.md` that lists them. In `plain` format the index is a list of links under the `site_name`, each labelled with the doc's title:

```markdown
# pev

- [pev](readme.md)
- [PEV Setup](setup.md)
- [PEV User Guide](user-guide.md)
```

## Writing the nav file

A nav file has three required keys:

- `site_name`: the title of the generated index page in `plain` format.
- `root`: the source folder, relative to the project root. Output paths and doc ids come from the file paths under it.
- `show`: the pages to publish, in order. A doc under `root` that is not listed is not rendered.

Each `show` entry is a page or a folder:

```yaml
show:
  - index              # page: docs/consumer/index.docjson
  - concepts:          # folder: docs/consumer/concepts/
      show:
        - the-mesh
        - staleness
  - viz
```

A string is always one page, even with a slash in it: `concepts/staleness` at the top level is one page, not a `concepts` group. A folder is a single-key mapping with its own `show` list, and folders can nest. A top-level `index` page becomes the output's `index.md`, with the navigation added below its text; without one, render-site generates `index.md`.

Each folder gets a landing page at `<folder>/index.md`, taken from the folder's own `index.docjson` or from the doc named by `landing: <name>` in the mapping, but not both. With neither, a `sphinx` target writes a page holding the folder name and a table of contents, and a `plain` target writes no page and lists the folder's pages under a heading on the parent page. render-site has no option for Sphinx `:caption:` groups; folders always nest.

Every listed page must be an indexed doc. If one is not, the target writes nothing and says why:

```
  [guide] sphinx -> userdocs/guide : 0 page(s)
    ! Nav validation: Unresolvable stem 'concepts/missing': no indexed doc with id axiom_graph::docs/consumer/concepts/missing
```

## Choosing plain or sphinx

| | `plain` | `sphinx` |
|---|---|---|
| Output | GitHub-flavored Markdown | MyST Markdown for Sphinx |
| Folder navigation | lists of links | `{toctree}` directives |
| Use for | READMEs, docs folders read on GitHub | a Sphinx or Read the Docs site |
| Target type | `doc` or `nav` | `nav` only |

`format` defaults to `plain`. A `doc` target cannot be `sphinx`; the config fails to load with:

```
site target 'readme' is format 'sphinx' but declares 'doc'; sphinx requires 'nav'
```

## Rendering some or all targets

`render-site` renders every target unless you name some with `--target`, which you can repeat:

```bash
axiom-graph render-site .
axiom-graph render-site . --target guide
axiom-graph render-site . --target readme --target plugin-pev
```

Targets you did not name are listed as `skipped`. The MCP tool `axiom_graph_render_site` takes the same names as `targets=["readme", "plugin-pev"]`.

`--build` (MCP: `build=true`) also runs `sphinx-build` for each `sphinx` target, on the output folder's parent: for the guide, `userdocs/` is built into `userdocs/_build/html`. It does nothing for `plain` targets. Sphinx must be installed; if it is not, the render prints a warning.

`--nav` and `--output` (MCP: `nav_path`, `output_dir`) ignore the target list and render one nav file as Sphinx pages. They default to `site-nav.yml` and `userdocs/guide`.

## Overwriting and regeneration

Each page rendered from a doc starts with a stamp, `<!-- generated from <doc id> @ <hash>; do not edit -->`. Before writing a file, render-site checks for it:

- A file with the stamp is overwritten.
- A file without it, such as a README you wrote by hand, is skipped unless the target sets `overwrite = true`.

A skipped file shows as a warning:

```
  [readme] plain -> README.md : 0 page(s)
    ! Refusing to overwrite un-stamped file (set overwrite): README.md
```

After the first render the file carries the stamp, so later renders replace it without `overwrite`. To change a generated page, edit its DocJSON doc and render again. render-site refuses an `output` path outside the project root.

Each `nav` target also writes a `.render-manifest.json` to its output folder, and `doc` targets are recorded in `.axiom_graph/render-manifest.json`. Both map each generated file to its source doc id and hash.

## Next steps

- [Configuration](../get-started/configuration.md): every `[[axiom_graph.site.targets]]` key and the rules a target must follow.
- [The docs-honesty loop](docs-honesty-loop.md): how a published page is flagged when the code it describes changes, and how to update and republish it.
