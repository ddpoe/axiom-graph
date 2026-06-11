import { describe, test } from 'vitest';
import { workflow } from 'axiom-annotations';

describe('Bug 3 group', () => {
  test('Inside describe', workflow({purpose: 'nested one level'})(async () => {
    // body
  }));
});
