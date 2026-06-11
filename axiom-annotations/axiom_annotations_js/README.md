# axiom-annotations (JS/TS)

Zero-dependency JS/TS port of the [axiom-annotations](../axiom_annotations_py) Python package. Provides the shared annotation vocabulary used by [axiom-graph](https://github.com/ddpoe/axiom-graph) — for JS and TypeScript code.

Contains:

- `workflow()` and `task()` HOF factories
- `Step()` and `AutoStep()` inline marker factories
- `StepMarker` / `AutoStepMarker` marker classes
- `StepValidationError` exception
- Per-marker argument validation helpers

All markers are pure JS with **no third-party runtime dependencies**.

## Install

```bash
npm install axiom-annotations
```

## Usage

```typescript
import { workflow, task, Step, AutoStep } from 'axiom-annotations';

const loadData = task({
  purpose: 'Load and validate raw input file',
  inputs: 'file path',
  outputs: 'parsed records',
})(async (path: string) => {
  return JSON.parse(await fs.readFile(path, 'utf8'));
});

const runPipeline = workflow({
  purpose: 'Process input through full pipeline',
  outputs: 'final results',
  critical: 'Takes 10+ minutes on large inputs',
})(async (config) => {
  const ax1 = AutoStep({ stepNum: 1 });
  const records = await loadData(config.path);

  const ax2 = Step({
    stepNum: 2,
    name: 'Filter records',
    purpose: 'Drop invalid rows',
  });
  const clean = records.filter((r) => r.valid);

  for (const batch of chunk(clean, 100)) {
    const ax21 = AutoStep({ stepNum: 2.1 }); // minor steps inside loops only
    await processBatch(batch);
  }
});
```

## API differences from Python

| Python | TS / JS |
|---|---|
| `@workflow(purpose=..., critical=...)` decorator | `workflow({purpose, critical})(fn)` HOF — JS has no native function decorators |
| `Step(step_num=1, name=...)` keyword args | `Step({stepNum: 1, name: ...})` object literal |
| snake_case (`step_num`, `set_register_hook`) | camelCase (`stepNum`, `setRegisterHook`) |

The axiom-graph JS scanner statically extracts envelope and step metadata from the AST. **Options must be passed as inline object literals** — passing a variable instead works at runtime but yields no envelope metadata in the graph.

## Registry hook

By default `workflow()` and `task()` just invoke an optional registry hook and return the wrapped function unchanged. Tools that want to collect decorated functions install a hook:

```typescript
import { setRegisterHook } from 'axiom-annotations';

setRegisterHook((fn, meta) => {
  // meta = { purpose, kind: 'WORKFLOW' | 'TASK', inputs?, outputs?, critical? }
  myRegistry.add(fn, meta);
});
```

## Validation

Validation runs at two layers, complementary to one another:

### Layer 1 — Per-field validation (always)

Each marker validates its arguments at runtime and throws `StepValidationError` on bad input:

- `stepNum` must be a positive number (NaN explicitly rejected — JS gotcha)
- `Step({...})`: `name` and `purpose` must be non-empty strings
- `AutoStep({...})`: `name` (if provided) must be a non-empty string

### Layer 2 — Shape guard (since 0.2.0)

Before per-field validation, each marker first checks that the argument is an inline object literal at all. The shape guard rejects:

- `Step(undefined)` / `AutoStep(undefined)` / `workflow(undefined)` / `task(undefined)`
- `null`
- arrays (e.g., `Step([1, 2])`)
- non-object values (`Step('foo')`, `Step(42)`)

Each rejection throws `StepValidationError` mentioning "inline object literal" and the offending shape. Without this guard, calls like `Step(undefined)` would crash with a cryptic `TypeError: Cannot read properties of undefined (reading 'stepNum')` deep inside the field validator.

### Sequence-level rules

No duplicate step numbers, contiguous major numbering, minor steps only inside loops, etc. — these are enforced by the axiom-graph scanner, not this package.

### Static-vs-runtime asymmetry

JavaScript cannot detect "inline literal vs. variable holding the same object" at runtime — both produce identical values. So Layer 2 cannot fully enforce the static contract:

- **At runtime:** `Step({stepNum: 1, name: 'n', purpose: 'p'})` and `Step(opts)` (where `opts` is a variable holding the same object) are indistinguishable. Both pass Layer 2.
- **At scan time:** the axiom-graph scanner *requires* inline object literals — `Step(opts)` produces a "loud" finding (`JS-LIT-STEP`, severity `important`) and no step node is emitted in the graph.

Pass inline object literals to all four markers to get full graph integration. Layer 2 catches obvious shape mistakes; the scanner catches the "I expected the variable to be followed" mistake.

## Development

```bash
npm install
npm run build      # tsc → dist/
npm test           # build tests → run via node --test
```

Requires Node ≥ 18 to consume; Node ≥ 20 to run the test suite (stable `node --test`).

## Relationship to the Python package

The two packages share semantics but ship independently. They live as siblings under [`axiom-annotations/`](..) in the axiom-graph monorepo. See [`../README.md`](../README.md) for the multi-language overview.
