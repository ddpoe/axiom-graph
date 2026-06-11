import test from "node:test";
import { strict as assert } from "node:assert";
import * as ax from "../src/index.js";

test("public surface: all expected exports present", () => {
  // Runtime values
  assert.equal(typeof ax.workflow, "function");
  assert.equal(typeof ax.task, "function");
  assert.equal(typeof ax.Step, "function");
  assert.equal(typeof ax.AutoStep, "function");
  assert.equal(typeof ax.StepMarker, "function"); // constructor
  assert.equal(typeof ax.AutoStepMarker, "function");
  assert.equal(typeof ax.StepValidationError, "function");
  assert.equal(typeof ax.setRegisterHook, "function");
  assert.equal(typeof ax.validateStepNum, "function");
  assert.equal(typeof ax.validateStepArgs, "function");
  assert.equal(typeof ax.validateAutoStepArgs, "function");
  assert.equal(typeof ax.VERSION, "string");
});

test("public surface: end-to-end use via index re-exports", () => {
  ax.setRegisterHook(null); // ensure clean state
  const myTask = ax.task({ purpose: "Do thing" })(() => 42);
  assert.equal(myTask(), 42);

  const m = ax.Step({ stepNum: 1, name: "n", purpose: "p" });
  assert.ok(m instanceof ax.StepMarker);

  const am = ax.AutoStep({ stepNum: 2, name: "x" });
  assert.ok(am instanceof ax.AutoStepMarker);

  assert.throws(
    () => ax.Step({ stepNum: 0, name: "n", purpose: "p" }),
    ax.StepValidationError,
  );
});

test("VERSION string matches package version (0.2.0)", () => {
  assert.equal(ax.VERSION, "0.2.0");
});
