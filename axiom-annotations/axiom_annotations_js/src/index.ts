/**
 * axiom-annotations — zero-dependency workflow / task / Step / AutoStep
 * markers for JS/TS.
 *
 * Public surface:
 *
 *   import {
 *     workflow, task,             // HOF factories
 *     Step, AutoStep,             // inline marker factories
 *     StepMarker, AutoStepMarker, // marker classes
 *     StepValidationError,
 *     setRegisterHook,            // host-app integration point
 *   } from 'axiom-annotations';
 *
 * Per-marker validation helpers (validateStepNum, validateStepArgs,
 * validateAutoStepArgs) are exported for advanced consumers that need to
 * pre-validate without constructing a marker.
 */

export { StepValidationError } from "./exceptions.js";
export {
  setRegisterHook,
  task,
  workflow,
} from "./decorators.js";
export type {
  EnvelopeKind,
  EnvelopeOpts,
  RegisterHook,
} from "./decorators.js";
export {
  AutoStep,
  AutoStepMarker,
  Step,
  StepMarker,
} from "./markers.js";
export type { AutoStepOpts, StepOpts } from "./markers.js";
export {
  validateAutoStepArgs,
  validateStepArgs,
  validateStepNum,
} from "./validation.js";

export const VERSION = "0.2.0";
