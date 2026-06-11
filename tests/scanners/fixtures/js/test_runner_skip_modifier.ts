import { test } from '@playwright/test';
import { workflow } from 'axiom-annotations';

test.skip('skipped test', workflow({purpose: 'should not be discovered'})(async () => {
  // body
}));
