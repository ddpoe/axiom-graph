import test from "node:test";
import { strict as assert } from "node:assert";
import {
  AutoStep,
  AutoStepMarker,
  Step,
  StepMarker,
} from "../src/markers.js";
import { StepValidationError } from "../src/exceptions.js";

// ---------------------------------------------------------------------------
// Step()
// ---------------------------------------------------------------------------

test("Step: returns a StepMarker with required fields", () => {
  const m = Step({ stepNum: 1, name: "Filter", purpose: "Remove bad rows" });
  assert.ok(m instanceof StepMarker);
  assert.equal(m.stepNum, 1);
  assert.equal(m.name, "Filter");
  assert.equal(m.purpose, "Remove bad rows");
  assert.equal(m.inputs, undefined);
  assert.equal(m.outputs, undefined);
  assert.equal(m.critical, undefined);
});

test("Step: preserves optional fields", () => {
  const m = Step({
    stepNum: 2,
    name: "Train",
    purpose: "Fit model",
    inputs: "Filtered data",
    outputs: "Model",
    critical: "Takes 30 minutes",
  });
  assert.equal(m.inputs, "Filtered data");
  assert.equal(m.outputs, "Model");
  assert.equal(m.critical, "Takes 30 minutes");
});

test("Step: accepts minor step numbers", () => {
  const m = Step({ stepNum: 2.1, name: "x", purpose: "y" });
  assert.equal(m.stepNum, 2.1);
});

test("Step: throws on invalid args (delegates to validateStepArgs)", () => {
  assert.throws(
    () => Step({ stepNum: 1, name: "", purpose: "y" }),
    StepValidationError,
  );
  assert.throws(
    () => Step({ stepNum: 0, name: "x", purpose: "y" }),
    StepValidationError,
  );
});

test("Step: toString contains class name, step num, and quoted name", () => {
  const m = Step({ stepNum: 3, name: "Filter cells", purpose: "p" });
  assert.equal(m.toString(), 'StepMarker(3, "Filter cells")');
});

// ---------------------------------------------------------------------------
// AutoStep()
// ---------------------------------------------------------------------------

test("AutoStep: returns an AutoStepMarker with step number only", () => {
  const m = AutoStep({ stepNum: 1 });
  assert.ok(m instanceof AutoStepMarker);
  assert.equal(m.stepNum, 1);
  assert.equal(m.name, undefined);
});

test("AutoStep: preserves optional name", () => {
  const m = AutoStep({ stepNum: 2, name: "Load raw data" });
  assert.equal(m.stepNum, 2);
  assert.equal(m.name, "Load raw data");
});

test("AutoStep: throws on invalid step number", () => {
  assert.throws(
    () => AutoStep({ stepNum: -1 }),
    StepValidationError,
  );
});

test("AutoStep: throws on empty name", () => {
  assert.throws(
    () => AutoStep({ stepNum: 1, name: "   " }),
    StepValidationError,
  );
});

test("AutoStep: toString includes name when present, omits when absent", () => {
  assert.equal(AutoStep({ stepNum: 1 }).toString(), "AutoStepMarker(1)");
  assert.equal(
    AutoStep({ stepNum: 2, name: "Load" }).toString(),
    'AutoStepMarker(2, "Load")',
  );
});

// ---------------------------------------------------------------------------
// Layer 2 runtime contract: opts must be a plain object literal
//
// These assertions cover the strict-literal rejection matrix from US-3
// (runtime). For each marker we verify the four invalid shapes (undefined,
// null, array, primitive) throw StepValidationError mentioning the
// "inline object literal" expectation, and that valid usage still passes.
// ---------------------------------------------------------------------------

test("Step: rejects undefined opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => Step(undefined as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /undefined/);
      return true;
    },
  );
});

test("Step: rejects null opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => Step(null as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /null/);
      return true;
    },
  );
});

test("Step: rejects array opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => Step([1] as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /array/);
      return true;
    },
  );
});

test("Step: rejects string opts with typeof-string in the message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => Step("foo" as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /string/);
      return true;
    },
  );
});

test("Step: rejects number opts with typeof-number in the message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => Step(42 as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /number/);
      return true;
    },
  );
});

test("Step: valid object literal still passes after Layer 2 check", () => {
  const m = Step({ stepNum: 1, name: "n", purpose: "p" });
  assert.ok(m instanceof StepMarker);
});

test("AutoStep: rejects undefined opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => AutoStep(undefined as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /undefined/);
      return true;
    },
  );
});

test("AutoStep: rejects null opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => AutoStep(null as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /null/);
      return true;
    },
  );
});

test("AutoStep: rejects array opts with inline-object-literal message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => AutoStep([1] as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /array/);
      return true;
    },
  );
});

test("AutoStep: rejects string opts with typeof-string in the message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => AutoStep("foo" as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /string/);
      return true;
    },
  );
});

test("AutoStep: rejects number opts with typeof-number in the message", () => {
  assert.throws(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    () => AutoStep(42 as any),
    (err: unknown) => {
      assert.ok(err instanceof StepValidationError);
      assert.match((err as Error).message, /inline object literal/);
      assert.match((err as Error).message, /number/);
      return true;
    },
  );
});

test("AutoStep: valid object literal still passes after Layer 2 check", () => {
  const m = AutoStep({ stepNum: 1 });
  assert.ok(m instanceof AutoStepMarker);
});
