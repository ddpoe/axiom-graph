// JS-LIT-ENV fixture: the workflow() opts argument is an identifier, not an
// inline object literal.
//
// The scanner is expected to:
//   - emit one IMPORTANT finding with rule_id JS-LIT-ENV;
//   - skip envelope kwarg extraction (no envelope node);
//   - still create the function-level node for `run` (the call site's
//     wrapped function).
import { workflow } from 'axiom-annotations';

const opts = {purpose: 'p'};

export const run = workflow(opts)(async () => {
  console.log('hi');
});
