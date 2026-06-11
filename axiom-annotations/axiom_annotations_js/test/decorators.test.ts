import test from "node:test";
import { strict as assert } from "node:assert";
import {
  setRegisterHook,
  task,
  workflow,
  type EnvelopeKind,
  type EnvelopeOpts,
  type RegisterHook,
} from "../src/decorators.js";
import { StepValidationError } from "../src/exceptions.js";

// Helper: clear the hook between tests so they don't leak state.
function withCleanHook(fn: () => void): void {
  setRegisterHook(null);
  try {
    fn();
  } finally {
    setRegisterHook(null);
  }
}

test("workflow: returns the wrapped function unchanged when no hook is set", () => {
  withCleanHook(() => {
    const original = (x: number) => x * 2;
    const wrapped = workflow({ purpose: "Double the input" })(original);
    assert.equal(wrapped, original);
    assert.equal(wrapped(3), 6);
  });
});

test("task: returns the wrapped function unchanged when no hook is set", () => {
  withCleanHook(() => {
    const original = (s: string) => s.toUpperCase();
    const wrapped = task({ purpose: "Uppercase a string" })(original);
    assert.equal(wrapped, original);
    assert.equal(wrapped("hi"), "HI");
  });
});

test("workflow: invokes register hook with WORKFLOW kind and full opts", () => {
  withCleanHook(() => {
    const calls: Array<{ fn: unknown; meta: EnvelopeOpts & { kind: EnvelopeKind } }> = [];
    const hook: RegisterHook = (fn, meta) => {
      calls.push({ fn, meta });
    };
    setRegisterHook(hook);

    const fn = () => "ok";
    workflow({
      purpose: "p",
      inputs: "i",
      outputs: "o",
      critical: "c",
    })(fn);

    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.fn, fn);
    assert.deepEqual(calls[0]?.meta, {
      purpose: "p",
      inputs: "i",
      outputs: "o",
      critical: "c",
      kind: "WORKFLOW",
    });
  });
});

test("task: invokes register hook with TASK kind", () => {
  withCleanHook(() => {
    let receivedKind: EnvelopeKind | null = null;
    setRegisterHook((_fn, meta) => {
      receivedKind = meta.kind;
    });

    task({ purpose: "p" })(() => undefined);
    assert.equal(receivedKind, "TASK");
  });
});

test("setRegisterHook(null): clears a previously installed hook", () => {
  withCleanHook(() => {
    let calls = 0;
    setRegisterHook(() => {
      calls += 1;
    });

    workflow({ purpose: "p" })(() => undefined);
    assert.equal(calls, 1);

    setRegisterHook(null);
    workflow({ purpose: "p" })(() => undefined);
    assert.equal(calls, 1, "hook should not fire after clear");
  });
});

test("hook receives a fresh copy of meta (mutation does not leak)", () => {
  withCleanHook(() => {
    const captured: Array<EnvelopeOpts & { kind: EnvelopeKind }> = [];
    setRegisterHook((_fn, meta) => {
      captured.push(meta);
    });

    const opts: EnvelopeOpts = { purpose: "p", inputs: "i" };
    workflow(opts)(() => undefined);

    // Mutate the original opts; hook's captured copy should be unaffected.
    opts.inputs = "MUTATED";
    assert.equal(captured[0]?.inputs, "i");
  });
});

test("decorator can be applied to async functions", () => {
  withCleanHook(() => {
    const fn = async (x: number) => x + 1;
    const wrapped = task({ purpose: "Increment" })(fn);
    assert.equal(typeof wrapped, "function");
    return wrapped(2).then((result) => {
      assert.equal(result, 3);
    });
  });
});

// ---------------------------------------------------------------------------
// Layer 2 runtime contract: workflow(opts)(fn) / task(opts)(fn) opts must
// be a plain object literal.
//
// The HOF outer call (workflow(opts) / task(opts)) is what gets validated —
// the inner (fn) call only receives the validated decorator. So we test the
// outer-call shapes here.
// ---------------------------------------------------------------------------

test("workflow: rejects undefined opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => workflow(undefined as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /undefined/);
      return true;
    },
  );
});

test("workflow: rejects null opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => workflow(null as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /null/);
      return true;
    },
  );
});

test("workflow: rejects array opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => workflow([1] as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /array/);
      return true;
    },
  );
});

test("workflow: rejects string opts with typeof-string in the message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => workflow("foo" as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /string/);
      return true;
    },
  );
});

test("workflow: rejects number opts with typeof-number in the message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => workflow(42 as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /number/);
      return true;
    },
  );
});

test("workflow: valid object literal still passes after Layer 2 check", () => {
  withCleanHook(() => {
    const fn = (x: number) => x;
    const wrapped = workflow({ purpose: "p" })(fn);
    assert.equal(wrapped, fn);
  });
});

test("task: rejects undefined opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => task(undefined as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /undefined/);
      return true;
    },
  );
});

test("task: rejects null opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => task(null as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /null/);
      return true;
    },
  );
});

test("task: rejects array opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => task([1] as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /array/);
      return true;
    },
  );
});

test("task: rejects string opts with typeof-string in the message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => task("foo" as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /string/);
      return true;
    },
  );
});

test("task: rejects number opts with typeof-number in the message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => task(42 as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /number/);
      return true;
    },
  );
});

test("task: valid object literal still passes after Layer 2 check", () => {
  withCleanHook(() => {
    const fn = (x: number) => x;
    const wrapped = task({ purpose: "p" })(fn);
    assert.equal(wrapped, fn);
  });
});
