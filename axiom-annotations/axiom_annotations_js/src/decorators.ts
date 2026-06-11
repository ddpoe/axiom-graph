/**
 * HOF factories for `workflow()` and `task()`.
 *
 * JS has no native function decorators, so axiom-annotations uses the
 * higher-order-function form:
 *
 *   const myWorkflow = workflow({purpose: '...'})(async (...) => { ... });
 *
 * The axiom-graph JS scanner detects this exact pattern via tree-sitter and
 * extracts the envelope kwargs from the inline object literal. Passing a
 * variable instead of an object literal will work at runtime but yield no
 * envelope metadata in the graph.
 *
 * By default these factories simply invoke an optional registry hook and
 * return the wrapped function unchanged — so a zero-dependency consumer
 * can annotate code without any registry infrastructure. Applications
 * that want to collect decorated functions into a registry install a
 * callback via setRegisterHook().
 */

import { StepValidationError } from "./exceptions.js";

export type EnvelopeKind = "WORKFLOW" | "TASK";

/**
 * Validate that an opts argument is a plain object literal at runtime.
 *
 * Layer 2 contract for ``workflow()`` / ``task()`` factories.
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

export interface EnvelopeOpts {
  /** What this function accomplishes. Required. */
  purpose: string;
  /** Optional description of inputs. */
  inputs?: string;
  /** Optional description of outputs. */
  outputs?: string;
  /** Optional warning about time / resources. */
  critical?: string;
}

/**
 * Callback signature for registry integration.
 *
 * Invoked at decoration time with the wrapped function and the resolved
 * envelope metadata.
 */
export type RegisterHook = (
  fn: AnyFn,
  meta: EnvelopeOpts & { kind: EnvelopeKind },
) => void;

// Internal: any callable. We don't constrain return type or args.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyFn = (...args: any[]) => any;

let _registerHook: RegisterHook | null = null;

/**
 * Install (or clear) the registry callback for `workflow()` / `task()`.
 *
 * The hook, if set, is invoked as
 *   hook(fn, {purpose, kind, inputs, outputs, critical})
 * every time a `workflow()` or `task()` factory is applied. Pass `null`
 * to restore the default no-op behaviour.
 */
export function setRegisterHook(hook: RegisterHook | null): void {
  _registerHook = hook;
}

function _baseDecorator(
  opts: EnvelopeOpts,
  kind: EnvelopeKind,
): <T extends AnyFn>(fn: T) => T {
  return <T extends AnyFn>(fn: T): T => {
    if (_registerHook !== null) {
      _registerHook(fn, { ...opts, kind });
    }
    return fn;
  };
}

/**
 * HOF for workflow entry-point functions.
 *
 * Workflows are top-level orchestration functions that coordinate
 * multiple tasks. They appear in the axiom-graph workflow registry for
 * discovery.
 *
 * @example
 *   const runScrnaPipeline = workflow({
 *     purpose: 'Complete single-cell analysis pipeline',
 *   })(async (dataPath: string) => {
 *     // ...
 *   });
 */
export function workflow(opts: EnvelopeOpts): <T extends AnyFn>(fn: T) => T {
  _validateOptsObject(opts, "workflow");
  return _baseDecorator(opts, "WORKFLOW");
}

/**
 * HOF for task functions.
 *
 * Tasks are reusable units of work that can be called from workflows or
 * other tasks. AutoStep markers pull documentation from tasks.
 *
 * @example
 *   const loadAdata = task({
 *     purpose: 'Load and validate data from h5ad file',
 *   })(async (path: string) => {
 *     // ...
 *   });
 */
export function task(opts: EnvelopeOpts): <T extends AnyFn>(fn: T) => T {
  _validateOptsObject(opts, "task");
  return _baseDecorator(opts, "TASK");
}
