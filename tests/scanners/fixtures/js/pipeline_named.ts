import { workflow, AutoStep } from 'axiom-annotations';
import { loadData } from './loader';

export const run = workflow({purpose: 'p'})(async () => {
  AutoStep({stepNum: 1});
  loadData();
});
