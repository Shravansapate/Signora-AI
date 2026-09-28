import { LoadingManager } from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { MotionValidationError, assertCompatibleRig, captureRigProfile, validateClipBindings } from './rig-validation.mjs';

const MAX_ASSET_BYTES = 128 * 1024 * 1024;
const UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const ASSET_PATH = new RegExp(`^/api/v1/playback/(${UUID})/assets/(${UUID})/([0-9a-f]{64})\\.glb$`, 'u');
const fail = (code, message) => { throw new MotionValidationError(code, message); };
const checkSignal = signal => signal?.throwIfAborted();

export function validateAssetReference(reference, manifestId) {
  const match = typeof reference?.asset_url === 'string' && reference.asset_url.match(ASSET_PATH);
  if (!match || (manifestId && match[1] !== manifestId) || match[2] !== reference.motion_version_id || match[3] !== reference.sha256) {
    fail('INVALID_ASSET_URL', 'The immutable asset URL does not match its pinned manifest, version, and checksum.');
  }
  if (!Number.isSafeInteger(reference.size_bytes) || reference.size_bytes < 20 || reference.size_bytes > MAX_ASSET_BYTES) {
    fail('ASSET_SIZE', 'The declared asset size is outside the supported limit.');
  }
}

/** Reject external resources before GLTFLoader can initiate resource requests. */
export function inspectSelfContainedGlb(bytes) {
  if (!(bytes instanceof ArrayBuffer) || bytes.byteLength < 20 || bytes.byteLength > MAX_ASSET_BYTES) fail('INVALID_GLB', 'Invalid GLB size.');
  const view = new DataView(bytes);
  if (view.getUint32(0, true) !== 0x46546c67 || view.getUint32(4, true) !== 2 || view.getUint32(8, true) !== bytes.byteLength) {
    fail('INVALID_GLB', 'Invalid GLB 2.0 header.');
  }
  let offset = 12;
  let document;
  let binLength = 0;
  while (offset < bytes.byteLength) {
    if (offset + 8 > bytes.byteLength) fail('INVALID_GLB', 'Truncated GLB chunk header.');
    const length = view.getUint32(offset, true);
    const kind = view.getUint32(offset + 4, true);
    if (length % 4 || offset + 8 + length > bytes.byteLength) fail('INVALID_GLB', 'Truncated or unaligned GLB chunk.');
    if (offset === 12 && kind === 0x4e4f534a) {
      try { document = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(bytes, offset + 8, length))); }
      catch { fail('INVALID_GLB', 'Invalid GLB JSON.'); }
    } else if (document && kind === 0x004e4942 && !binLength) binLength = length;
    else fail('INVALID_GLB', 'Unexpected or duplicate GLB chunk.');
    offset += 8 + length;
  }
  if (!document || document.asset?.version !== '2.0') fail('INVALID_GLB', 'A glTF 2.0 document is required.');
  const pending = [document];
  while (pending.length) {
    const value = pending.pop();
    if (!value || typeof value !== 'object') continue;
    if (Object.hasOwn(value, 'uri')) fail('EXTERNAL_RESOURCE', 'Only embedded GLB resources are supported.');
    pending.push(...Object.values(value).filter(item => item && typeof item === 'object'));
  }
  if (document.buffers?.length !== 1 || !Number.isSafeInteger(document.buffers[0].byteLength) || document.buffers[0].byteLength <= 0 || document.buffers[0].byteLength > binLength) {
    fail('INVALID_GLB', 'One embedded binary buffer is required.');
  }
  for (const bufferView of document.bufferViews ?? []) {
    const start = bufferView.byteOffset ?? 0;
    if (bufferView.buffer !== 0 || !Number.isSafeInteger(start) || start < 0 || !Number.isSafeInteger(bufferView.byteLength) || bufferView.byteLength <= 0 || start + bufferView.byteLength > document.buffers[0].byteLength) {
      fail('INVALID_GLB', 'An embedded buffer view is outside the binary buffer.');
    }
  }
  for (const image of document.images ?? []) {
    if (!Number.isSafeInteger(image.bufferView) || !document.bufferViews?.[image.bufferView] || !['image/png', 'image/jpeg'].includes(image.mimeType)) {
      fail('UNSUPPORTED_IMAGE', 'Embedded PNG or JPEG images with valid buffer views are required.');
    }
  }
  return document;
}

export async function fetchVerifiedGlb(reference, { token, signal, fetchImpl = globalThis.fetch, cryptoImpl = globalThis.crypto } = {}) {
  validateAssetReference(reference);
  if (typeof token !== 'string' || !token || /[\r\n]/u.test(token)) fail('AUTH_REQUIRED', 'An in-memory access token is required.');
  checkSignal(signal);
  const timeout = AbortSignal.timeout(120000);
  signal = signal ? AbortSignal.any([signal, timeout]) : timeout;
  const response = await fetchImpl(reference.asset_url, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'model/gltf-binary' }, signal,
    redirect: 'error', cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer',
  });
  if (!response.ok) fail('ASSET_FETCH_FAILED', `Asset request failed (${response.status}). Prepare a fresh plan.`);
  const declared = response.headers.get('content-length');
  if (declared !== null && Number(declared) !== reference.size_bytes) fail('ASSET_SIZE', 'The response length differs from the pinned asset.');
  if (!response.body) fail('ASSET_FETCH_FAILED', 'Asset response has no readable body.');
  const reader = response.body.getReader();
  const bytes = new Uint8Array(reference.size_bytes);
  let offset = 0;
  try {
    while (true) {
      checkSignal(signal);
      const { value, done } = await reader.read();
      if (done) break;
      if (offset + value.byteLength > bytes.length) fail('ASSET_SIZE', 'Asset response exceeds its pinned size.');
      bytes.set(value, offset);
      offset += value.byteLength;
    }
    if (offset !== bytes.length) fail('ASSET_SIZE', 'Asset response is truncated.');
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally { reader.releaseLock(); }
  checkSignal(signal);
  if (!cryptoImpl?.subtle) fail('SECURE_CONTEXT_REQUIRED', 'Asset verification requires HTTPS or localhost Web Crypto.');
  const digest = [...new Uint8Array(await cryptoImpl.subtle.digest('SHA-256', bytes))].map(value => value.toString(16).padStart(2, '0')).join('');
  if (digest !== reference.sha256) fail('ASSET_CHECKSUM', 'Asset bytes do not match the pinned checksum.');
  inspectSelfContainedGlb(bytes.buffer);
  checkSignal(signal);
  return bytes.buffer;
}

/** Deduplicate shared resources, including ImageBitmap handles not released by Texture.dispose(). */
export function disposeScenes(roots, additionalTextures = []) {
  const geometries = new Set();
  const materials = new Set();
  const skeletons = new Set();
  const textures = new Set(additionalTextures.filter(Boolean));
  const images = new Set();
  for (const root of new Set(roots.filter(Boolean))) root.traverse(node => {
    if (node.geometry) geometries.add(node.geometry);
    if (node.skeleton) skeletons.add(node.skeleton);
    for (const material of Array.isArray(node.material) ? node.material : [node.material]) if (material) materials.add(material);
  });
  for (const material of materials) for (const value of Object.values(material)) if (value?.isTexture) textures.add(value);
  for (const texture of textures) {
    for (const image of Array.isArray(texture.image) ? texture.image : [texture.image]) if (image) images.add(image);
    texture.dispose();
  }
  for (const image of images) image.close?.();
  for (const material of materials) material.dispose();
  for (const geometry of geometries) geometry.dispose();
  for (const skeleton of skeletons) skeleton.dispose();
}

export async function parseBrowserGlb(bytes, { motionOnly = false } = {}) {
  const manager = new LoadingManager();
  manager.setURLModifier(url => {
    if (!url.startsWith('blob:')) fail('EXTERNAL_RESOURCE', 'External GLB resource requests are forbidden.');
    return url;
  });
  const loader = new GLTFLoader(manager);
  if (motionOnly) loader.register(parser => ({
    name: 'SIGNORA_ANIMATION_RESOURCES',
    beforeRoot() {
      // The persistent avatar owns the appearance. Motion files still load their
      // geometry, skins, rest transforms and tracks for full binding comparison,
      // but must not decode another copy of every embedded avatar texture.
      for (const mesh of parser.json.meshes ?? []) {
        for (const primitive of mesh.primitives) delete primitive.material;
      }
      parser.json.materials = [];
      parser.json.textures = [];
      parser.json.images = [];
    },
  }));
  const gltf = await loader.parseAsync(bytes, '');
  let textures = [];
  try {
    // GLTFLoader may resolve a failed texture to null. Treat that as a decoding failure.
    textures = await gltf.parser.getDependencies('texture');
    if (textures.some(texture => !texture?.image || !(texture.image.width > 0) || !(texture.image.height > 0))) {
      fail('TEXTURE_DECODE', 'An embedded texture could not be decoded.');
    }
    // The preview occupies a few hundred screen pixels. Keep original GLBs intact,
    // but avoid retaining/uploading five 2K/4K images on this local 8 GB PC.
    const resized = new Map();
    for (const texture of textures) {
      const original = texture.image;
      const scale = Math.min(1, 1024 / Math.max(original.width, original.height));
      if (scale === 1) continue;
      if (!resized.has(original)) resized.set(original, await createImageBitmap(original, {
        resizeWidth: Math.max(1, Math.round(original.width * scale)),
        resizeHeight: Math.max(1, Math.round(original.height * scale)),
        resizeQuality: 'high', premultiplyAlpha: 'none', colorSpaceConversion: 'none',
      }));
      texture.image = resized.get(original);
      texture.needsUpdate = true;
    }
    for (const original of resized.keys()) original.close?.();
    return { root: gltf.scene, scenes: gltf.scenes, animations: gltf.animations, textures };
  } catch (error) {
    disposeScenes(gltf.scenes, textures);
    throw error;
  }
}

const clipBytes = clip => clip.tracks.reduce((sum, track) => sum + track.times.byteLength + track.values.byteLength, 0);

export class AnimationCache {
  constructor({ maxEntries = 64, maxBytes = 128 * 1024 * 1024 } = {}) {
    if (!Number.isSafeInteger(maxEntries) || maxEntries < 1 || !Number.isSafeInteger(maxBytes) || maxBytes < 1) throw new Error('Cache limits must be positive integers.');
    this.maxEntries = maxEntries;
    this.maxBytes = maxBytes;
    this.entries = new Map();
    this.pinned = new Set();
    this.bytes = 0;
  }
  pin(keys) { this.pinned = new Set(keys); }
  get(key) {
    const value = this.entries.get(key);
    if (value) { this.entries.delete(key); this.entries.set(key, value); }
    return value?.clip;
  }
  put(key, clip) {
    if (this.entries.has(key)) return this.get(key);
    const bytes = clipBytes(clip);
    if (bytes > this.maxBytes) fail('CACHE_CAPACITY', 'A decoded motion exceeds the animation memory budget.');
    while (this.entries.size >= this.maxEntries || this.bytes + bytes > this.maxBytes) {
      const removable = [...this.entries.keys()].find(candidate => !this.pinned.has(candidate));
      if (removable === undefined) fail('CACHE_CAPACITY', 'The complete message exceeds the animation cache budget.');
      this.bytes -= this.entries.get(removable).bytes;
      this.entries.delete(removable);
    }
    this.entries.set(key, { clip, bytes });
    this.bytes += bytes;
    return clip;
  }
  clear() { this.entries.clear(); this.pinned.clear(); this.bytes = 0; }
}

export class BrowserMotionStore {
  constructor({ fetchImpl, cryptoImpl, parse = parseBrowserGlb, cache = new AnimationCache() } = {}) {
    this.fetchOptions = { fetchImpl, cryptoImpl };
    this.parse = parse;
    this.cache = cache;
    this.avatar = null;
    this.disposed = false;
  }
  key(item, avatar) { return `${avatar.profile_id}:${avatar.rig_fingerprint}:three-186-v1:${item.motion_version_id}:${item.sha256}:${item.clip_name}`; }
  beginPlan(plan) { this.cache.pin(plan.items.map(item => this.key(item, plan.avatar))); }
  async loadAvatar(reference, options) {
    checkSignal(options.signal);
    if (this.disposed) fail('DISPOSED', 'The avatar session is closed.');
    if (this.avatar) {
      if (this.avatar.identity !== `${reference.profile_id}:${reference.motion_version_id}:${reference.sha256}`) fail('AVATAR_SESSION_MISMATCH', 'Reset the display session before choosing another canonical avatar.');
      return this.avatar;
    }
    const bytes = await fetchVerifiedGlb(reference, { ...this.fetchOptions, ...options });
    const loaded = await this.parse(bytes);
    try {
      checkSignal(options.signal);
      if (this.disposed) fail('DISPOSED', 'The avatar session is closed.');
      const profile = captureRigProfile(loaded.root);
      for (const clip of loaded.animations) validateClipBindings(loaded.root, clip);
      loaded.root.visible = false;
      this.avatar = { ...loaded, profile, identity: `${reference.profile_id}:${reference.motion_version_id}:${reference.sha256}`, source: reference };
      return this.avatar;
    } catch (error) { disposeScenes(loaded.scenes ?? [loaded.root], loaded.textures); throw error; }
  }
  async loadMotion(item, reference, options) {
    checkSignal(options.signal);
    if (!this.avatar || this.disposed) fail('AVATAR_REQUIRED', 'Load the canonical avatar first.');
    const key = this.key(item, reference);
    let clip = this.cache.get(key);
    if (!clip) {
      let loaded;
      const isAvatar = item.motion_version_id === this.avatar.source.motion_version_id && item.sha256 === this.avatar.source.sha256;
      try {
        loaded = isAvatar ? this.avatar : await this.parse(await fetchVerifiedGlb(item, { ...this.fetchOptions, ...options }), { motionOnly: true });
        checkSignal(options.signal);
        if (this.disposed) fail('DISPOSED', 'The avatar session is closed.');
        assertCompatibleRig(this.avatar.profile, isAvatar ? this.avatar.profile : captureRigProfile(loaded.root));
        const matches = loaded.animations.filter(candidate => candidate.name === item.clip_name);
        if (matches.length !== 1) fail('CLIP_IDENTITY', 'The pinned animation name is missing or ambiguous.');
        clip = matches[0];
        validateClipBindings(this.avatar.root, clip);
        this.cache.put(key, clip);
      } finally { if (loaded && !isAvatar) disposeScenes(loaded.scenes ?? [loaded.root], loaded.textures); }
    }
    if (Math.abs(clip.duration - item.duration_seconds) > 1e-4) fail('CLIP_DURATION', 'The decoded motion duration differs from the pinned manifest.');
    return clip;
  }
  dispose() {
    this.disposed = true;
    this.cache.clear();
    if (this.avatar) {
      this.avatar.root.removeFromParent();
      disposeScenes(this.avatar.scenes ?? [this.avatar.root], this.avatar.textures);
      this.avatar = null;
    }
  }
}
