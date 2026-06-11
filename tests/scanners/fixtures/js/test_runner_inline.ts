import { test } from '@playwright/test';
import { workflow, Step } from 'axiom-annotations';

test('Bug 3: node paints running before completed', workflow({purpose: 'regression for terminal-event defer'})(async ({page}: any) => {
  Step({stepNum: 1, name: 'Seed', purpose: 'set up timeline'});
  Step({stepNum: 2, name: 'Assert', purpose: 'check ordering'});
}));
