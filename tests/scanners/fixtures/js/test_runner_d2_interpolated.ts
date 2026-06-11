import { test } from '@playwright/test';
import { workflow } from 'axiom-annotations';

const name = 'dynamic';

test(`behavior: ${name}`, workflow({purpose: 'should not produce envelope'})(async () => {
  // body
}));
