import { test } from '@playwright/test';
import { workflow } from 'axiom-annotations';

const helper = workflow({purpose: 'helper workflow used elsewhere'})(async () => {
  // never invoked from a test()
});

test('Inline test', workflow({purpose: 'inline-form test envelope'})(async () => {
  // body
}));
