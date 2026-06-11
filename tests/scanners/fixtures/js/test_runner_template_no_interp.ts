import { test } from '@playwright/test';
import { workflow } from 'axiom-annotations';

test(`Plain template literal`, workflow({purpose: 'template without interpolation is fine'})(async () => {
  // body
}));
