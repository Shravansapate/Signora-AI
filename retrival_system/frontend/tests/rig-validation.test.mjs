import assert from 'node:assert/strict';
import test from 'node:test';
import {
  AnimationClip, Bone, BufferGeometry, Group, MeshBasicMaterial, NumberKeyframeTrack,
  QuaternionKeyframeTrack, Scene, Skeleton, SkinnedMesh, VectorKeyframeTrack,
} from 'three';
import {
  assertCompatibleRig, assertFinitePose, captureRigProfile, compareRigProfiles,
  validateClipBindings,
} from '../src/motion/rig-validation.mjs';
import { probeSequence } from '../scripts/mixer-probe.mjs';

// Small in-memory test scenes exercise Three.js behavior. They are not ISL assets.
function avatar() {
  const root = new Scene();
  const armature = new Group();
  armature.name = 'Armature';
  const hand = new Bone();
  hand.name = 'Hand';
  const finger = new Bone();
  finger.name = 'Finger';
  finger.position.y = 1;
  hand.add(finger);
  armature.add(hand);
  root.add(armature);
  const mesh = new SkinnedMesh(new BufferGeometry(), new MeshBasicMaterial());
  mesh.name = 'Avatar';
  mesh.morphTargetDictionary = { brow: 0, mouth: 1 };
  mesh.morphTargetInfluences = [0, 0];
  root.add(mesh);
  mesh.bind(new Skeleton([hand, finger]));
  return { root, hand, finger, armature, mesh };
}

function movement(name = 'motion', end = 2) {
  return new AnimationClip(name, 1, [
    new VectorKeyframeTrack('Hand.position', [0, 1], [0, 0, 0, end, 0, 0]),
    new QuaternionKeyframeTrack('Finger.quaternion', [0, 1], [0, 0, 0, 1, 0, 0, 0, 1]),
  ]);
}

function expectCode(callback, code) {
  assert.throws(callback, error => error.name === 'MotionValidationError' && error.code === code);
}

test('independent rigs compare by structure, not object identity; opposite quaternions agree', () => {
  const first = avatar();
  const second = avatar();
  second.finger.quaternion.set(0, 0, 0, -1);
  assert.equal(assertCompatibleRig(captureRigProfile(first.root), captureRigProfile(second.root)).compatible, true);
});

for (const [name, mutate, code] of [
  ['parent transform', a => { a.armature.position.x = 0.1; }, 'INITIAL_TRANSFORM'],
  ['local transform', a => { a.finger.scale.y = 1.1; }, 'INITIAL_TRANSFORM'],
  ['hierarchy', a => a.armature.add(a.finger), 'HIERARCHY'],
  ['inverse bind pose', a => { a.mesh.skeleton.boneInverses[0].elements[12] = 1; }, 'INVERSE_BINDS'],
  ['skin bind matrix', a => { a.mesh.bindMatrix.elements[0] = 2; }, 'SKIN_BIND_MATRIX'],
  ['joint order', a => a.mesh.skeleton.bones.reverse(), 'SKIN_JOINTS'],
  ['morph mapping', a => { a.mesh.morphTargetDictionary = { mouth: 0, brow: 1 }; }, 'MORPH_MAPPING'],
]) {
  test(`matching bone names cannot hide changed ${name}`, () => {
    const first = avatar();
    const second = avatar();
    mutate(second);
    const result = compareRigProfiles(captureRigProfile(first.root), captureRigProfile(second.root));
    assert.equal(result.compatible, false);
    assert.ok(result.differences.some(difference => difference.code === code));
    expectCode(() => assertCompatibleRig(captureRigProfile(first.root), captureRigProfile(second.root)), 'INCOMPATIBLE_RIG');
  });
}

test('duplicate or absent bone identities fail profile capture', () => {
  const { root } = avatar();
  const duplicate = new Bone();
  duplicate.name = 'Finger';
  root.add(duplicate);
  expectCode(() => captureRigProfile(root), 'AMBIGUOUS_BONE');
  expectCode(() => captureRigProfile(new Scene()), 'NO_BONES');
});

test('binding validates real properties including constant fingers and required morph channels', () => {
  const { root, hand, finger, mesh } = avatar();
  const clip = movement();
  clip.tracks.push(new NumberKeyframeTrack('Avatar.morphTargetInfluences', [0, 1], [0, 0, 0.5, 1]));
  const bindings = validateClipBindings(root, clip);
  assert.deepEqual(bindings.map(binding => binding.node), [hand, finger, mesh]);
  assert.deepEqual(bindings.map(binding => binding.size), [3, 4, 2]);
  assert.equal(probeSequence(root, [clip]).completionEvents, 1);
});

for (const [name, mutate, code] of [
  ['missing target', c => { c.tracks[0].name = 'Missing.position'; }, 'MISSING_TARGET'],
  ['indexed target', c => { c.tracks[0].name = 'Hand.position[x]'; }, 'UNSUPPORTED_TRACK_PATH'],
  ['hierarchy target', c => { c.tracks[0].name = 'Armature/Hand.position'; }, 'UNSUPPORTED_TRACK_PATH'],
  ['unsupported property', c => { c.tracks[0].name = 'Hand.visible'; }, 'UNSUPPORTED_CHANNEL'],
  ['prototype property', c => { c.tracks[0].name = 'Hand.constructor'; }, 'UNSUPPORTED_CHANNEL'],
  ['duplicate track', c => c.tracks.push(c.tracks[0].clone()), 'DUPLICATE_TRACK'],
  ['wrong width', c => { c.tracks[0].values = new Float32Array(4); }, 'TRACK_WIDTH'],
  ['negative time', c => { c.tracks[0].times[0] = -1; }, 'INVALID_TIMELINE'],
  ['repeated time', c => { c.tracks[0].times[1] = 0; }, 'INVALID_TIMELINE'],
  ['time outside clip', c => { c.tracks[0].times[1] = 2; }, 'INVALID_TIMELINE'],
  ['nonfinite time', c => { c.tracks[0].times[1] = NaN; }, 'NONFINITE_VALUE'],
  ['nonfinite value', c => { c.tracks[0].values[0] = Infinity; }, 'NONFINITE_VALUE'],
  ['nonfinite duration', c => { c.duration = NaN; }, 'INVALID_CLIP'],
  ['empty clip', c => { c.tracks = []; }, 'INVALID_CLIP'],
]) {
  test(`invalid ${name} fails before playback`, () => {
    const { root } = avatar();
    const clip = movement();
    mutate(clip);
    expectCode(() => validateClipBindings(root, clip), code);
  });
}

test('ambiguous target and unavailable required morph channel fail binding', () => {
  const { root, mesh } = avatar();
  const duplicate = new Group();
  duplicate.name = 'Hand';
  root.add(duplicate);
  expectCode(() => validateClipBindings(root, movement()), 'AMBIGUOUS_TARGET');
  root.remove(duplicate);
  const clip = new AnimationClip('required-face', 1, [
    new NumberKeyframeTrack('Avatar.morphTargetInfluences', [0, 1], [0, 0, 1, 1]),
  ]);
  delete mesh.morphTargetInfluences;
  expectCode(() => validateClipBindings(root, clip), 'UNSUPPORTED_CHANNEL');
});

for (const count of [1, 3, 12]) {
  test(`${count} sequential occurrences finish exactly once on the same avatar and restore it after cleanup`, () => {
    const { root, hand } = avatar();
    const before = captureRigProfile(root);
    const children = [...root.children];
    const first = movement('first');
    const second = movement('second', 4);
    const clips = Array.from({ length: count }, (_, index) => index % 2 ? second : first);
    const report = probeSequence(root, clips);
    assert.equal(report.occurrences, count);
    assert.equal(report.completionEvents, count);
    assert.equal(report.uniqueActions, Math.min(count, 2));
    assert.deepEqual(report.completed.map(item => item.clip), clips.map(clip => clip.name));
    assert.deepEqual(root.children, children);
    assert.deepEqual(hand.position.toArray(), [0, 0, 0]);
    assert.equal(compareRigProfiles(before, captureRigProfile(root)).compatible, true);
  });
}

test('twelve repeated occurrences share one action while all twelve replay', () => {
  const { root } = avatar();
  const clip = movement();
  const result = probeSequence(root, Array(12).fill(clip), 0.3);
  assert.equal(result.uniqueActions, 1);
  assert.equal(result.completionEvents, 12);
  assert.deepEqual(result.completed.map(item => item.index), Array.from({ length: 12 }, (_, index) => index));
});

test('a missing later clip target prevents the first occurrence from moving', () => {
  const { root, hand } = avatar();
  let changes = 0;
  const original = hand.position.fromArray.bind(hand.position);
  hand.position.fromArray = (...args) => { changes++; return original(...args); };
  const invalid = movement('bad');
  invalid.tracks[0].name = 'Unavailable.position';
  expectCode(() => probeSequence(root, [movement(), invalid]), 'MISSING_TARGET');
  assert.equal(changes, 0);
});

test('invalid sampled quaternion fails and cleanup restores the original pose', () => {
  const { root, finger } = avatar();
  const clip = new AnimationClip('invalid-pose', 1, [
    new QuaternionKeyframeTrack('Finger.quaternion', [0, 1], [0, 0, 0, 0, 0, 0, 0, 0]),
  ]);
  expectCode(() => probeSequence(root, [clip]), 'INVALID_QUATERNION');
  assert.deepEqual(finger.quaternion.toArray(), [0, 0, 0, 1]);
  assert.doesNotThrow(() => assertFinitePose(root));
  assert.equal(probeSequence(root, [movement()]).completionEvents, 1);
});

test('empty sequences and invalid sample steps fail immediately', () => {
  const { root } = avatar();
  assert.throws(() => probeSequence(root, []), /Provide clips/);
  for (const step of [0, -1, NaN, Infinity]) {
    assert.throws(() => probeSequence(root, [movement()], step), /positive sample step/);
  }
});
