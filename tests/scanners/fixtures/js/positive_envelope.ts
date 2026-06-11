import { workflow } from 'axiom-annotations';

export const run = workflow({purpose: 'p', critical: 'c'})(async (cfg: any) => {
  console.log(cfg);
});
