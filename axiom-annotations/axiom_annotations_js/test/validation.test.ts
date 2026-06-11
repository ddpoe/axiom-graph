import test from "node:test";
import { strict as assert } from "node:assert";
import {
  validateAutoStepArgs,
  validateStepArgs,
  validateStepNum,
} from "../src/validation.js";
import { StepValidationError } from "../src/exceptions.js";

// ---------------------------------------------------------------------------
// validateStepNum
// ---------------------------------------------------------------------------

test("validateStepNum: accepts positive integers and floats", () => {
  validateStepNum(1);
  validateStepNum(42);
  validateStepNum(2.1);
  validateStepNum(0.001);
});

test("validateStepNum: rejects zero", () => {
  assert.throws(
    () => validateStepNum(0),
    (e: unknown) =>
      e instanceof StepValidationError && /must be positive/.test(e.message),
  );
});

test("validateStepNum: rejects negatives", () => {
  assert.throws(
    () => validateStepNum(-1),
    (e: unknown) =>
      e instanceof StepValidationError && /must be positive/.test(e.message),
  );
});

test("validateStepNum: rejects NaN (JS quirk — typeof NaN === 'number')", () => {
  assert.throws(
    () => validateStepNum(Number.NaN),
    (e: unknown) =>
      e instanceof StepValidationError &&
      /must be a number/.test(e.message),
  );
});

test("validateStepNum: rejects strings, null, undefined, objects", () => {
  for (const bad of ["1", null, undefined, {}, [], true]) {
    assert.throws(
      () => validateStepNum(bad),
      (e: unknown) =>
        e instanceof StepValidationError && /must be a number/.test(e.message),
      `expected throw for ${JSON.stringify(bad)}`,
    );
  }
});

// ---------------------------------------------------------------------------
// validateStepArgs
// ---------------------------------------------------------------------------

test("validateStepArgs: accepts well-formed args", () => {
  validateStepArgs(1, "Filter", "Remove low-quality rows");
});

test("validateStepArgs: rejects empty name with 'required' message", () => {
  assert.throws(
    () => validateStepArgs(1, "", "purpose"),
    (e: unknown) =>
      e instanceof StepValidationError &&
      /'name' is required/.test(e.message),
  );
});

test("validateStepArgs: rejects whitespace-only name", () => {
  assert.throws(
    () => validateStepArgs(1, "   ", "purpose"),
    (e: unknown) =>
      e instanceof StepValidationError &&
      /'name' is required/.test(e.message),
  );
});

test("validateStepArgs: rejects empty purpose", () => {
  assert.throws(
    () => validateStepArgs(1, "name", ""),
    (e: unknown) =>
      e instanceof StepValidationError &&
      /'purpose' is required/.test(e.message),
  );
});

test("validateStepArgs: rejects non-string name (when truthy)", () => {
  assert.throws(
    () => validateStepArgs(1, 42, "purpose"),
    (e: unknown) =>
      e instanceof StepValidationError &&
      /'name' must be a string/.test(e.message),
  );
});

test("validateStepArgs: rejects non-string purpose (when truthy)", () => {
  assert.throws(
    () => validateStepArgs(1, "name", { not: "a string" }),
    (e: unknown) =>
      e instanceof StepValidationError &&
      /'purpose' must be a string/.test(e.message),
  );
});

test("validateStepArgs: error message includes step number", () => {
  assert.throws(
    () => validateStepArgs(7, "", "purpose"),
    (e: unknown) =>
      e instanceof StepValidationError && /^Step 7:/.test(e.message),
  );
});

// ---------------------------------------------------------------------------
// validateAutoStepArgs
// ---------------------------------------------------------------------------

test("validateAutoStepArgs: accepts valid step number with no name", () => {
  validateAutoStepArgs(1);
});

test("validateAutoStepArgs: accepts valid step number with name", () => {
  validateAutoStepArgs(1, "Load data");
});

test("validateAutoStepArgs: accepts undefined name explicitly", () => {
  validateAutoStepArgs(1, undefined);
});

test("validateAutoStepArgs: accepts null name (treated as omitted)", () => {
  validateAutoStepArgs(1, null);
});

test("validateAutoStepArgs: rejects invalid step number", () => {
  assert.throws(
    () => validateAutoStepArgs(-1),
    (e: unknown) =>
      e instanceof StepValidationError && /must be positive/.test(e.message),
  );
});

test("validateAutoStepArgs: rejects empty-string name", () => {
  assert.throws(
    () => validateAutoStepArgs(1, ""),
    (e: unknown) =>
      e instanceof StepValidationError &&
      /'name' cannot be an empty string/.test(e.message),
  );
});

test("validateAutoStepArgs: rejects whitespace-only name", () => {
  assert.throws(
    () => validateAutoStepArgs(1, "   "),
    (e: unknown) =>
      e instanceof StepValidationError &&
      /'name' cannot be an empty string/.test(e.message),
  );
});

test("validateAutoStepArgs: rejects non-string name", () => {
  assert.throws(
    () => validateAutoStepArgs(1, 42),
    (e: unknown) =>
      e instanceof StepValidationError &&
      /'name' must be a string/.test(e.message),
  );
});
