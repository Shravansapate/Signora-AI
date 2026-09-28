import assert from 'node:assert/strict';
import { test } from 'node:test';
import { apiRequest } from '../src/services/api.mjs';

test('deliberate DELETE carries the exact revision/hash body with memory-only authorization', async () => {
  const original = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, options) => { request = { path, ...options }; return new Response('{"status":"CLEANUP_QUEUED"}', { status: 202 }); };
  try {
    const body = { expected_revision: 3, confirm_sha256: 'a'.repeat(64), reason: 'Rejected candidate' };
    const value = await apiRequest('/api/v1/admin/signs/concept/motions/version', { token: 'session-secret', method: 'DELETE', body });
    assert.equal(request.method, 'DELETE'); assert.deepEqual(JSON.parse(request.body), body);
    assert.equal(request.headers.Authorization, 'Bearer session-secret');
    assert.equal(request.credentials, 'omit'); assert.equal(request.redirect, 'error');
    assert(!request.path.includes('session-secret')); assert.equal(value.status, 'CLEANUP_QUEUED');
  } finally { globalThis.fetch = original; }
});

test('validation and stale-revision errors expose actionable fields and request identity', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: [{ loc: ['body','definition','platforms'], msg: 'Platform identifiers must be unique' }] }), { status: 422, headers: { 'x-request-id': 'observed-request' } });
  try {
    await assert.rejects(apiRequest('/api/v1/admin/stations/TEST', { token: 'secret', body: {} }), error => error.status === 422 && error.requestId === 'observed-request' && error.message.includes('definition.platforms'));
    globalThis.fetch = async () => new Response('{"detail":"Refresh announcement revision"}', { status: 409 });
    await assert.rejects(apiRequest('/api/v1/announcements/id/revisions', { token: 'secret', body: {} }), /Refresh announcement revision/);
  } finally { globalThis.fetch = original; }
});
