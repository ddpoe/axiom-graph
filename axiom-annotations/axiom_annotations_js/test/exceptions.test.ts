import test from "node:test";
import { strict as assert } from "node:assert";
import { StepValidationError } from "../src/exceptions.js";

test("StepValidationError: prefixes message with step number when provided", () => {
  const err = new StepValidationError(3, "name is required");
  assert.equal(err.message, "Step 3: name is required");
  assert.equal(err.stepNum, 3);
  assert.equal(err.name, "StepValidationError");
});

test("StepValidationError: omits step number from prefix when undefined", () => {
  const err = new StepValidationError(undefined, "stepNum must be a number");
  assert.equal(err.message, "Step: stepNum must be a number");
  assert.equal(err.stepNum, undefined);
});

test("StepValidationError: instanceof works after extends Error", () => {
  const err = new StepValidationError(1, "x");
  assert.ok(err instanceof StepValidationError);
  assert.ok(err instanceof Error);
});

test("StepValidationError: handles fractional step numbers (minor steps)", () => {
  const err = new StepValidationError(2.1, "minor steps only inside loops");
  assert.equal(err.message, "Step 2.1: minor steps only inside loops");
  assert.equal(err.stepNum, 2.1);
});
