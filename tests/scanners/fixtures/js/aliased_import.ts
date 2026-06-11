// Aliased-import fixture: `import { loadData as ld } from './loader'`.
//
// The scanner is expected to resolve the AutoStep delegates_to edge against
// the original_name (`loadData`), not the alias (`ld`).
import { workflow, AutoStep } from 'axiom-annotations';
import { loadData as ld } from './loader';

export const run = workflow({purpose: 'aliased'})(async () => {
  AutoStep({stepNum: 1});
  ld();
});
