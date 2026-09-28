import { PropertyBinding } from 'three';

/** Technical evidence only: this module cannot grant linguistic or composition approval. */
export class MotionValidationError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'MotionValidationError';
    this.code = code;
  }
}

function fail(code, message) {
  throw new MotionValidationError(code, message);
}

function finite(values, context) {
  if (!Array.from(values).every(Number.isFinite)) fail('NONFINITE_VALUE', context);
}

function namedNodes(root) {
  const nodes = new Map();
  root.traverse((node) => {
    if (!node.name) return;
    const matches = nodes.get(node.name) ?? [];
    matches.push(node);
    nodes.set(node.name, matches);
  });
  return nodes;
}

/** Capture before any mixer updates; initial node pose and inverse bind pose are distinct. */
export function captureRigProfile(root) {
  const nodes = namedNodes(root);
  const bones = [];
  const skins = [];
  root.traverse((node) => {
    if (node.isBone) {
      if (!node.name || nodes.get(node.name).length !== 1) {
        fail('AMBIGUOUS_BONE', `Bone requires a unique name: ${node.name}`);
      }
      const ancestry = [];
      for (let ancestor = node; ancestor; ancestor = ancestor.parent) {
        const position = ancestor.position.toArray();
        const quaternion = ancestor.quaternion.toArray();
        const scale = ancestor.scale.toArray();
        finite([...position, ...quaternion, ...scale], ancestor.name);
        ancestry.push({ name: ancestor === root ? '$root' : ancestor.name, position, quaternion, scale });
        if (ancestor === root) break;
      }
      bones.push({ name: node.name, ancestry });
    }
    if (node.isSkinnedMesh) {
      if (!node.name || nodes.get(node.name).length !== 1) {
        fail('AMBIGUOUS_SKIN', `Skin requires a unique name: ${node.name}`);
      }
      const inverseBinds = node.skeleton.boneInverses.map((matrix) => matrix.toArray());
      inverseBinds.forEach((values) => finite(values, node.name));
      skins.push({
        name: node.name,
        joints: node.skeleton.bones.map((bone) => bone.name),
        inverseBinds,
        bindMatrix: node.bindMatrix.toArray(),
        bindMatrixInverse: node.bindMatrixInverse.toArray(),
        morphTargets: node.morphTargetDictionary ?? {},
      });
    }
  });
  if (!bones.length) fail('NO_BONES', 'Candidate avatar has no bones.');
  return { bones: bones.sort((a, b) => a.name.localeCompare(b.name)), skins: skins.sort((a, b) => a.name.localeCompare(b.name)) };
}

function vectorEqual(a, b, tolerance) {
  return a.length === b.length && a.every((value, index) => Math.abs(value - b[index]) <= tolerance);
}

function quaternionEqual(a, b, tolerance) {
  // q and -q encode the same rotation.
  return vectorEqual(a, b, tolerance) || vectorEqual(a, b.map((value) => -value), tolerance);
}

export function compareRigProfiles(candidate, source, tolerance = 1e-5) {
  if (!Number.isFinite(tolerance) || tolerance < 0) fail('INVALID_TOLERANCE', 'Tolerance must be finite and nonnegative.');
  const differences = [];
  const sources = new Map(source.bones.map((bone) => [bone.name, bone]));
  if (candidate.bones.length !== source.bones.length) differences.push({ code: 'BONE_COUNT' });
  for (const bone of candidate.bones) {
    const other = sources.get(bone.name);
    if (!other) { differences.push({ code: 'MISSING_BONE', bone: bone.name }); continue; }
    if (bone.ancestry.map((entry) => entry.name).join('/') !== other.ancestry.map((entry) => entry.name).join('/')) {
      differences.push({ code: 'HIERARCHY', bone: bone.name });
      continue;
    }
    for (let index = 0; index < bone.ancestry.length; index++) {
      const left = bone.ancestry[index];
      const right = other.ancestry[index];
      for (const property of ['position', 'quaternion', 'scale']) {
        const equals = property === 'quaternion' ? quaternionEqual : vectorEqual;
        if (!equals(left[property], right[property], tolerance)) {
          differences.push({ code: 'INITIAL_TRANSFORM', bone: bone.name, ancestor: left.name, property });
        }
      }
    }
  }
  const sourceSkins = new Map(source.skins.map((skin) => [skin.name, skin]));
  if (candidate.skins.length !== source.skins.length) differences.push({ code: 'SKIN_COUNT' });
  for (const skin of candidate.skins) {
    const other = sourceSkins.get(skin.name);
    if (!other) { differences.push({ code: 'MISSING_SKIN', skin: skin.name }); continue; }
    if (JSON.stringify(skin.joints) !== JSON.stringify(other.joints)) differences.push({ code: 'SKIN_JOINTS', skin: skin.name });
    if (!vectorEqual(skin.inverseBinds.flat(), other.inverseBinds.flat(), tolerance)) differences.push({ code: 'INVERSE_BINDS', skin: skin.name });
    for (const property of ['bindMatrix', 'bindMatrixInverse']) {
      if (!vectorEqual(skin[property], other[property], tolerance)) differences.push({ code: 'SKIN_BIND_MATRIX', skin: skin.name, property });
    }
    const orderedMorphs = (map) => Object.entries(map).sort(([left], [right]) => left.localeCompare(right));
    if (JSON.stringify(orderedMorphs(skin.morphTargets)) !== JSON.stringify(orderedMorphs(other.morphTargets))) {
      differences.push({ code: 'MORPH_MAPPING', skin: skin.name });
    }
  }
  return { compatible: differences.length === 0, tolerance, differences };
}

export function assertCompatibleRig(candidate, source) {
  const result = compareRigProfiles(candidate, source);
  if (!result.compatible) fail('INCOMPATIBLE_RIG', JSON.stringify(result.differences));
  return result;
}

/** Fail closed on unsupported channels; never rename or retarget tracks at runtime. */
export function validateClipBindings(root, clip) {
  if (!Number.isFinite(clip.duration) || clip.duration <= 0 || !clip.tracks.length) {
    fail('INVALID_CLIP', 'A motion needs tracks and a finite positive duration.');
  }
  const nodes = namedNodes(root);
  const seen = new Set();
  const resolved = [];
  for (const track of clip.tracks) {
    if (seen.has(track.name)) fail('DUPLICATE_TRACK', track.name);
    seen.add(track.name);
    const path = PropertyBinding.parseTrackName(track.name);
    if (!path.nodeName || path.objectName || path.objectIndex || path.propertyIndex || /[:/]/u.test(track.name)) {
      fail('UNSUPPORTED_TRACK_PATH', track.name);
    }
    const matches = nodes.get(path.nodeName) ?? [];
    if (matches.length !== 1) fail(matches.length ? 'AMBIGUOUS_TARGET' : 'MISSING_TARGET', track.name);
    const node = matches[0];
    const sizes = { position: 3, quaternion: 4, scale: 3, morphTargetInfluences: node.morphTargetInfluences?.length };
    const size = sizes[path.propertyName];
    if (!size || !Object.hasOwn(sizes, path.propertyName)) fail('UNSUPPORTED_CHANNEL', track.name);
    const cubic = track.createInterpolant.isInterpolantFactoryMethodGLTFCubicSpline === true;
    if (track.getValueSize() !== size * (cubic ? 3 : 1)) fail('TRACK_WIDTH', track.name);
    if (!track.times.length || track.times[0] < 0 || track.times.at(-1) > clip.duration) fail('INVALID_TIMELINE', track.name);
    finite(track.times, track.name);
    finite(track.values, track.name);
    for (let index = 1; index < track.times.length; index++) {
      if (track.times[index] <= track.times[index - 1]) fail('INVALID_TIMELINE', track.name);
    }
    const binding = PropertyBinding.create(root, track.name);
    const values = new Float64Array(size).fill(Number.NaN);
    binding.bind();
    binding.getValue(values, 0);
    binding.unbind();
    finite(values, `Unbound or nonfinite property: ${track.name}`);
    resolved.push({ track, node, property: path.propertyName, size });
  }
  return resolved;
}

export function assertFinitePose(root) {
  root.updateMatrixWorld(true);
  root.traverse((node) => {
    finite([...node.position, ...node.quaternion, ...node.scale, ...node.matrixWorld.elements], node.name);
    const norm = node.quaternion.lengthSq();
    if (Math.abs(norm - 1) > 1e-3) fail('INVALID_QUATERNION', node.name);
    if (node.morphTargetInfluences) finite(node.morphTargetInfluences, node.name);
    if (node.isSkinnedMesh) {
      node.skeleton.update();
      finite(node.skeleton.boneMatrices, node.name);
    }
  });
}
