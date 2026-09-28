import assert from 'node:assert/strict';
import test from 'node:test';
import { numberedStationDefinition, suggestedStationId } from '../src/services/station-setup.mjs';

test('basic station setup preserves advanced rules and builds the exact platform inventory', () => {
  const existing = { train_min_digits: 5, train_max_digits: 5, places: [{ id: 'SRC', name: 'Nagpur' }], platforms: ['01', '1A', '3'] };
  assert.deepEqual(numberedStationDefinition(' Nagpur ', '8', existing), { ...existing, name: 'Nagpur', platforms: ['1','2','3','4','5','6','7','8'] });
  assert.deepEqual(numberedStationDefinition('Nagpur Junction', '3', existing, false).platforms, ['01','1A','3']);
  assert.deepEqual(existing.platforms, ['01','1A','3']);
  for (const count of ['', '0', '-1', '1.5', '257', 'NaN']) assert.throws(() => numberedStationDefinition('Nagpur', count));
  assert.throws(() => numberedStationDefinition(' ', '8'));
  assert.equal(suggestedStationId('  Nagpur station  '), 'NAGPUR_STATION');
});
