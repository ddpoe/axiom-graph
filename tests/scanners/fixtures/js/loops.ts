import { workflow, AutoStep } from 'axiom-annotations';

export const run = workflow({purpose: 'loops'})(async (items: any[]) => {
  for (const x of items) {
    AutoStep({stepNum: 1.1});
  }
  for (let i = 0; i < 5; i++) {
    AutoStep({stepNum: 2.1});
  }
  let n = 0;
  while (n < 3) {
    AutoStep({stepNum: 3.1});
    n += 1;
  }
});
