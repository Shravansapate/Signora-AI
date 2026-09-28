import test from 'node:test';
import assert from 'node:assert/strict';
import { arrivalExample } from '../src/services/template-example.mjs';

const concepts = ['train', 'platform', 'arrive', 'now right now', 'zero', '1 one', '2 two']
  .map((name, index) => ({ id: `concept-${index}`, canonical_text: name, gloss: name.toUpperCase() }));
test('arrival example uses actual IDs and preserves repeated identifier digits', () => {
  const example = arrivalExample(concepts, 'avatar');
  assert.equal(example.avatar_profile_id, 'avatar');
  assert.deepEqual(example.recipe[1].policy.units['1201'], ['concept-5', 'concept-6', 'concept-4', 'concept-5']);
  assert.deepEqual(Object.keys(example.recipe[3].policy.units), ['1', '2']);
  assert.equal(example.recipe[0].concept_id, concepts[0].id);
});
test('example refuses missing or ambiguous concepts instead of inventing IDs', () => {
  assert.throws(() => arrivalExample(concepts.slice(1), 'avatar'), /train/u);
  assert.throws(() => arrivalExample([...concepts, { ...concepts[0], id: 'duplicate' }], 'avatar'), /unambiguous/u);
});
