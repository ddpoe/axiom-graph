import { test } from '@playwright/test';
import { workflow } from 'axiom-annotations';

test('Same name', workflow({purpose: 'first wins'})(async () => {
  // first
}));

test('Same name', workflow({purpose: 'second loses'})(async () => {
  // second
}));
