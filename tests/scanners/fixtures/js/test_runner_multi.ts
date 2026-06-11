import { test } from '@playwright/test';
import { workflow } from 'axiom-annotations';

test('First case', workflow({purpose: 'first'})(async () => {
  // body
}));

test('Second case', workflow({purpose: 'second'})(async () => {
  // body
}));

test('Third case', workflow({purpose: 'third'})(async () => {
  // body
}));
