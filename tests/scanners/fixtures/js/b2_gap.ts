// B2 fixture: major step numbers skip 2 (sequence is 1, 3).
//
// The scanner is expected to emit one B2 finding (major-gap) for the missing
// step.
import { workflow, Step } from 'axiom-annotations';

export const run = workflow({purpose: 'major-gap'})(async () => {
  Step({stepNum: 1, name: 'first', purpose: 'p'});
  Step({stepNum: 3, name: 'third', purpose: 'p'});
});
