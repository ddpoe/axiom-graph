/**
 * Exceptions raised by axiom-annotations markers and decorators.
 */

/**
 * Raised when a Step() or AutoStep() marker has invalid arguments.
 *
 * Fires at runtime (when the marker call is evaluated), giving immediate
 * feedback during execution and in REPLs / notebooks.
 */
export class StepValidationError extends Error {
  public readonly stepNum: number | undefined;

  constructor(stepNum: number | undefined, message: string) {
    const prefix = stepNum !== undefined ? `Step ${stepNum}` : "Step";
    super(`${prefix}: ${message}`);
    this.name = "StepValidationError";
    this.stepNum = stepNum;
    Object.setPrototypeOf(this, StepValidationError.prototype);
  }
}
