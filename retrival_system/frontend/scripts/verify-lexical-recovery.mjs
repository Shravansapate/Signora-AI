/** Real database acceptance matrix. Store responses without credentials. */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';

const { SIGNORA_BROWSER_ORIGIN: origin, SIGNORA_BROWSER_TOKEN: token, SIGNORA_BROWSER_OUTPUT: output } = process.env;
if (!origin || !token || !output) throw new Error('Set origin, operator token and output directory.');
await mkdir(output, { recursive: true });
const cases = [
  ['Train ACCIDENT', 2], ['train accident', 2], ['TRAIN ACCIDENT', 2], ['Train Accident', 2],
  ['Train accident danger'], ['Train accident xyzabc', 8], ['Train accident unknownword', 13],
  ['train emergency'], ['train danger'], ['platform accident'], ['railway accident'],
  ['accident train'], ['train fire'], ['train help'], ['passenger accident'], ['train emergency accident'],
  ['Train 1201 Arrives at platform 2', 8], ['Train 1201 Arrives at platfrm 2', 8],
  ['Train 1201 arrives at platfrom 2', 8], ['Train 1201 arrive at platform 2', 8],
  ['Train 1201 arriving at platform 2', 9], ['train 1201 arives at platform 2', 8],
  ['trian 1201 arrives at platform 2', 8], ['Train 1201 platform 2', 7],
  ['Train no 1201 arrives platform 2', 8], ['Train number 1201 is arriving at platform number 2', 9],
  ['1201 arrives at platform 2', 8], ['Train 1201 departs from platform 2'],
  ['Train 1201 departing from platfrm 2'], ['Train 1201 is delayed by 20 minutes'],
  ['Train 1201 has been cancelled'], ['Train accident at the platform', 3], ['🦄', 0],
];
const results = [];
for (const [text, expected] of cases) {
  const response = await fetch(`${origin}/api/v1/translate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ request_id: randomUUID(), station_id: 'NAGPUR', input_type: 'TEXT', text }),
  });
  assert.equal(response.status, 200);
  const body = await response.json(), trace = body.retrieval;
  results.push(body);
  await writeFile(`${output}/acceptance-responses.json`, JSON.stringify(results, null, 2));
  assert.equal(trace.last_resort, false);
  assert(!trace.matches.some(match => match.method === 'LAST_RESORT'));
  if (expected !== undefined) assert.equal(trace.clip_count, expected, text);
  if (expected === 0) {
    assert.equal(body.manifest, null);
    assert.equal(trace.retrieval_status, 'EMPTY');
  } else {
    assert.equal(body.status, 'READY', JSON.stringify(body.issues));
    assert(body.manifest.items.length > 0);
    assert.equal(body.manifest.items.length, trace.clip_count);
  }
  if (text.toLowerCase() === 'train accident') {
    assert.equal(trace.semantic_parse, null);
    assert.equal(trace.retrieval_status, 'COMPLETE');
    assert.equal(trace.semantic_coverage, 'LEXICAL_ONLY');
    assert.deepEqual(body.manifest.items.map(item => item.semantic_key), ['ISL_TRAIN_01', 'ISL_ACCIDENT_01']);
  }
  console.log(JSON.stringify({ text, normalized: trace.normalized_input, corrections: trace.normalization_corrections,
    semantic: trace.semantic_status, retrieval: trace.retrieval_status, clips: trace.clip_count }));
}
console.log(`Passed ${results.length} real database acceptance cases.`);
