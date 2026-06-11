// Default-import fixture: `import handler from './default_handler'`.
//
// Per D-4 (Builder), default-import bindings are stored as `(target_id, None)`
// — same as namespace bindings — so a bare `handler()` call produces NO
// delegates_to edge in v1.
import { workflow, AutoStep } from 'axiom-annotations';
import handler from './default_handler';

export const run = workflow({purpose: 'default-import'})(async () => {
  AutoStep({stepNum: 1});
  handler();
});
