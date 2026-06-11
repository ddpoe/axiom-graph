// US-3 (static): full 7-variant non-literal Step matrix.
//
// The seven variants enumerated in the architect's test plan:
//   1. Step(opts)            -- identifier
//   2. Step({...spread})     -- spread element
//   3. Step(buildOpts())     -- function call result
//   4. Step(cond ? a : b)    -- ternary
//   5. Step(this.opts)       -- member expression
//   6. Step()                -- missing arg
//   7. Step(a, b)            -- multi-arg
//
// Each must produce one IMPORTANT finding and no step node.  A trailing
// valid Step({stepNum: 8, ...}) confirms subsequent extraction still works.
import { workflow, Step } from 'axiom-annotations';

const opts = {stepNum: 1, name: 'n', purpose: 'p'};
const base = {stepNum: 0, name: 'b', purpose: 'b'};
function buildOpts(): any {
  return opts;
}

class Holder {
  opts = {stepNum: 5, name: 'h', purpose: 'p'};
  fire(): void {
    Step(this.opts);
  }
}

export const run = workflow({purpose: 'matrix'})(async () => {
  Step(opts);                                      // 1: identifier
  Step({...base, stepNum: 2});                     // 2: spread
  Step(buildOpts());                               // 3: function-call result
  Step(true ? opts : base);                        // 4: ternary
  // (member-expression variant lives on Holder.fire above; it still scans here)
  Step();                                          // 6: missing arg
  Step(opts, base);                                // 7: multi-arg
  // Subsequent valid step still extracts:
  Step({stepNum: 8, name: 'ok', purpose: 'survives'});
});
