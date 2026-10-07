---
name: axiom-annotations-markers
description: Reference for axiom-annotations decorator and step marker syntax. Use when adding or modifying @workflow, @task, Step, or AutoStep markers in Python code.
disable-model-invocation: false
---

# Axiom Annotation Marker Syntax Reference

Self-contained reference for the axiom-annotations `@workflow`/`@task` decorators and
`Step`/`AutoStep` markers. Use it whenever you add, modify, or review these markers in
Python code — don't write markers from memory.

This skill is the **canonical reference** for axiom-annotations marker syntax — edit it directly when the rules change.

## Core Rule

**Minor step numbers (e.g. `6.1`, `3.2`) can ONLY appear inside loops — regardless of
whether they are on a `Step` or `AutoStep`.** This is the single overarching constraint.
Major step numbers (integers) can appear anywhere. Everything else follows from this.

These rules are enforced by axiom-annotations validation — violations raise a validation error.

## Decorators

### @workflow
Marks an orchestration function that coordinates multiple steps and may delegate to `@task` functions.

```python
@workflow(
    purpose="Short description of what this workflow does",
    inputs="description of inputs",
    outputs="description of outputs",
)
def build(project_root: Path) -> dict:
```

- Should contain step markers to document the workflow's phases (but not strictly required).
- Can use any mix of `Step` and `AutoStep` markers.

### @task
Marks a leaf function that performs a discrete unit of work.

```python
@task(
    purpose="Short description of what this task does",
    inputs="description of inputs",
    outputs="description of outputs",
)
def delete_doc_by_id(conn, doc_id) -> None:
```

- Does not require step markers (but can have them for documenting phases).
- Can contain `Step` or `AutoStep` markers, including minor steps inside loops.

## Step Markers

Both `Step` and `AutoStep` use the same numbering rules:

- **Major step number** (integer: 1, 2, 3, …) — can appear anywhere in the function body.
- **Minor step number** (float: 3.1, 7.2, …) — **ONLY inside `for` loops.** The integer part
  is the parent step number; the decimal part is the iteration sub-step.

### Step(step_num=N)
Marks a sequential phase. Typically used for phases that contain inline logic.

```python
口 = Step(step_num=1, name="Resolve config",
         purpose="Load axiom-graph.toml and determine project_id")
```

- `step_num`: **required** — integer or float (minor only inside loops).
- `name`: **required** — short label for the step.
- `purpose`: **required** — what this step accomplishes.
- `inputs`: (optional) what this step reads.
- `outputs`: (optional) what this step produces.
- `critical`: (optional) important constraints or invariants.

### AutoStep(step_num=N)

**`AutoStep` takes `step_num` and `name` only — nothing else.** It does *not* share `Step`'s
fields, and passing `purpose=`, `inputs=`, `outputs=` or `critical=` raises `TypeError` at
import time, which takes down every module that imports the file.

```python
口 = AutoStep(step_num=10, name="Purge stale entries")
nodes_purged = _purge_stale_entries(db_path, project_root, warnings)
```

- `step_num`: **required** — integer or float (minor only inside loops).
- `name`: (optional) short label for the step.

This is by design, not an omission. An `AutoStep` delegates to a `@task`/`@workflow`, and the
intent lives on *that* function's decorator — purpose, inputs, outputs and critical are resolved
from the delegate target during the assemble phase. Writing them on the marker would duplicate
the target's own metadata and let the two drift apart. **If you want to describe what the step
does, put it on the target's `@task`, not on the `AutoStep`.**

- Use it when the function it calls is `@task`- or `@workflow`-decorated. axiom-graph links the AutoStep to that function with a `delegates_to` edge in `graph.db`.

#### Placing an AutoStep

The statement directly after an `AutoStep` is the call it names, so a reader sees what the step does at a glance. axiom-graph's rule B4 warns when it isn't, or when the called function isn't decorated.

- **One call, directly under the marker:** `f(...)` or `x = f(...)`.
- **Split nested calls** so the delegated call stands alone on its line:
  ```python
  口 = AutoStep(step_num=8, name="Render within budget")
  text = _pack_read(path, targets, max_chars, offset, shown)
  return _with_read_notes(text, shown)
  ```
- **Returns:** assign the call first, then return the variable.
- **Blocks:** when the call is inside a `try`, an `if` or a loop, the marker goes inside the block, directly above the call.
- **Guard clauses and helper lines** go above the marker.
- **A conditional expression** (`x = f(...) if c else None`) becomes an `if` block with the marker inside it.
- **The called function has no `@task`/`@workflow`:** use a plain `Step(name, purpose)`.

### Minor steps inside loops

Both `Step` and `AutoStep` can use minor numbering inside loops:

```python
for node in all_nodes:
    口 = AutoStep(step_num=7.1, name="Upsert node")
    db.upsert_node_conn(conn, node)
```

```python
for edge in all_edges:
    口 = Step(step_num=7.2, name="Validate edge")
    if not valid_edge(edge): warnings.append(edge)
```

## Common Mistakes

| Mistake | Error | Fix |
|---------|-------|-----|
| Minor step number (N.M) outside a loop | Invalid — minor steps are iteration markers | Use a major step number (integer) outside loops, or move the step inside the loop |
| Calling a `@task` from a `Step` instead of `AutoStep` | Works but the scanner won't auto-resolve the delegation edge | Change to `AutoStep(N)` when the step's sole purpose is delegating to a task |
| `AutoStep` above a `try`, an `if`, a guard clause or a helper call | B4: "not followed by a direct call statement" | Move the marker directly above the delegated call, inside the block if the call is in one |
| `AutoStep` on `return f(...)` or on a nested call `g(f(...))` | B4 | Put the delegated call on its own line under the marker (`x = f(...)`), then use `x` |
| `AutoStep` whose call has no `@task`/`@workflow` | B4: "target … is undecorated" | Use a plain `Step(name, purpose)` |

## Pattern Summary

```
@workflow
  Step(1)        — sequential phase
  Step(2)        — sequential phase
  Step(3)        — sequential phase
    for item in items:
      AutoStep(3.1)  — per-iteration (minor step, loop only)
      some_task(item)
  AutoStep(4)    — delegates to @task
  _my_task(args)

@task
  Step(1)        — first phase (optional)
  for row in rows:
    Step(1.1)      — per-iteration (minor step, loop only)
    process(row)
```

**Quick reminder:** Minor step numbers (N.M) can ONLY appear inside loops. Major step
numbers (integers) can appear anywhere.
