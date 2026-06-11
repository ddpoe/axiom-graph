import { workflow, AutoStep } from 'axiom-annotations';
import * as Loader from './loader';

export const run = workflow({purpose: 'p'})(async () => {
  AutoStep({stepNum: 1});
  Loader.loadData();
});
