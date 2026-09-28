import { AnimationMixer, LoopOnce } from 'three';
import { BrowserMotionStore, validateAssetReference } from './asset-store.mjs';
import { MotionValidationError, assertFinitePose, validateClipBindings } from './rig-validation.mjs';

const fail = (code, message) => { throw new MotionValidationError(code, message); };
const validId = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value);
const validHash = value => typeof value === 'string' && /^[0-9a-f]{64}$/u.test(value);

function capturePose(root) {
  const pose = [];
  root.traverse(node => pose.push({ node, position: node.position.clone(), quaternion: node.quaternion.clone(),
    scale: node.scale.clone(), morphs: node.morphTargetInfluences?.slice() }));
  return pose;
}
function restorePose(pose) {
  for (const { node, position, quaternion, scale, morphs } of pose) {
    node.position.copy(position); node.quaternion.copy(quaternion); node.scale.copy(scale);
    if (morphs) node.morphTargetInfluences.splice(0, morphs.length, ...morphs);
  }
}

export function validatePlaybackPlan(plan, now = Date.now()) {
  const developmentLive = plan?.schema_version === 5 && plan?.purpose === 'PUBLISHED' && plan?.development === true;
  const live = developmentLive || (plan?.schema_version === 4 && plan?.purpose === 'PUBLISHED');
  const announcement = (live && !developmentLive) || (plan?.schema_version === 3 && plan?.purpose === 'ANNOUNCEMENT_PREVIEW');
  if (!plan || !(developmentLive || announcement || (plan.schema_version === 2 && ['CONTENT_REVIEW', 'EXACT_CONTENT'].includes(plan.purpose)))
    || !validId(plan.manifest_id) || !validHash(plan.manifest_hash) || plan.output_language !== 'ISL'
    || plan.operational !== live || plan.readiness_policy !== 'FULL_MESSAGE'
    || !Array.isArray(plan.items) || !plan.items.length || plan.items.length > 64
    || !validId(plan.avatar?.profile_id) || !validHash(plan.avatar?.rig_fingerprint)) {
    fail('INVALID_MANIFEST', 'A supported, explicit ISL playback plan is required.');
  }
  if (announcement) {
    const context = plan.announcement;
    if (!context || !validId(context.template_version_id) || !validId(context.review_id) || !validId(context.input_id)
      || !validHash(context.definition_hash) || !validHash(context.meaning_hash)
      || !Number.isSafeInteger(context.template_revision) || context.template_revision < 1
      || !Number.isSafeInteger(context.station_revision) || context.station_revision < 1
      || typeof context.station_id !== 'string' || context.meaning?.station_id !== context.station_id) {
      fail('INVALID_MANIFEST', 'The reviewed announcement context is invalid.');
    }
  }
  if (developmentLive && (!validId(plan.announcement?.input_id) || typeof plan.announcement?.station_id !== 'string' || !plan.announcement.station_id)) fail('INVALID_MANIFEST', 'Invalid development station context.');
  if (live && (!validId(plan.delivery?.message_id) || !Number.isSafeInteger(plan.delivery.revision)
    || plan.delivery.revision < 1 || !Number.isSafeInteger(plan.delivery.source_revision) || plan.delivery.source_revision < 1
    || !Number.isInteger(plan.delivery.priority) || plan.delivery.priority < 0 || plan.delivery.priority > 3
    || !plan.safe_boundaries?.includes(plan.items.length - 1))) fail('INVALID_MANIFEST', 'Invalid live delivery identity or final boundary.');
  validateAssetReference(plan.avatar, plan.manifest_id);
  const issued = Date.parse(plan.issued_at);
  const expiry = Date.parse(plan.valid_until);
  if (!Number.isFinite(issued) || !Number.isFinite(expiry) || expiry <= issued || issued > now + 30_000) fail('INVALID_MANIFEST', 'The manifest validity interval is invalid.');
  if (expiry <= now) fail('MANIFEST_EXPIRED', 'This plan expired. Prepare a fresh plan.');
  if (plan.purpose === 'EXACT_CONTENT' && plan.items.length !== 1) fail('UNAPPROVED_COMPOSITION', 'Exact content playback requires a single complete motion.');
  let duration = 0;
  for (const [index, item] of plan.items.entries()) {
    validateAssetReference(item, plan.manifest_id);
    if (item.sequence_index !== index || !validId(item.concept_id) || !Number.isSafeInteger(item.version_no) || item.version_no < 1
      || typeof item.clip_name !== 'string' || !item.clip_name || typeof item.semantic_key !== 'string' || !item.semantic_key
      || typeof item.semantic_group !== 'string' || !item.semantic_group || item.playback_rate !== 1
      || !Number.isFinite(item.duration_seconds) || item.duration_seconds <= 0) fail('INVALID_MANIFEST', 'The manifest contains an invalid motion occurrence.');
    if ((plan.purpose === 'EXACT_CONTENT' && item.transition !== 'NONE')
      || (announcement && item.transition !== 'APPROVED_CUT')
      || ((plan.purpose === 'CONTENT_REVIEW' || developmentLive) && !((index === 0 && item.transition === 'NONE') || item.transition === 'REVIEW_CUT'))) {
      fail('UNAPPROVED_TRANSITION', 'The transition is not permitted by this playback contract.');
    }
    duration += item.duration_seconds;
  }
  if (!Number.isFinite(plan.estimated_duration_seconds) || duration > 600 || Math.abs(duration - plan.estimated_duration_seconds) > 0.01) fail('INVALID_MANIFEST', 'The complete message duration is invalid.');
  if (!Array.isArray(plan.safe_boundaries) || plan.safe_boundaries.some((value, index, list) => !Number.isInteger(value) || value < 0 || value >= plan.items.length || (index && value <= list[index - 1]))) {
    fail('INVALID_MANIFEST', 'Semantic boundaries are invalid.');
  }
  if (announcement && plan.safe_boundaries.some(index => index < plan.items.length - 1
    && plan.items[index].semantic_group === plan.items[index + 1].semantic_group)) {
    fail('INVALID_MANIFEST', 'An interruption boundary cannot split an identifier or semantic group.');
  }
  if (typeof plan.caption_text !== 'string' || plan.caption_text.length > 8192) fail('INVALID_MANIFEST', 'The exact text caption is invalid.');
  return plan;
}

async function mapConcurrent(values, concurrency, callback) {
  let cursor = 0;
  const results = new Array(values.length);
  const workers = Array.from({ length: Math.min(concurrency, values.length) }, async () => {
    while (cursor < values.length) {
      const index = cursor++;
      results[index] = await callback(values[index]);
    }
  });
  await Promise.all(workers);
  return results;
}

/** One scene, one mixer, and one generation own all occurrences of a complete plan. */
export class PlaybackController {
  constructor({ onState = () => {}, onAvatar = () => {}, validateManifest, assetStore = new BrowserMotionStore(), now = Date.now } = {}) {
    if (typeof validateManifest !== 'function') throw new Error('A server manifest validation callback is required.');
    this.onState = onState;
    this.onAvatar = onAvatar;
    this.validateManifest = validateManifest;
    this.store = assetStore;
    this.now = now;
    this.state = 'IDLE';
    this.generation = 0;
    this.index = -1;
    this.completed = 0;
    this.error = null;
    this.plan = null;
    this.root = null;
    this.restPose = null;
    this.mixer = null;
    this.action = null;
    this.clips = [];
    this.abort = null;
    this.pendingFinished = false;
    this.starting = false;
    this.disposed = false;
    this.leaseUntil = 0;
    this.boundaryStop = null;
    this.finishedListener = event => {
      if (this.state === 'PLAYING' && event.action === this.action && event.direction === 1) this.pendingFinished = true;
    };
  }
  get snapshot() {
    return { state: this.state, index: this.index, total: this.plan?.items.length ?? 0, completed: this.completed,
      manifestId: this.plan?.manifest_id ?? null, error: this.error };
  }
  emit(state = this.state) { this.state = state; this.onState(this.snapshot); }
  current(generation) { return !this.disposed && generation === this.generation; }
  freezeAndUncache() {
    if (!this.mixer) return;
    // Three restores the original pose when stopping. Preserve the last rendered pose atomically.
    const pose = capturePose(this.root);
    this.mixer.stopAllAction();
    this.mixer.uncacheRoot(this.root);
    restorePose(pose);
    this.action = null;
    this.pendingFinished = false;
  }
  supersede() {
    this.generation++;
    this.abort?.abort();
    this.abort = new AbortController();
    this.freezeAndUncache();
    this.clips = [];
    this.starting = false;
    this.boundaryStop = null;
  }
  grantLease(deadline) { this.leaseUntil = deadline; }
  requestBoundaryStop(reason = 'SUPERSEDED') {
    if (this.state === 'PLAYING') this.boundaryStop = reason;
    else this.cancel();
  }
  checkLease() {
    if (this.plan?.operational && performance.now() >= this.leaseUntil) {
      fail('LEASE_EXPIRED', 'Live information is no longer fresh. Reconnect before restarting this message.');
    }
  }
  setError(error) {
    this.abort?.abort();
    if (this.action) this.action.paused = true;
    this.error = { code: error.code ?? 'PLAYBACK_FAILED', message: error.message ?? 'Playback failed. Prepare a fresh plan.' };
    this.emit('ERROR');
  }
  async prepare(input, { token } = {}) {
    if (this.disposed) return false;
    this.supersede();
    const generation = this.generation;
    const signal = this.abort.signal;
    this.error = null;
    this.index = -1;
    this.completed = 0;
    this.plan = null;
    try {
      // Own an isolated plan so callers cannot mutate version identity during async loading.
      this.plan = validatePlaybackPlan(structuredClone(input), this.now());
      this.emit('PRELOADING');
      this.store.beginPlan(this.plan);
      const avatar = await this.store.loadAvatar(this.plan.avatar, { token, signal });
      if (!this.current(generation)) return false;
      if (!this.root) {
        this.root = avatar.root;
        this.restPose = capturePose(this.root);
        this.root.visible = false;
        this.mixer = new AnimationMixer(this.root);
        this.mixer.addEventListener('finished', this.finishedListener);
        this.onAvatar(this.root);
      } else if (avatar.root !== this.root) fail('AVATAR_SESSION_MISMATCH', 'The canonical avatar changed within the display session.');
      const unique = [...new Map(this.plan.items.map(item => [this.store.key(item, this.plan.avatar), item])).entries()];
      const loaded = await mapConcurrent(unique, 2, async ([key, item]) => {
        signal.throwIfAborted();
        const clip = await this.store.loadMotion(item, this.plan.avatar, { token, signal });
        signal.throwIfAborted();
        validateClipBindings(this.root, clip);
        return [key, clip];
      });
      if (!this.current(generation)) return false;
      validatePlaybackPlan(this.plan, this.now());
      const clips = new Map(loaded);
      this.clips = this.plan.items.map(item => clips.get(this.store.key(item, this.plan.avatar)));
      // All assets are ready before any first-frame pose becomes visible.
      this.activate(0, true);
      this.root.visible = true;
      this.emit('READY');
      return true;
    } catch (error) {
      if (this.current(generation)) this.setError(error);
      return false;
    }
  }
  activate(index, paused = false) {
    this.action?.stop();
    // An omitted channel means this compatible asset's rest value, never the previous sign's pose.
    // Restore and evaluate the next first frame synchronously, before rendering either state.
    restorePose(this.restPose);
    this.index = index;
    this.action = this.mixer.clipAction(this.clips[index]);
    this.action.reset().setLoop(LoopOnce, 1).setEffectiveWeight(1).setEffectiveTimeScale(1);
    this.action.clampWhenFinished = true;
    this.action.play();
    this.action.paused = paused;
    this.pendingFinished = false;
    this.mixer.update(0);
    assertFinitePose(this.root);
  }
  async start() {
    if (this.disposed || this.starting || !['READY', 'COMPLETE'].includes(this.state)) return false;
    const generation = this.generation;
    this.starting = true;
    try {
      validatePlaybackPlan(this.plan, this.now());
      await this.validateManifest(this.plan, { signal: this.abort.signal });
      if (!this.current(generation)) return false;
      validatePlaybackPlan(this.plan, this.now());
      this.checkLease();
      if (this.now() + this.plan.estimated_duration_seconds * 1000 >= Date.parse(this.plan.valid_until)) fail('MANIFEST_EXPIRED', 'Insufficient validity remains for complete playback. Prepare a fresh plan.');
      this.completed = 0;
      this.activate(0);
      this.emit('PLAYING');
      return true;
    } catch (error) {
      if (this.current(generation)) this.setError(error);
      return false;
    } finally { if (this.current(generation)) this.starting = false; }
  }
  update(deltaSeconds) {
    if (this.disposed || !this.plan || ['IDLE', 'ERROR', 'PRELOADING'].includes(this.state)) return;
    try {
      if (Date.parse(this.plan.valid_until) <= this.now()) fail('MANIFEST_EXPIRED', 'This plan expired. Prepare a fresh plan.');
      if (this.state !== 'PLAYING') return;
      this.checkLease();
      if (!Number.isFinite(deltaSeconds) || deltaSeconds < 0) fail('INVALID_FRAME_TIME', 'A nonnegative finite mixer delta is required.');
      this.mixer.update(deltaSeconds);
      assertFinitePose(this.root);
      // Handle completion after mixer.update returns; never mutate actions within Three's iteration.
      if (this.pendingFinished) {
        this.pendingFinished = false;
        this.completed++;
        if (this.completed === this.clips.length) this.emit('COMPLETE');
        else if (this.boundaryStop && this.plan.safe_boundaries.includes(this.index)) {
          this.action.paused = true;
          this.emit('INTERRUPTED');
        }
        else { this.activate(this.index + 1); this.emit('PLAYING'); }
      }
    } catch (error) { this.setError(error); }
  }
  cancel() {
    if (this.disposed) return;
    this.supersede();
    this.plan = null;
    this.index = -1;
    this.completed = 0;
    this.error = null;
    this.emit('IDLE');
  }
  dispose() {
    if (this.disposed) return;
    this.supersede();
    this.disposed = true;
    this.mixer?.removeEventListener('finished', this.finishedListener);
    this.store.dispose();
    this.root = null;
    this.restPose = null;
    this.mixer = null;
    this.plan = null;
    this.onState = () => {};
    this.onAvatar = () => {};
  }
}
