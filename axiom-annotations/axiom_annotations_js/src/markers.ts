/**
 * Step and AutoStep markers.
 *
 * Provides the Step() and AutoStep() factory functions plus their marker
 * object classes (StepMarker and AutoStepMarker). Arguments are validated
 * at call time via the validation module.
 *
 * Strict inline-object-literal API: callers must pass an object literal
 * directly (`Step({stepNum: 1, name: '...', purpose: '...'})`). The axiom-graph
 * scanner statically extracts these fields from the AST; passing a
 * variable instead of a literal will work at runtime but yield no
 * envelope metadata in the graph.
 */

import { validateAutoStepArgs, validateStepArgs } from "./validation.js";
import { StepValidationError } from "./exceptions.js";

/**
 * Validate that an opts argument is a plain object literal at runtime.
 *
 * Layer 2 contract: callers must pass an object — undefined, null, arrays,
 * primitives, etc. all throw {@link StepValidationError} mentioning the
 * "inline object literal" expectation and the offending shape.
 *
 * @internal
 */
function _validateOptsObject(opts: unknown, callerName: string): void {
  if (opts === undefined || opts === null) {
    throw new StepValidationError(
      undefined,
      `${callerName}() expects an inline object literal, got ${opts === null ? "null" : "undefined"}`,
    );
  }
  if (Array.isArray(opts)) {
    throw new StepValidationError(
      undefined,
      `${callerName}() expects an inline object literal, got array`,
    );
  }
  if (typeof opts !== "object") {
    throw new StepValidationError(
      undefined,
      `${callerName}() expects an inline object literal, got ${typeof opts}`,
    );
  }
}

/**
 * Marker returned by Step().
 *
 * Holds step metadata for potential runtime use (logging, timing, etc.).
 * The axiom-graph AST scanner extracts the same data statically at registration
 * time.
 */
export class StepMarker {
  public readonly stepNum: number;
  public readonly name: string;
  public readonly purpose: string;
  public readonly inputs: string | undefined;
  public readonly outputs: string | undefined;
  public readonly critical: string | undefined;

  constructor(
    stepNum: number,
    name: string,
    purpose: string,
    inputs?: string,
    outputs?: string,
    critical?: string,
  ) {
    this.stepNum = stepNum;
    this.name = name;
    this.purpose = purpose;
    this.inputs = inputs;
    this.outputs = outputs;
    this.critical = critical;
  }

  toString(): string {
    return `StepMarker(${this.stepNum}, ${JSON.stringify(this.name)})`;
  }
}

/**
 * Marker returned by AutoStep().
 *
 * Holds the step number and an optional user-supplied name. All other
 * metadata (purpose, inputs, outputs, critical) is resolved from the
 * called function during the assemble phase by the axiom-graph scanner.
 */
export class AutoStepMarker {
  public readonly stepNum: number;
  public readonly name: string | undefined;

  constructor(stepNum: number, name?: string) {
    this.stepNum = stepNum;
    this.name = name;
  }

  toString(): string {
    if (this.name !== undefined) {
      return `AutoStepMarker(${this.stepNum}, ${JSON.stringify(this.name)})`;
    }
    return `AutoStepMarker(${this.stepNum})`;
  }
}

export interface StepOpts {
  stepNum: number;
  name: string;
  purpose: string;
  inputs?: string;
  outputs?: string;
  critical?: string;
}

export interface AutoStepOpts {
  stepNum: number;
  name?: string;
}

/**
 * Marker for inline code blocks within a workflow / task function.
 *
 * Validates arguments at runtime — throws StepValidationError if name
 * or purpose is missing/empty, or stepNum is invalid.
 *
 * Minor steps (1.1, 1.2) should only be used inside loops.
 *
 * @example
 *   const ax = Step({stepNum: 1, name: 'Filter cells', purpose: 'Remove low-quality cells'});
 *   const ax = Step({
 *     stepNum: 2,
 *     name: 'Train model',
 *     purpose: 'Fit classifier',
 *     inputs: 'Filtered data',
 *     outputs: 'Trained model',
 *     critical: 'Takes 30+ minutes',
 *   });
 */
export function Step(opts: StepOpts): StepMarker {
  _validateOptsObject(opts, "Step");
  validateStepArgs(opts.stepNum, opts.name, opts.purpose);
  return new StepMarker(
    opts.stepNum,
    opts.name,
    opts.purpose,
    opts.inputs,
    opts.outputs,
    opts.critical,
  );
}

/**
 * Marker that extracts documentation from the next function call.
 *
 * Validates that stepNum is a valid positive number. The next statement
 * should be a call to a workflow()-or-task()-wrapped function — this is
 * verified at registration time by the axiom-graph cross-step validator (not
 * here).
 *
 * @example
 *   const ax = AutoStep({stepNum: 1});
 *   const result = someDecoratedFunction(args);
 *
 *   const ax = AutoStep({stepNum: 2, name: 'Load raw data'});
 *   const result = loadData(args);
 */
export function AutoStep(opts: AutoStepOpts): AutoStepMarker {
  _validateOptsObject(opts, "AutoStep");
  validateAutoStepArgs(opts.stepNum, opts.name);
  return new AutoStepMarker(opts.stepNum, opts.name);
}
