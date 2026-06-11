import { workflow, Step, AutoStep } from 'axiom-annotations';

export const run = workflow({purpose: 'orchestrate'})(async (cfg: any) => {
  Step({stepNum: 1, name: 'Filter', purpose: 'Remove bad rows'});
  AutoStep({stepNum: 2});
  doWork();
});

function doWork(): void {
  console.log('hi');
}
