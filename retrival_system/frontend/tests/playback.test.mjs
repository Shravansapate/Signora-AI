import assert from 'node:assert/strict';
import { test } from 'node:test';
import { randomUUID, createHash, webcrypto } from 'node:crypto';
import { AnimationClip, Group, Bone, VectorKeyframeTrack } from 'three';
import { PlaybackController, validatePlaybackPlan } from '../src/motion/playback-controller.mjs';
import { AnimationCache, fetchVerifiedGlb, inspectSelfContainedGlb, disposeScenes } from '../src/motion/asset-store.mjs';

function plan(count = 1) {
  const id = randomUUID(), version = randomUUID(), sha = 'a'.repeat(64);
  const ref = { motion_version_id: version, sha256: sha, size_bytes: 32,
    asset_url: `/api/v1/playback/${id}/assets/${version}/${sha}.glb` };
  return { schema_version: 2, manifest_id: id, manifest_hash: 'b'.repeat(64), purpose: 'CONTENT_REVIEW',
    output_language: 'ISL', operational: false, readiness_policy: 'FULL_MESSAGE', caption_text: 'Unit fixture',
    issued_at: new Date(Date.now() - 1000).toISOString(), valid_until: new Date(Date.now() + 900000).toISOString(),
    avatar: { ...ref, profile_id: randomUUID(), rig_fingerprint: 'c'.repeat(64) },
    items: Array.from({ length: count }, (_, sequence_index) => ({ ...ref, sequence_index,
      concept_id: randomUUID(), version_no: 1, semantic_key: 'UNIT_FIXTURE', semantic_group: `review-${sequence_index}`,
      clip_name: 'move', duration_seconds: 0.25, transition: 'REVIEW_CUT', playback_rate: 1 })),
    safe_boundaries: Array.from({ length: count }, (_, i) => i), estimated_duration_seconds: count * .25 };
}
function setup(extra = {}) {
  const root = new Group(), bone = new Bone(); bone.name = 'Hand'; root.add(bone);
  const clip = new AnimationClip('move', .25, [new VectorKeyframeTrack('Hand.position', [0, .25], [0, 0, 0, 2, 0, 0])]);
  const events = [], counts = { motion: 0, avatar: 0, disposed: 0 };
  const store = { beginPlan() {}, key: item => item.motion_version_id,
    async loadAvatar() { return { root }; },
    async loadMotion() { counts.motion++; return clip; },
    dispose() { counts.disposed++; }, ...extra.store };
  const controller = new PlaybackController({ assetStore: store, validateManifest: async () => {},
    onAvatar: () => counts.avatar++, onState: state => events.push(state), ...extra.controller });
  return { controller, root, bone, clip, store, counts, events };
}

function livePlan() {
  const value = plan(6);
  Object.assign(value, { schema_version: 4, purpose: 'PUBLISHED', operational: true,
    safe_boundaries: [4, 5], delivery: { message_id: randomUUID(), revision: 1, source_revision: 1, priority: 2 },
    announcement: { template_version_id: randomUUID(), review_id: randomUUID(), input_id: randomUUID(),
      definition_hash: 'd'.repeat(64), meaning_hash: 'e'.repeat(64), template_revision: 1, station_revision: 1,
      station_id: 'TEST', meaning: { station_id: 'TEST' } } });
  value.items.forEach((item, index) => { item.transition = 'APPROVED_CUT'; item.semantic_group = index < 5 ? 'identifier' : 'event'; });
  return value;
}

test('development live plans play review clips with a fresh display lease and no fabricated review', async () => {
  const value = plan(2);
  Object.assign(value, { schema_version: 5, purpose: 'PUBLISHED', operational: true, development: true,
    announcement: { station_id: 'TEST', input_id: randomUUID() },
    delivery: { message_id: randomUUID(), revision: 1, source_revision: 1, priority: 2 } });
  const { controller, bone } = setup();
  assert(await controller.prepare(value));
  controller.grantLease(performance.now() + 15000);
  assert(await controller.start());
  controller.update(.125);
  assert(bone.position.x > 0);
  controller.update(.125); controller.update(.25);
  assert.equal(controller.state, 'COMPLETE');
  assert.throws(() => validatePlaybackPlan({ ...value, development: false }));
  assert.throws(() => validatePlaybackPlan({ ...value, announcement: {} }));
  controller.dispose();
});

test('live priority interruption cannot split a five-clip identifier', async () => {
  const { controller, root } = setup();
  await controller.prepare(livePlan());
  controller.grantLease(performance.now() + 15000);
  assert.equal(await controller.start(), true);
  controller.update(.25); controller.requestBoundaryStop('SUPERSEDED');
  for (let i = 1; i < 4; i++) { controller.update(.25); assert.equal(controller.state, 'PLAYING'); }
  controller.update(.25);
  assert.equal(controller.state, 'INTERRUPTED'); assert.equal(controller.completed, 5);
  assert.equal(controller.root, root); assert.equal(controller.index, 4);
  controller.update(1); assert.equal(controller.completed, 5);
  controller.dispose();
});

test('a cached operational plan requires a lease before start and stops on freshness loss', async () => {
  const { controller } = setup();
  await controller.prepare(livePlan());
  assert.equal(await controller.start(), false); assert.equal(controller.error.code, 'LEASE_EXPIRED');
  await controller.prepare(livePlan()); controller.grantLease(performance.now() + 15000);
  assert.equal(await controller.start(), true);
  controller.grantLease(0); controller.update(.01);
  assert.equal(controller.state, 'ERROR'); assert.equal(controller.action.paused, true);
  controller.dispose();
});

test('reviewed announcement plan preserves atomic identifier groups and rejects altered contracts', () => {
  const announcement = plan(6);
  Object.assign(announcement, { schema_version: 3, purpose: 'ANNOUNCEMENT_PREVIEW', safe_boundaries: [4, 5],
    announcement: { template_version_id: randomUUID(), review_id: randomUUID(), input_id: randomUUID(),
      definition_hash: 'd'.repeat(64), meaning_hash: 'e'.repeat(64), template_revision: 1, station_revision: 1,
      station_id: 'TEST', meaning: { station_id: 'TEST' } } });
  announcement.items.forEach((item, index) => { item.transition = 'APPROVED_CUT'; item.semantic_group = index < 5 ? 'identifier' : 'event'; });
  assert.equal(validatePlaybackPlan(announcement), announcement);
  for (const change of [
    copy => { copy.safe_boundaries = [1, 4, 5]; },
    copy => { copy.items[2].transition = 'REVIEW_CUT'; },
    copy => { copy.announcement.station_id = 'OTHER'; },
    copy => { copy.operational = true; },
  ]) {
    const copy = structuredClone(announcement); change(copy);
    assert.throws(() => validatePlaybackPlan(copy));
  }
});

for (const count of [1, 3, 12]) test(`${count} repeated occurrences finish in exact order on one avatar`, async () => {
  const { controller, bone, root, counts, events } = setup();
  assert.equal(await controller.prepare(plan(count)), true);
  assert.equal(controller.state, 'READY');
  assert.equal(bone.position.x, 0);
  assert.equal(await controller.start(), true);
  for (let i = 0; i < count; i++) {
    assert.equal(controller.index, i);
    controller.update(.125); assert.equal(bone.position.x, 1);
    controller.update(.125);
  }
  assert.equal(controller.state, 'COMPLETE'); assert.equal(controller.completed, count);
  assert.equal(counts.avatar, 1); assert.equal(counts.motion, 1);
  assert.equal(controller.root, root); assert.equal(bone.position.x, 2);
  controller.update(.5); assert.equal(bone.position.x, 2);
  assert.equal(await controller.start(), true); assert.equal(bone.position.x, 0);
  controller.cancel(); assert.equal(controller.mixer._actions.length, 0);
  const eventCount = events.length;
  controller.dispose(); controller.dispose(); controller.update(1);
  assert.equal(counts.disposed, 1); assert.equal(events.length, eventCount);
});

test('no first sign is visible when any required clip fails preload', async () => {
  const { controller, root } = setup({ store: { async loadMotion() { throw new Error('missing required motion'); } } });
  assert.equal(await controller.prepare(plan(3)), false);
  assert.equal(controller.state, 'ERROR'); assert.equal(root.visible, false);
  assert.equal(await controller.start(), false); controller.dispose();
});
test('server invalidation blocks a fully cached plan before playback', async () => {
  const { controller } = setup({ controller: { validateManifest: async () => { throw new Error('revoked'); } } });
  await controller.prepare(plan()); assert.equal(controller.state, 'READY');
  assert.equal(await controller.start(), false); assert.equal(controller.state, 'ERROR'); controller.dispose();
});
test('late preload completion cannot overwrite a newer queue', async () => {
  let release, calls = 0;
  const { controller, clip, store } = setup();
  store.loadMotion = async () => { if (++calls === 1) await new Promise(resolve => { release = resolve; }); return clip; };
  const first = controller.prepare(plan());
  await new Promise(resolve => setImmediate(resolve));
  const next = plan(3); await controller.prepare(next);
  release(); await first;
  assert.equal(controller.plan.manifest_id, next.manifest_id);
  assert.equal(controller.state, 'READY'); assert.equal(controller.clips.length, 3); controller.dispose();
});
test('cancel during start authorization prevents stale playback', async () => {
  let release;
  const { controller } = setup({ controller: { validateManifest: () => new Promise(resolve => { release = resolve; }) } });
  await controller.prepare(plan()); const starting = controller.start();
  controller.cancel(); release(); assert.equal(await starting, false); assert.equal(controller.state, 'IDLE'); controller.dispose();
});
test('expiry is enforced while ready and during playback', async () => {
  for (const started of [false, true]) {
    let now = Date.now();
    const { controller } = setup({ controller: { now: () => now } });
    const input = plan(); await controller.prepare(input); if (started) await controller.start();
    now = Date.parse(input.valid_until); controller.update(.1);
    assert.equal(controller.state, 'ERROR'); assert.equal(controller.error.code, 'MANIFEST_EXPIRED'); controller.dispose();
  }
});
test('caller mutation cannot change the prepared ordered snapshot', async () => {
  const { controller } = setup(); const input = plan(3);
  await controller.prepare(input); input.items.pop();
  assert.equal(controller.plan.items.length, 3); controller.dispose();
});
test('a new plan cannot inherit an omitted animated channel from the preceding sign', async () => {
  const { controller, bone, root, store } = setup();
  const other = new Bone(); other.name = 'OtherHand'; root.add(other);
  await controller.prepare(plan()); await controller.start(); controller.update(.25);
  assert.equal(bone.position.x, 2);
  store.loadMotion = async () => new AnimationClip('move', .25,
    [new VectorKeyframeTrack('OtherHand.position', [0, .25], [0, 0, 0, 2, 0, 0])]);
  await controller.prepare(plan());
  assert.equal(bone.position.x, 0, 'Missing channels use canonical rest values');
  assert.equal(other.position.x, 0);
  await controller.start(); controller.update(.25);
  assert.equal(bone.position.x, 0); assert.equal(other.position.x, 2); controller.dispose();
});
for (const [label, change] of [
  ['external asset URL', p => { p.items[0].asset_url = 'https://example.com/secret'; }],
  ['partial readiness', p => { p.readiness_policy = 'PROGRESSIVE'; }],
  ['operational composition', p => { p.operational = true; }],
  ['invented blend', p => { p.items[0].transition = 'CROSSFADE'; }],
  ['lost occurrence', p => { p.items[0].sequence_index = 2; }],
]) test(`manifest rejects ${label}`, () => { const p = plan(); change(p); assert.throws(() => validatePlaybackPlan(p)); });

function glb(document = { asset: { version: '2.0' }, buffers: [{ byteLength: 4 }] }) {
  const json = Buffer.from(JSON.stringify(document).padEnd(Math.ceil(JSON.stringify(document).length / 4) * 4, ' '));
  const bytes = Buffer.alloc(12 + 8 + json.length + 8 + 4);
  bytes.writeUInt32LE(0x46546c67, 0); bytes.writeUInt32LE(2, 4); bytes.writeUInt32LE(bytes.length, 8);
  bytes.writeUInt32LE(json.length, 12); bytes.writeUInt32LE(0x4e4f534a, 16); json.copy(bytes, 20);
  bytes.writeUInt32LE(4, 20 + json.length); bytes.writeUInt32LE(0x004e4942, 24 + json.length);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}
test('asset reader verifies actual streamed bytes and never sends token in the URL', async () => {
  const bytes = glb(), ref = plan().avatar;
  ref.sha256 = createHash('sha256').update(new Uint8Array(bytes)).digest('hex');
  ref.asset_url = ref.asset_url.replace(/[^/]+\.glb$/, `${ref.sha256}.glb`); ref.size_bytes = bytes.byteLength;
  const result = await fetchVerifiedGlb(ref, { token: 'unit-secret', cryptoImpl: webcrypto,
    fetchImpl: async (url, options) => {
      assert(!url.includes('unit-secret')); assert.equal(options.headers.Authorization, 'Bearer unit-secret');
      assert.equal(options.redirect, 'error'); return new Response(bytes);
    } });
  assert.deepEqual(result, bytes);
  await assert.rejects(fetchVerifiedGlb({ ...ref, sha256: 'd'.repeat(64), asset_url: ref.asset_url.replace(ref.sha256, 'd'.repeat(64)) },
    { token: 'unit-secret', cryptoImpl: webcrypto, fetchImpl: async () => new Response(bytes) }), /checksum/);
  await assert.rejects(fetchVerifiedGlb(ref, { token: 'unit-secret', fetchImpl: async () => new Response(new Uint8Array(bytes).slice(0, 20)) }), /truncated/);
});
test('GLB preflight rejects external resources and invalid declared length before decoding', () => {
  assert.throws(() => inspectSelfContainedGlb(glb({ asset: { version: '2.0' }, buffers: [{ byteLength: 4, uri: 'https://example.com/file' }] })), /embedded/);
  const bytes = glb(); new DataView(bytes).setUint32(8, 100, true);
  assert.throws(() => inspectSelfContainedGlb(bytes), /header/);
});
test('bounded cache retains required clips and evicts only unpinned entries', () => {
  const { clip } = setup(); const cache = new AnimationCache({ maxEntries: 1 });
  cache.put('one', clip); cache.pin(['one']); assert.throws(() => cache.put('two', clip), /budget/);
  cache.pin(['two']); cache.put('two', clip); assert.equal(cache.get('one'), undefined);
  assert.equal(cache.get('two'), clip); cache.clear(); assert.equal(cache.bytes, 0);
});
test('shared graphics resources and image handles are released once', () => {
  const counts = { texture: 0, image: 0, geometry: 0, material: 0 };
  const texture = { isTexture: true, image: { close() { counts.image++; } }, dispose() { counts.texture++; } };
  const root = new Group(); root.material = { map: texture, dispose() { counts.material++; } };
  root.geometry = { dispose() { counts.geometry++; } };
  disposeScenes([root, root], [texture]); assert.deepEqual(counts, { texture: 1, image: 1, geometry: 1, material: 1 });
});
