<!-- generated from axiom_graph::docs/consumer/examples/docs-honesty-loop @ 360e20d5c376; do not edit -->

# Tutorial: The docs-honesty loop

## What you'll build

A consumer doc is a page written for your users and published to a site. This tutorial adds one to the project from the [reporting-pipeline tutorial](reporting-pipeline.md), changes the code it describes, and takes the page through being flagged, updated, verified and published again. This user guide is maintained the same way.

Start with the project files from the first section of that tutorial, indexed with `axiom-graph build .`, and `axiom-graph check .` reporting all nodes `VERIFIED`. Run every command from the project root.

## How a consumer doc links to code

A consumer doc links to a section of a dev doc, such as a design doc, a spec or a PRD, and that section links to the code:

```
transform  <--documents--  design-doc section  <--documents--  consumer-doc section
```

When the code changes, the flag passes along both links, so the consumer page is flagged even though it names no function. When a function is renamed, only the dev doc's link has to change. [DocJSON](../concepts/docjson.md) covers links in general.

## Step 1: Write the consumer doc

Find the dev-doc section that describes the behavior your page covers. To see which sections document `transform`:

```
axiom-graph graph proj::reporting.pipeline::transform . --direction in
```

The output includes `<--[documents]-- proj::docs/design/reporting-pipeline::transform`. An agent can find it with `axiom_graph_search` and `scope="docs"`.

Create `docs/consumer/generating-reports.docjson`. Tag the doc `consumer`, and link its section to the design-doc section, not to the function:

```json
{
  "title": "Generating Reports",
  "tags": ["consumer"],
  "sections": [
    {
      "id": "filtering",
      "heading": "Which rows the report includes",
      "content": "The report includes only rows whose status is `active`.",
      "links": [{"node_id": "proj::docs/design/reporting-pipeline::transform"}]
    }
  ]
}
```

In `axiom-graph.toml`, let docs tagged `consumer` receive flags through the docs they link to:

```toml
[axiom_graph.staleness]
transitive_tags = ["consumer"]
```

Without this setting a code change flags the design doc but not the consumer doc. [Configuration](../get-started/configuration.md) lists the staleness settings.

Index the new doc:

```
axiom-graph build .
```

Because you wrote the file by hand, the build warns:

```
! 1 DocJSON section(s) were edited outside the doc tools (raw DocJSON edits) and are not verified. Fix: re-apply the change with update_section / patch_section / add_section, or accept the current text with axiom_graph_accept_doc_edits (CLI: axiom-graph stamps accept <ids>|--all).
```

Accept it:

```
axiom-graph stamps accept . --all
```

An agent creates the same file with `axiom_graph_write_doc`, which indexes it and needs no accept step.

## Step 2: Change the code

Change the filter in `transform` so trial users are included, then rebuild:

```python
    return df[df['status'].isin(['active', 'trial'])]
```

```
axiom-graph build .
```

The consumer doc has not been touched.

## Step 3: See the consumer doc flagged

```
axiom-graph check .
```

```
own: 2 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 7 LINKED_STALE / 0 BROKEN_LINK · 8 VERIFIED

NODE                                                        OWN_STATUS       LINK_STATUS
--------------------------------------------------------------------------------------------------
proj::reporting.pipeline                                    CONTENT_UPDATED  VERIFIED
proj::reporting.pipeline::transform                         CONTENT_UPDATED  VERIFIED
proj::tests.test_pipeline                                   VERIFIED         LINKED_STALE
proj::tests.test_pipeline::test_transform_filters_inactive  VERIFIED         LINKED_STALE  via proj::reporting.pipeline::transform
proj::tests.test_pipeline::test_reporting_pipeline_e2e      VERIFIED         LINKED_STALE  via proj::reporting.pipeline::transform
proj::docs/design/reporting-pipeline                        VERIFIED         LINKED_STALE
proj::docs/design/reporting-pipeline::transform             VERIFIED         LINKED_STALE  via proj::reporting.pipeline::transform
proj::docs/consumer/generating-reports                      VERIFIED         LINKED_STALE
proj::docs/consumer/generating-reports::filtering           VERIFIED         LINKED_STALE  via proj::docs/design/reporting-pipeline::transform
```

Follow the `via` column from the last row up: the consumer section is stale via the design-doc section, which is stale via `transform`. The tests are flagged as in the [reporting-pipeline tutorial](reporting-pipeline.md). [Staleness](../concepts/staleness.md) explains the statuses.

## Step 4: Update the docs and verify

Read the code change and the design-doc section, then decide whether the page is still true. It is not: it says only `active` rows are included.

1. Change the consumer section to "The report includes rows whose status is `active` or `trial`." An agent saves it with `axiom_graph_update_section`. If you edit the file by hand or in the Docs tab of the [dashboard](../viz.md), accept the edit:

   ```
   axiom-graph stamps accept . proj::docs/consumer/generating-reports::filtering
   ```

   The section is still `LINKED_STALE`. Its flag comes through the design-doc section, so verifying the consumer section does not clear it. The agent's save says so:

   ```
   still LINKED_STALE via: proj::docs/design/reporting-pipeline::transform (clears when it does)
   ```

2. Update the design-doc section the same way. An agent saves it with `axiom_graph_update_section` and names the code the section was flagged through, `addresses=["proj::reporting.pipeline::transform"]`. Saving the text alone, or accepting a hand edit, leaves the section `LINKED_STALE`. After a hand edit, or if the text is still true, verify it:

   ```
   axiom-graph mark-clean proj::docs/design/reporting-pipeline::transform . --reason "covers trial users"
   ```

   Clearing the design-doc section also clears every consumer section that links to it, so update those pages first.

`axiom-graph check .` no longer lists the doc rows. Clear the code and tests as in Step 5 of the [reporting-pipeline tutorial](reporting-pipeline.md).

If the dev-doc section a consumer page links to is deleted, `build` and `check` mark the consumer section `BROKEN_LINK`. Point its link at another section by editing `links`, or with `axiom_graph_delete_link` and `axiom_graph_add_link`.

## Step 5: Publish the site

`render-site` turns the docs listed in `site-nav.yml` into Markdown pages for Sphinx (MyST). Create `site-nav.yml` at the project root:

```yaml
site_name: Reporting
root: docs/consumer
show:
  - generating-reports
```

`root` is the folder that holds the published docs, and `show` lists the pages in order. A doc that is not listed is not published. [Multi-target rendering](multi-target-rendering.md) covers the full nav format and other outputs, such as a README.

```
axiom-graph render-site .
```

```
  [guide] sphinx -> userdocs/guide : 1 page(s)
```

`userdocs/guide/generating-reports.md` holds the corrected text, under a comment that names the source doc and commit:

```
<!-- generated from proj::docs/consumer/generating-reports @ 7110821ab167; do not edit -->

# Generating Reports

## Which rows the report includes

The report includes rows whose status is `active` or `trial`.
```

`render-site` also writes `userdocs/guide/index.md`, the table of contents. Commit the generated pages with your docs.
