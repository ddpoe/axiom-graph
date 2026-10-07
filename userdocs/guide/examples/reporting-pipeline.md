<!-- generated from axiom_graph::docs/consumer/examples/reporting-pipeline @ 179038708077; do not edit -->

# Tutorial: The reporting pipeline, end to end

## What you'll build

This tutorial runs axiom-graph on a small Python project: a three-function module, its tests and a design doc. You index the project, look up a function's tests and docs, change the code, see which tests and docs get flagged, and verify them again. The last step renames a function.

Install axiom-graph first ([Use the CLI](../get-started/use-the-cli.md)). Steps 2 and 6 use an MCP client such as Claude Code ([Connect your agent](../get-started/connect-your-agent.md)).

Create these files in a new directory:

```
axiom-graph.toml
reporting/__init__.py        (empty)
reporting/pipeline.py
tests/__init__.py            (empty)
tests/test_pipeline.py
docs/design/reporting-pipeline.docjson
```

`axiom-graph.toml` sets the project id, which starts every node id:

```toml
[axiom_graph]
project_id = "proj"
```

`reporting/pipeline.py`:

```python
import pandas as pd


def load_data(path: str) -> pd.DataFrame:
    """Read the raw CSV from disk."""
    return pd.read_csv(path)


def transform(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize columns and filter rows where status == 'active'."""
    df = df.rename(columns=str.lower)
    return df[df['status'] == 'active']


def export_report(df: pd.DataFrame, dest: str) -> None:
    """Write the filtered DataFrame to JSON."""
    df.to_json(dest, orient='records')
```

`tests/test_pipeline.py` has a test for each function and one end-to-end test:

```python
import pandas as pd

from reporting.pipeline import load_data, transform, export_report


def test_load_data_reads_csv(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("Status,Name\nactive,a\n")
    assert len(load_data(str(p))) == 1


def test_transform_filters_inactive():
    df = pd.DataFrame({"Status": ["active", "inactive"]})
    assert list(transform(df)["status"]) == ["active"]


def test_export_report_writes_json(tmp_path):
    dest = tmp_path / "out.json"
    export_report(pd.DataFrame({"a": [1]}), str(dest))
    assert dest.exists()


def test_reporting_pipeline_e2e(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("Status,Name\nactive,a\ninactive,b\n")
    df = transform(load_data(str(p)))
    export_report(df, str(tmp_path / "out.json"))
    assert (tmp_path / "out.json").exists()
```

`docs/design/reporting-pipeline.docjson` is a design doc with a section for each function. Each section's `links` entry ties it to the function it describes ([DocJSON](../concepts/docjson.md) explains the format):

```json
{
  "title": "Reporting Pipeline Design",
  "tags": ["design"],
  "sections": [
    {"id": "load-data", "heading": "load_data",
     "content": "Reads the raw CSV from disk.",
     "links": [{"node_id": "proj::reporting.pipeline::load_data"}]},
    {"id": "transform", "heading": "transform",
     "content": "Lowercases column names and keeps rows where status is 'active'.",
     "links": [{"node_id": "proj::reporting.pipeline::transform"}]},
    {"id": "export-report", "heading": "export_report",
     "content": "Writes the filtered rows to JSON.",
     "links": [{"node_id": "proj::reporting.pipeline::export_report"}]}
  ]
}
```

Commit the files to a git repository; rename detection in Step 7 reads the earlier version of a function from git. Run every command from the project root.

## Step 1: Build the index

Index the project:

```
axiom-graph build .
```

The build ends with a status summary:

```
  staleness     : own: 0 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 0 LINKED_STALE / 0 BROKEN_LINK · 15 VERIFIED
```

The index has a node for each module, function and test, one for the design doc, and one for each of its sections. Two kinds of edge connect them:

- `validates`, from a test to each function it calls. The build finds these by reading the test bodies.
- `documents`, from a design-doc section to the function named in its `links`.

[The mesh](../concepts/the-mesh.md) explains nodes and edges; [the ontology](../concepts/ontology.md) lists every type.

`axiom-graph check .` reports each node's drift status. Nothing has changed yet:

```
own: 0 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 0 LINKED_STALE / 0 BROKEN_LINK · 15 VERIFIED
(all nodes VERIFIED)
```

## Step 2: Look up a function, its tests and its docs

An agent connected over MCP can read one function or one doc section instead of whole files.

`axiom_graph_source` with `node_id="proj::reporting.pipeline::transform"` returns the function's source:

```
# proj::reporting.pipeline::transform  @ reporting/pipeline.py#L9-L12

def transform(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize columns and filter rows where status == 'active'."""
    df = df.rename(columns=str.lower)
    return df[df['status'] == 'active']
```

`axiom_graph_read_doc` with `doc_id="proj::docs/design/reporting-pipeline"` and `section="transform"` returns that one section, followed by the code it links to.

`axiom_graph_graph` with `direction="in"` lists what depends on a node. The CLI has the same command:

```
axiom-graph graph proj::reporting.pipeline::transform . --direction in
```

```
[atomic_process] proj::reporting.pipeline::transform  @ reporting/pipeline.py#L9-L12
  incoming:
  <--[composes]-- proj::reporting.pipeline
  <--[documents]-- proj::docs/design/reporting-pipeline::transform
  <--[validates]-- proj::tests.test_pipeline::test_transform_filters_inactive
  <--[validates]-- proj::tests.test_pipeline::test_reporting_pipeline_e2e
```

The `composes` edge comes from the module that contains the function. The doc section and the two tests are what a change to `transform` can flag.

## Step 3: Edit a docstring

Change only the docstring of `transform`:

```python
    """Lowercase the column names and keep rows whose status is 'active'."""
```

Rebuild and check:

```
axiom-graph build .
axiom-graph check .
```

```
own: 0 CONTENT_UPDATED / 2 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 0 LINKED_STALE / 0 BROKEN_LINK · 13 VERIFIED

NODE                                 OWN_STATUS       LINK_STATUS
---------------------------------------------------------------------------
proj::reporting.pipeline             DESC_UPDATED     VERIFIED
proj::reporting.pipeline::transform  DESC_UPDATED     VERIFIED
```

`DESC_UPDATED` means the docstring changed but the code did not, so the tests and the doc section are not flagged. The module row repeats its function's status. Accept the change:

```
axiom-graph mark-clean proj::reporting.pipeline::transform . --reason "docstring only"
```

`check` reports all nodes `VERIFIED` again. [Staleness](../concepts/staleness.md) describes every status.

## Step 4: Change what the code does

Trial users should appear in the report. Change the filter in `transform`:

```python
    return df[df['status'].isin(['active', 'trial'])]
```

Rebuild and check:

```
axiom-graph build .
axiom-graph check .
```

```
own: 2 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 5 LINKED_STALE / 0 BROKEN_LINK · 8 VERIFIED

NODE                                                        OWN_STATUS       LINK_STATUS
--------------------------------------------------------------------------------------------------
proj::reporting.pipeline                                    CONTENT_UPDATED  VERIFIED
proj::reporting.pipeline::transform                         CONTENT_UPDATED  VERIFIED
proj::tests.test_pipeline                                   VERIFIED         LINKED_STALE
proj::tests.test_pipeline::test_transform_filters_inactive  VERIFIED         LINKED_STALE  via proj::reporting.pipeline::transform
proj::tests.test_pipeline::test_reporting_pipeline_e2e      VERIFIED         LINKED_STALE  via proj::reporting.pipeline::transform
proj::docs/design/reporting-pipeline                        VERIFIED         LINKED_STALE
proj::docs/design/reporting-pipeline::transform             VERIFIED         LINKED_STALE  via proj::reporting.pipeline::transform
```

`transform` is `CONTENT_UPDATED`: its code changed. The two tests and the doc section from Step 2 are `LINKED_STALE`: something they depend on changed, and `via` names it. The test module and the design doc repeat their children's status. The other tests and doc sections are not flagged.

## Step 5: Update and verify

Each flagged node clears when you verify it. Editing a test or a doc does not clear its `LINKED_STALE` flag, and neither does verifying the code it depends on.

1. Accept the code change:

   ```
   axiom-graph mark-clean proj::reporting.pipeline::transform . --reason "trial users belong in the report"
   ```

   This clears `transform` only.

2. Update the unit test to expect trial rows:

   ```python
   def test_transform_filters_inactive():
       df = pd.DataFrame({"Status": ["active", "trial", "inactive"]})
       assert list(transform(df)["status"]) == ["active", "trial"]
   ```

   Run the tests. When they pass, mark both flagged tests clean. The end-to-end test needs no edit.

   ```
   axiom-graph mark-clean proj::tests.test_pipeline::test_transform_filters_inactive .
   axiom-graph mark-clean proj::tests.test_pipeline::test_reporting_pipeline_e2e .
   ```

3. Change the `transform` section of the design doc to "Lowercases column names and keeps rows where status is 'active' or 'trial'." An agent saves it with `axiom_graph_update_section` and names the code the section was flagged through, `addresses=["proj::reporting.pipeline::transform"]`, which clears the section. Saving the text alone leaves it `LINKED_STALE`.

   If you edit the file by hand, accept the edit, then verify the section:

   ```
   axiom-graph stamps accept . proj::docs/design/reporting-pipeline::transform
   axiom-graph mark-clean proj::docs/design/reporting-pipeline::transform . --reason "covers trial users"
   ```

`axiom-graph check .` reports all nodes `VERIFIED`.

## Step 6: Let an agent make the updates

With an MCP client connected, make the Step 4 change and ask the agent to update the tests and docs that went stale. It can:

1. list the flagged nodes and their causes with `axiom_graph_check` or `axiom_graph_drift_query`
2. see what changed with `axiom_graph_diff`, and read the code and the doc section with `axiom_graph_source` and `axiom_graph_read_doc`
3. fix the doc section with `axiom_graph_update_section`, naming the changed code in `addresses` so the section clears, and edit the test
4. run the tests, then verify the tests and the code with `axiom_graph_mark_clean`

You review its changes like any other change. To keep stale docs from merging, run `axiom-graph check --fail-on stale .` in CI or a pre-push hook; it exits 1 while anything is flagged. The [PEV plugin](../pev/overview.md) builds a full agent workflow on these tools.

## Step 7: Rename a function

Rename `transform` to `normalize` in `reporting/pipeline.py`, and update the import and the two calls in `tests/test_pipeline.py`. Rebuild and check.

The build looks for a function that disappeared and a new one whose body is similar, and records the pair as a rename. It compares `normalize` with `transform` as it was in your first commit. Here too little matches: since that commit `transform` has a new docstring, a new filter and now a new name. So `check` lists `transform` as `NOT_FOUND`, and `normalize` is a new function with no history.

Record the rename yourself:

```
axiom-graph rename apply proj::reporting.pipeline::transform proj::reporting.pipeline::normalize .
```

```
Applied rename: proj::reporting.pipeline::transform -> proj::reporting.pipeline::normalize (new node marked RENAMED).
```

`normalize` takes over the old id's history and edges, and the design doc's link is rewritten to point at it. `axiom-graph check .` now shows:

```
own: 4 CONTENT_UPDATED / 0 DESC_UPDATED / 1 RENAMED / 1 NOT_FOUND · link: 5 LINKED_STALE / 0 BROKEN_LINK · 9 VERIFIED

NODE                                                        OWN_STATUS       LINK_STATUS
--------------------------------------------------------------------------------------------------
proj::reporting.pipeline::transform                         NOT_FOUND        VERIFIED
proj::tests.test_pipeline                                   CONTENT_UPDATED  LINKED_STALE
proj::tests.test_pipeline::test_transform_filters_inactive  CONTENT_UPDATED  LINKED_STALE  via proj::reporting.pipeline::normalize
proj::tests.test_pipeline::test_reporting_pipeline_e2e      CONTENT_UPDATED  LINKED_STALE  via proj::reporting.pipeline::normalize
proj::docs/design/reporting-pipeline                        CONTENT_UPDATED  LINKED_STALE
proj::docs/design/reporting-pipeline::transform             VERIFIED         LINKED_STALE  via proj::reporting.pipeline::normalize
proj::reporting.pipeline::normalize                         RENAMED          VERIFIED

reporting/pipeline.py has 1 indexed function no longer found — run build
```

No link is broken. `normalize` is `RENAMED`. The tests are `CONTENT_UPDATED` because you edited their calls, and the design doc because its link was rewritten. The tests and the doc section are also `LINKED_STALE` via `normalize`, the code they point at now. The old id stays as a `NOT_FOUND` record so the rename can be undone; the last line counts that record.

When most of a renamed function still matches its committed version, the build pairs the two itself and `check` shows `RENAMED` without this step. `axiom-graph rename revert proj::reporting.pipeline::normalize .` undoes a rename either way. Agents use `axiom_graph_apply_rename` and `axiom_graph_revert_rename`.

To finish, rebuild, accept the rewritten link (the build reports it as an edit made outside the doc tools), verify the renamed function, the tests and the doc section, and remove the old record:

```
axiom-graph build .
axiom-graph stamps accept . --all
axiom-graph mark-clean proj::reporting.pipeline::normalize .
axiom-graph mark-clean proj::tests.test_pipeline::test_transform_filters_inactive .
axiom-graph mark-clean proj::tests.test_pipeline::test_reporting_pipeline_e2e .
axiom-graph mark-clean proj::docs/design/reporting-pipeline::transform .
axiom-graph purge proj::reporting.pipeline::transform .
```

`axiom-graph check .` reports all nodes `VERIFIED`.

## Where to go next

- [The mesh](../concepts/the-mesh.md): nodes, edges and node ids
- [Staleness](../concepts/staleness.md): every status and how to clear it
- [The docs-honesty loop](docs-honesty-loop.md): add a user-facing doc to this project and publish it
- [Use the CLI](../get-started/use-the-cli.md) and [Connect your agent](../get-started/connect-your-agent.md): run these steps on your own project
