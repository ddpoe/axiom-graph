<!-- generated from axiom_graph::docs/consumer/concepts/staleness @ 395af2934dee; do not edit -->

# Staleness

## What staleness tracks

axiom-graph flags code and docs that changed since someone last checked them, along with the docs, tests and workflows linked to them that may now be out of date. It follows the same links you navigate in [the mesh](the-mesh.md): a doc section that `documents` a function is flagged when the function changes.

`axiom-graph build` and `axiom-graph check` update the flags, and the MCP tools bring the flags they show up to date before they answer. A flag tells you what to review. Once you have reviewed a node and fixed anything wrong, you verify it: axiom-graph records who checked it and clears the flag.

## Statuses

Each node has two statuses. Its **own status** says whether the node itself changed. Its **link status** says whether something it links to changed. The two are independent: a doc section can be `CONTENT_UPDATED` because you edited it and `LINKED_STALE` because the function it describes changed.

Own status:

| Status | Meaning |
|---|---|
| `VERIFIED` | Matches the version last verified. |
| `DESC_UPDATED` | Only the description changed, such as a function's docstring. |
| `CONTENT_UPDATED` | The content changed: a function's body, or a doc section's text. |
| `RENAMED` | The node was renamed or moved, and its history and links moved with it. |
| `NOT_FOUND` | The node is no longer in its file, or the file is gone. |

Link status:

| Status | Meaning |
|---|---|
| `VERIFIED` | Nothing it links to changed since it was last verified. |
| `LINKED_STALE` | Something it documents, tests or wraps changed after it was last verified. |
| `BROKEN_LINK` | It links to a node ID that is not in the index. |

A module or a doc shows the worst status among its parts, so one stale section marks its whole doc. From least to most severe, own statuses run `VERIFIED`, `DESC_UPDATED`, `CONTENT_UPDATED`, `RENAMED`, `NOT_FOUND`, and link statuses run `VERIFIED`, `LINKED_STALE`, `BROKEN_LINK`.

## What flags a node

`build` and `check` compare each node with the version last verified. The first time a node is indexed, that version is its baseline. A function's body and its docstring are compared separately.

| Change | What is flagged |
|---|---|
| A function's body changes | The function is `CONTENT_UPDATED`. Doc sections linked to it, tests that exercise it, and the `@workflow` or `@task` on it become `LINKED_STALE`. |
| Only a function's docstring changes | The function is `DESC_UPDATED`. The `@workflow` or `@task` on it becomes `LINKED_STALE`. Linked doc sections and tests are not flagged. |
| The body of a task called through an `AutoStep` changes | The workflow that calls it becomes `LINKED_STALE`, through any chain of AutoSteps. |
| A doc section's text changes | The section is `CONTENT_UPDATED`. |
| A node disappears | The node is `NOT_FOUND`. |
| A link points at a node that is not in the index | The section, test or workflow holding the link is `BROKEN_LINK`. |

**What a section is compared with.** Verifying a doc section or test records the version of each function it links to. The section is flagged while any of those functions is at another version. If a function goes back to the version the section was checked against, the flag clears on the next `check`. So an edit you revert leaves nothing to review, and a section you verified against the new code is flagged again if the code is reverted.

The doc tools verify the text they write, so a section's own status reads `CONTENT_UPDATED` only when its text was changed outside them.

`check` also prints a line for each file whose functions and the index disagree:

```
config.py has 1 new function — run build
```

A new function joins the index on the next `build`. A function that is gone from its file stays `NOT_FOUND` until you record its rename or purge it (see [Renames and deleted code](staleness.md#renames-and-deleted-code)).

The workflow markers are described in [annotations](annotations.md).

## How a change reaches a doc

When a doc section links to code (see [DocJSON](docjson.md)), a change to the code's body makes the section `LINKED_STALE`. `check` names the cause in its `via` column, with `(+N more)` when there are several.

User-facing docs usually link to a section of a developer doc rather than to code. To pass the flag along those doc-to-doc links, list the tags of the linking docs in `transitive_tags` (see [configuration](../get-started/configuration.md)):

```toml
[axiom_graph.staleness]
transitive_tags = ["consumer"]
```

A section in a doc tagged `consumer` that links to another doc section then becomes `LINKED_STALE` whenever that section is, through any number of hops. Without a listed tag, links between docs carry no flag. Only `LINKED_STALE` travels this way: editing a section's text does not flag the sections that link to it.

After a change to `parse_config`, `check` shows the whole chain:

```
$ axiom-graph check .
own: 2 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 4 LINKED_STALE / 0 BROKEN_LINK · 140 VERIFIED

NODE                                  OWN_STATUS       LINK_STATUS
----------------------------------------------------------------------------
myproject::config                     CONTENT_UPDATED  VERIFIED
myproject::config::parse_config       CONTENT_UPDATED  VERIFIED
myproject::docs/architecture          VERIFIED         LINKED_STALE
myproject::docs/architecture::config  VERIFIED         LINKED_STALE  via myproject::config::parse_config
myproject::docs/guide                 VERIFIED         LINKED_STALE
myproject::docs/guide::configuration  VERIFIED         LINKED_STALE  via myproject::docs/architecture::config
```

`docs/guide::configuration` is stale because `docs/architecture::config` is stale, which is stale because `parse_config` changed. The [docs-honesty loop](../examples/docs-honesty-loop.md) tutorial sets up this kind of chain for a published site.

## Finding and fixing drift

`axiom-graph check .` prints a summary line and one row per flagged node, as in the example above. `--all` lists `VERIFIED` nodes too, and `--format json` prints JSON. `build` prints the same summary line when it finishes, and the [dashboard](../viz.md) shows the same statuses.

Documents tagged as frozen (see [configuration](../get-started/configuration.md)) are left out of these counts, the document itself included, except for a broken link in one. A frozen section is never flagged `LINKED_STALE` because its child sections changed.

`check` recomputes only what changed since the last check. `check --full` re-hashes every file and recomputes every node. It gives the same statuses, more slowly.

Over MCP, `axiom_graph_check` returns the summary line and the lines about new or missing functions; `full=true` matches `--full`. `axiom_graph_drift_query` lists the flagged nodes with their causes. It can filter by status (`filter="LINKED_STALE"`) or path (`location_glob="src/billing/**"`), group by `status`, `location_prefix`, `feature` or `node_kind` (`code`, `test` and `doc` in one call; `test` is everything under your configured `scan.test_paths`), return bare IDs (`format="ids"`) to pass to `axiom_graph_mark_clean` or `axiom_graph_reverify`, and page through long lists with `page` and `limit`.

The MCP read tools (`axiom_graph_search`, `axiom_graph_source`, `axiom_graph_graph`, `axiom_graph_read_doc`, `axiom_graph_drift_query`) and the dashboard refresh the statuses they show before they answer, and tag each node they name that is not `VERIFIED`, for example `[CONTENT_UPDATED]`. `refresh_before_read` in `[axiom_graph.staleness]` (see [configuration](../get-started/configuration.md)) sets how:

| Value | Before a read answers |
|---|---|
| `"changed-files"` | Refreshes what the read shows from the files that changed. The default. |
| `"check"` | Runs `check` first. |
| `"off"` | Refreshes nothing, and adds `index is behind for N files — run check` when files have changed. |

| Status | What to do |
|---|---|
| `CONTENT_UPDATED`, `DESC_UPDATED` | Review the change, then run `axiom-graph mark-clean <node-id> . --reason "..."`. |
| `RENAMED` | Check that the match is right, then `mark-clean` the node. If it is wrong, revert it (see [Renames and deleted code](staleness.md#renames-and-deleted-code)). |
| `NOT_FOUND` | If the node moved, record the rename. If it was deleted, purge it. |
| `LINKED_STALE` | Read the `via` node's change, update the section if it is wrong, then verify the section (see [Clearing LINKED_STALE](staleness.md#clearing-linked_stale)). |
| `BROKEN_LINK` | Remove the link or point it at the right node, with `axiom_graph_delete_link` and `axiom_graph_add_link`. |

## Clearing LINKED_STALE

A `LINKED_STALE` that comes from code stays on a section until someone verifies the section against that code, or the code goes back to the version the section was checked against. Editing the file by hand, saving new text with a write tool, running `check` again, or marking the changed code clean does not clear it. These do:

- **`addresses`.** `axiom_graph_update_section` and `axiom_graph_patch_section` take `addresses=[node ids]`: the changed nodes the section was flagged through, which `axiom_graph_drift_query` shows as `via`. Each named node is recorded as checked at its current version, and the section clears once every node it was flagged through is named, in one edit or across several. Pass it with the edit, or on its own (`update_section` with only `addresses`) when the text is already right. In a batch (`edits=[...]`) each item takes its own `addresses`. While other causes remain, the reply ends with `still LINKED_STALE via: ...`. Naming a node that is not a current cause is an error, and nothing is written:

  ```
  ERROR: addresses must name current offenders of myproject::docs/architecture::config; not offenders: myproject::config::load_defaults. Current offenders: myproject::config::parse_config. Nothing was written.
  ```

- `axiom-graph mark-clean <section-id> . --reason "..."`, or `axiom_graph_mark_clean` over MCP (pass `node_ids` to verify several at once). This clears the section whatever flagged it, for when you have checked everything.
- `axiom_graph_reverify` on the code that changed, once you have checked that what depends on it is still right. It verifies that node and clears the `LINKED_STALE` it caused in one step, including on sections flagged through other docs. A section that another changed node also flags stays flagged, and the report lists it. A section that has its own unreviewed change is cleared of the `LINKED_STALE` only; its own `CONTENT_UPDATED` stays until you review it, and the report lists it as an own change kept. The report opens with how many nodes it cleared, then the `LINKED_STALE` count before and after, counted the way `check` counts. It stays short: it shows the first 20 skipped sections, 3 causes for each, and the first 10 ids it could not find. Pass `verbose=true` to see every source, every cleared node and every own change kept, and everything uncapped.

Saving text with a write tool (`write_doc`, `update_section`, `patch_section`, `add_section`, `accept_doc_edits`, or `stamps accept`) marks the section's own text as reviewed, so its own status is `VERIFIED`. It never clears a `LINKED_STALE`: not the section's, not its parent's, and not any other node's.

A later edit to the code flags the section again, as described in [What flags a node](staleness.md#what-flags-a-node).

A section flagged through another doc clears when the section it links to is verified, or when you reverify the code at the root of the chain. Verifying the linking section alone does not clear it while the section it links to is still flagged. Naming that section in `addresses` is accepted but changes nothing; the reply ends with `still LINKED_STALE via: <section> (clears when it does)`. Review a flagged consumer page before you verify the developer section it links to, because verifying that section clears the consumer page too.

Marking a doc, or a section with child sections, clean has no effect when its `LINKED_STALE` comes from its children. `mark-clean` says so and lists the sections to verify.

## Renames and deleted code

When you rename or move a function, `build` looks for a new node that matches the missing one. If it finds a confident match, the new node takes over the old node's history and links and is marked `RENAMED`, so doc sections linked to the old name follow it. The build summary counts these under `nodes renamed`. Review each one, then `mark-clean` it.

When the build misses a rename, the old node shows `NOT_FOUND`. Record the rename yourself, or undo a wrong one:

```bash
axiom-graph rename apply myproject::billing::total myproject::invoices::total .
axiom-graph rename revert myproject::invoices::total .
```

`rename apply` needs the old node to be `NOT_FOUND` and the new one to be newly indexed. The MCP tools are `axiom_graph_apply_rename` and `axiom_graph_revert_rename`. Applying a rename carries the verification of the docs that link the old node over to the new one: a doc stays verified only if the renamed code still matches the version it was verified against, and otherwise goes `LINKED_STALE` for a fresh review. A doc that could not be re-pointed because someone was writing it is named in the output as still linking the old id, and a doc file that could not be read is named separately. Rename detection covers code; doc sections are renamed through the doc tools (see [DocJSON](docjson.md)).

When code is gone for good, remove its node with `axiom-graph purge <node-id> .`, or all of them with `axiom-graph purge --all-not-found .` (`axiom_graph_purge_node` over MCP). Record any missed rename first: a purged node cannot be renamed. Doc sections that linked to a purged node become `BROKEN_LINK`. Until you purge it, sections linked to a `NOT_FOUND` node are not flagged. A deleted file needs no purge: the next build removes its nodes, and sections that linked to them become `BROKEN_LINK`.

## Frozen docs

Some docs record a decision at a point in time, such as design records and plans, and should not be flagged when the code moves on. List their doc tags in `frozen_tags`:

```toml
[axiom_graph.staleness]
frozen_tags = ["adr", "plan"]
```

Sections in a doc with a frozen tag get no new `LINKED_STALE`, from code, through other docs, or because a child section changed. A `LINKED_STALE` a section already had stays as long as code it links to has changed since you last verified the section; verify the section to clear it. Freezing the doc does not clear it, but once nothing causes it any more it clears on its own. `BROKEN_LINK` still shows. `check` and `drift_query` leave frozen sections, and the frozen doc itself, out of their results, except for `BROKEN_LINK`; pass `include_frozen=true` to `axiom_graph_check` or `axiom_graph_drift_query` to include them.

## Human and agent verification

Each verification records who made it and the reason given. `axiom-graph mark-clean` and `axiom-graph stamps accept` record a human verification. The MCP tools (`axiom_graph_mark_clean`, `axiom_graph_reverify`, `axiom_graph_accept_doc_edits` and the doc write tools) record an agent verification. A verification copied by `carry-forward` keeps the verifier it had. To see what agents verified before you push:

```bash
axiom-graph history agent-verified .
```

This lists every node whose latest history entry is an agent verification. `axiom-graph report .` summarises what changed since a checkpoint and who verified it; see [history](history.md).

In CI, `axiom-graph check . --fail-on stale` fails the job while anything is flagged:

| `--fail-on` | Exits 1 when |
|---|---|
| `none` | Never (the default). |
| `stale` | Any node has an own or link status other than `VERIFIED`. |
| `unverified` | Any node has an own status other than `VERIFIED`. Link status is ignored. |
| `any` | Same as `stale`. |

## After merging a worktree

If you work in a git worktree with its own index (`axiom-graph checkout <worktree>` copies one in), the nodes you verify there are verified only in the worktree's index. After you merge the branch, run `build` in the main checkout, then copy the verifications over:

```bash
axiom-graph carry-forward ../my-worktree --dry-run
axiom-graph carry-forward ../my-worktree
```

```
Carried 1 verification(s) in full and 1 in part from feat @ ae1de6054c70.
1 of the 1 carried in part now read VERIFIED; the rest stay stale through what did not carry (a link the worktree did not verify at this version) or until nodes they depend on settle.
Stale here: 4 before -> 2 after.
Not carried (2): 2 NOT_FOUND, RENAMED or BROKEN_LINK here.
own: 0 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 2 NOT_FOUND · link: 0 LINKED_STALE / 0 BROKEN_LINK · 5 VERIFIED
```

A stale node takes the worktree's whole verification when the worktree verified exactly what main now holds: the node is `VERIFIED` there, its content is the same, and every node it links to is at the version it was verified against.

A node the worktree verified at the content main holds, but which still has a link flagged for something else, carries in part. It takes its own text verification, and the worktree's check of each link whose target is at the checked version here. Every other link keeps the state main gave it, so a link nobody checked is never marked verified by the merge. The report counts full and partial carries separately and says how many partial ones now read `VERIFIED`. For each partial carry it lists the links left open and why: no receipt in the worktree, absent in the worktree, or the linked node differs.

Every other stale node keeps its status, and the report counts why: not verified in the worktree, content differs, a link is absent in the worktree, a linked node differs, or it is `NOT_FOUND`, `RENAMED` or `BROKEN_LINK` here. A carried section that links to a doc section still stale in main stays flagged until that section settles.

`--dry-run` writes nothing and lists the nodes it would carry, in full and in part; `--list` lists every stale node by verdict, on a dry run or a real one. Run it from the main checkout or pass `-p <checkout>`. Two indexes with different project ids or schema versions are refused. Over MCP the tool is `axiom_graph_carry_forward` (`dry_run`, `list_nodes`). Each carried verification keeps the verifier of the worktree's latest verification of that node, and its history entry names the branch and commit it came from (see [history](history.md)).
