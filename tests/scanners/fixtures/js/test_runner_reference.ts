import { test } from '@playwright/test';
import { workflow } from 'axiom-annotations';

const fooFlow = workflow({purpose: 'shared workflow body'})(async () => {
  // body
});

test('Foo flow regression', fooFlow);
