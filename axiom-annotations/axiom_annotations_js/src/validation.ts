/**
 * Per-marker argument validation for Step() and AutoStep().
 *
 * These validators check a single marker's arguments in isolation — they
 * are stateless and have no knowledge of surrounding steps. Sequence-level
 * checks (ordering, duplicates, minor-in-loops) live in the consumer
 * (axiom-graph) and are not part of this package.
 */

import { StepValidationError } from "./exceptions.js";

/**
 * Validate stepNum is a positive number (and not NaN — JS quirk:
 * `typeof NaN === 'number'` so we have to filter explicitly).
 *
 * @throws StepValidationError if stepNum is invalid.
 */
export function validateStepNum(stepNum: unknown): asserts stepNum is number {
  if (typeof stepNum !== "number" || Number.isNaN(stepNum)) {
    const got = stepNum === null ? "null" : typeof stepNum;
    throw new StepValidationError(
      undefined,
      `stepNum must be a number, got ${got}`,
    );
  }
  if (stepNum <= 0) {
    throw new StepValidationError(stepNum, "stepNum must be positive");
  }
}

/**
 * Validate arguments for a Step() marker.
 *
 * Mirrors the Python check order: empty/missing first, then type. This
 * means a 0 or false `name` triggers "required and cannot be empty"
 * rather than "must be a string", matching Python's `if not name`.
 *
 * @throws StepValidationError if any required argument is missing or invalid.
 */
export function validateStepArgs(
  stepNum: unknown,
  name: unknown,
  purpose: unknown,
): asserts stepNum is number {
  validateStepNum(stepNum);

  const nameIsEmpty =
    !name || (typeof name === "string" && name.trim() === "");
  if (nameIsEmpty) {
    throw new StepValidationError(
      stepNum,
      "'name' is required and cannot be empty",
    );
  }
  if (typeof name !== "string") {
    throw new StepValidationError(
      stepNum,
      `'name' must be a string, got ${typeof name}`,
    );
  }

  const purposeIsEmpty =
    !purpose || (typeof purpose === "string" && purpose.trim() === "");
  if (purposeIsEmpty) {
    throw new StepValidationError(
      stepNum,
      "'purpose' is required and cannot be empty",
    );
  }
  if (typeof purpose !== "string") {
    throw new StepValidationError(
      stepNum,
      `'purpose' must be a string, got ${typeof purpose}`,
    );
  }
}

/**
 * Validate arguments for an AutoStep() marker.
 *
 * @throws StepValidationError if stepNum is invalid or name is invalid
 *   (when provided).
 */
export function validateAutoStepArgs(
  stepNum: unknown,
  name?: unknown,
): asserts stepNum is number {
  validateStepNum(stepNum);

  if (name !== undefined && name !== null) {
    if (typeof name !== "string") {
      throw new StepValidationError(
        stepNum,
        `'name' must be a string, got ${typeof name}`,
      );
    }
    if (name.trim() === "") {
      throw new StepValidationError(stepNum, "'name' cannot be an empty string");
    }
  }
}
