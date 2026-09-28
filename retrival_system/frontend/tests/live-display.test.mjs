import assert from 'node:assert/strict';
import { test } from 'node:test';
import { randomUUID } from 'node:crypto';
import { DisplayProgress, selectQueue, LiveDisplay } from '../src/services/live-display.mjs';

function storage() {
  const data = new Map();
  return { getItem: key => data.get(key), setItem: (key, value) => data.set(key, value), data };
}
const item = (changes = {}) => ({ message_id: randomUUID(), manifest_id: randomUUID(), revision: 1,
  priority: 2, valid_until: new Date(Date.now()+90000).toISOString(), ...changes });

test('reload suppresses completed versions and stores no caption or credential', () => {
  const store = storage(), did = randomUUID(), completed = item(), pending = item();
  const progress = new DisplayProgress(store, did);
  progress.cursor = 9; progress.completed.add(completed.manifest_id); progress.save();
  const reloaded = new DisplayProgress(store, did);
  assert.equal(reloaded.cursor, 9);
  assert.deepEqual(selectQueue({ type: 'SYNC', cursor: 10, active: [completed, pending], events: [] }, reloaded), [pending]);
  assert.deepEqual(Object.keys(JSON.parse([...store.data.values()][0])), ['cursor', 'completed']);
});

test('authoritative state replaces missed corrections, expired work and completed server progress', () => {
  const old = item(), current = item({ message_id: old.message_id, revision: 2 });
  const progress = new DisplayProgress(storage(), randomUUID());
  const sync = { type: 'SYNC', cursor: 200, mode: 'SNAPSHOT', events: [], active: [current,
    item({ valid_until: new Date(Date.now()-1).toISOString() }), item({ progress: { state: 'COMPLETED' } })] };
  assert.deepEqual(selectQueue(sync, progress), [current]);
  assert.throws(() => selectQueue({ ...sync, active: [current, old] }, progress));
  assert.throws(() => selectQueue({ ...sync, active: Array.from({ length: 33 }, () => item()) }, progress));
});

test('correction updates captions but asks the player for a safe stop instead of preparing mid-identifier', () => {
  const requests = [], player = { grantLease: value => requests.push(['lease', value]),
    requestBoundaryStop: value => requests.push(['boundary', value]), cancel() {} };
  const session = new LiveDisplay({ displayId: randomUUID(), token: 'memory-only', player,
    onStatus: value => requests.push(['status', value]), storage: storage(), origin: 'https://station.example' });
  const original = item(); session.current = original; session.state = 'PLAYING';
  session.sentAt = performance.now(); session.socket = { readyState: 1, send: value => requests.push(['send', value]), close() {} };
  const replacement = item({ message_id: original.message_id, revision: 2, caption_text: 'Corrected platform' });
  const now = Date.now();
  session.receive({ type: 'SYNC', cursor: 2, events: [], active: [replacement],
    server_time: new Date(now).toISOString(), lease_until: new Date(now+15000).toISOString() });
  assert(requests.some(([kind, value]) => kind === 'boundary' && value === 'SUPERSEDED'));
  assert.equal(session.current, original);
  assert(requests.some(([kind, value]) => kind === 'status' && value.caption === 'Corrected platform'));
  session.close();
});

test('lost completion ACK is recovered after reload without preparing or replaying the message', async () => {
  const store = storage(), did = randomUUID(), completed = item({ progress: { state: 'STARTED' }, last_boundary: 7 });
  const previous = new DisplayProgress(store, did);
  previous.completed.add(completed.manifest_id); previous.save();
  const sent = [];
  const session = new LiveDisplay({ displayId: did, token: 'memory-only', storage: store,
    origin: 'https://station.example', onStatus() {}, request() { assert.fail('Completed message fetched'); },
    player: { grantLease() {}, prepare() { assert.fail('Completed message prepared'); }, cancel() {} } });
  session.socket = { readyState: 1, send: value => sent.push(JSON.parse(value)), close() {} };
  session.sentAt = performance.now();
  const now = Date.now();
  session.receive({ type: 'SYNC', cursor: 4, events: [], active: [completed],
    server_time: new Date(now).toISOString(), lease_until: new Date(now+15000).toISOString() });
  assert.equal(session.current, null);
  assert.equal(sent[0].state, 'COMPLETED');
  assert.equal(sent[0].boundary, 7);
  session.receive({ type: 'ACKNOWLEDGED' });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(session.recovering.size, 0);
  assert.equal(session.queue.length, 0);
  session.close();
});

test('a superseded start acknowledgement rejects its promise without invalidating the socket', async () => {
  const session = new LiveDisplay({ displayId: randomUUID(), token: 'memory-only', storage: storage(),
    origin: 'https://station.example', onStatus() {}, player: { cancel() {} } });
  session.socket = { readyState: 1, close() {} };
  const promise = session.ack({ state: 'STARTED', manifest_id: randomUUID() });
  session.pending = session.acks.shift();
  session.receive({ type: 'ACKNOWLEDGED', accepted: false, detail: 'Message superseded' });
  await assert.rejects(promise, /superseded/u);
  assert.equal(session.closed, false);
  session.close();
});
