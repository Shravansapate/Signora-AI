/** Real local translation/retrieval audit; no approvals or source assets are altered. */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';

const { SIGNORA_BROWSER_ORIGIN: origin, SIGNORA_BROWSER_TOKEN: token, SIGNORA_BROWSER_OUTPUT: output } = process.env;
if (!origin || !token || !output) throw new Error('Provide local origin, operator token and artifact directory.');
await mkdir(output, { recursive: true });
const cases = [
  ['Train 1201 arrives at platform 2', ['TRAIN', '1201', 'PLATFORM', '2', 'ARRIVE']],
  ['Train 1201 is arriving at platform 2', ['TRAIN', '1201', 'PLATFORM', '2', 'NOW RIGHT NOW', 'ARRIVE']],
  ['Train 1201 arrives on platform 2', ['TRAIN', '1201', 'PLATFORM', '2', 'ARRIVE']],
  ['Train 2245 departs from platform 3', ['TRAIN', '2245', 'PLATFORM', '3', 'DEPART']],
  ['Train 1201 will arrive at platform 5', ['TRAIN', '1201', 'PLATFORM', '5', 'FUTURE', 'ARRIVE']],
  ['Train 1201 arrives at Chhatrapati Shivaji Maharaj Terminus', ['TRAIN', '1201', 'STATION', 'CHHATRAPATI SHIVAJI MAHARAJ TERMINUS', 'ARRIVE']],
  ['Train 1201 is arriving on platform number 2', ['TRAIN', '1201', 'PLATFORM', '2', 'NOW RIGHT NOW', 'ARRIVE']],
  ['The train arrives at platform 2', ['TRAIN', 'PLATFORM', '2', 'ARRIVE']],
  ['Train 1201 has been delayed', ['TRAIN', '1201', 'DELAY']],
  ['Train 1201 will arrive shortly', ['TRAIN', '1201', 'FUTURE', 'SHORTLY', 'ARRIVE']],
  ['Train 00120 has arrived at platform 2', ['TRAIN', '00120', 'PLATFORM', '2', 'ALREADY', 'ARRIVE']],
  ['Train 1201 will not arrive at platform 2', ['TRAIN', '1201', 'PLATFORM', '2', 'FUTURE', 'NOT', 'ARRIVE']],
];
const results = [];
for (const [text, expected] of cases) {
  const response = await fetch(`${origin}/api/v1/translate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ request_id: randomUUID(), station_id: 'NAGPUR', input_type: 'TEXT', text }),
  });
  assert.equal(response.status, 200);
  const body = await response.json(), trace = body.retrieval;
  assert.equal(body.status, 'READY', JSON.stringify(body.issues));
  assert.deepEqual(trace.gloss_tokens, expected);
  assert.deepEqual(trace.retrieval_tokens, expected);
  assert.equal(trace.last_resort, false);
  assert(body.manifest.items.length > 0);
  const spelled = trace.matches.filter(match => match.method === 'ALPHABET');
  if (text.includes('Chhatrapati')) {
    assert(spelled.length > 0);
    assert(spelled.every(match => match.gloss_token === 'CHHATRAPATI SHIVAJI MAHARAJ TERMINUS'));
  } else assert(spelled.every(match => !['AT', 'THE', 'IS'].includes(match.gloss_token)));
  if (trace.missing_gloss.length) assert.equal(trace.semantic_coverage, 'PARTIAL');
  results.push({ http_status: response.status, response: body });
  console.log(JSON.stringify({ input: text, normalized: trace.normalized_input,
    intent: body.meaning.intent, temporal: body.meaning.temporal_state, polarity: body.meaning.polarity,
    semantic_slots: body.meaning.slots, gloss: trace.gloss_tokens, retrieval_tokens: trace.retrieval_tokens,
    matches: trace.matches, fingerspelled: trace.fingerspelled,
    missing_gloss: trace.missing_gloss, clip_count: trace.clip_count }));
}
await writeFile(`${output}/acceptance-responses.json`, JSON.stringify({ status: 'PASSED', results }, null, 2));
console.log(`Passed ${results.length} real API semantic/gloss/retrieval cases.`);
