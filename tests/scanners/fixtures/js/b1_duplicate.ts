// B1 fixture: two Step calls share stepNum=1 inside a single envelope.
//
// The scanner is expected to emit one B1 finding (duplicate step number) and
// keep first-occurrence-wins on node ID — only one node with id ending in
// `::step-1` is created.
import { workflow, Step } from 'axiom-annotations';

export const run = workflow({purpose: 'duplicate-major'})(async () => {
  Step({stepNum: 1, name: 'first', purpose: 'p1'});
  Step({stepNum: 1, name: 'second-duplicate', purpose: 'p2'});
});
