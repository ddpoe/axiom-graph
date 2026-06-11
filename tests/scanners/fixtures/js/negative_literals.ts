import { workflow, Step, AutoStep } from 'axiom-annotations';

const opts = {stepNum: 1, name: 'n', purpose: 'p'};

export const run = workflow({purpose: 'valid'})(async () => {
  Step(opts);
  Step({stepNum: 2, name: 'ok', purpose: 'pp'});
  AutoStep();
});
