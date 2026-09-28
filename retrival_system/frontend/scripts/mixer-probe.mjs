import { AnimationMixer, LoopOnce } from 'three';
import { MotionValidationError, assertFinitePose, validateClipBindings } from '../src/motion/rig-validation.mjs';

/** Synchronous engineering probe, not an operational announcement/transition controller. */
export function probeSequence(root, clips, stepSeconds = 1 / 30) {
  if (!clips.length || !Number.isFinite(stepSeconds) || stepSeconds <= 0) throw new Error('Provide clips and a positive sample step.');
  // Validate all occurrences before moving the single candidate avatar.
  const bindings = new Map(clips.map((clip) => [clip, validateClipBindings(root, clip)]));
  const mixer = new AnimationMixer(root);
  const cachedActions = new Map();
  const completed = [];
  let expectedAction;
  let eventCount = 0;
  let samples = 0;
  const finished = (event) => {
    if (event.action !== expectedAction || event.direction !== 1) throw new Error('Unexpected mixer completion event.');
    eventCount++;
  };
  mixer.addEventListener('finished', finished);
  try {
    for (const [index, clip] of clips.entries()) {
      const previous = expectedAction;
      const action = mixer.clipAction(clip);
      if (cachedActions.has(clip) && cachedActions.get(clip) !== action) throw new Error('Cached clip action changed.');
      cachedActions.set(clip, action);
      // Stop/reset/start/update is synchronous; no renderer can observe a restored rest pose in between.
      previous?.stop();
      expectedAction = action;
      action.reset().setLoop(LoopOnce, 1).setEffectiveWeight(1).setEffectiveTimeScale(1);
      action.clampWhenFinished = true;
      action.play();
      mixer.update(0);
      const resolved = bindings.get(clip).map((binding) => ({ ...binding, interpolant: binding.track.createInterpolant() }));
      const sample = () => {
        assertFinitePose(root);
        for (const binding of resolved) {
          const expected = binding.interpolant.evaluate(action.time);
          const property = binding.node[binding.property];
          const actual = property.toArray ? property.toArray() : property;
          const difference = (sign) => actual.every((value, item) => Math.abs(value - sign * expected[item]) < 5e-5);
          if (!difference(1) && !(binding.property === 'quaternion' && difference(-1))) {
            throw new MotionValidationError('MIXER_BINDING_MISMATCH', `${binding.track.name} at ${action.time}`);
          }
        }
        samples++;
      };
      sample();
      const eventsBefore = eventCount;
      const limit = Math.ceil(clip.duration / stepSeconds) + 2;
      for (let frame = 0; eventCount === eventsBefore && frame < limit; frame++) {
        mixer.update(Math.min(stepSeconds, Math.max(clip.duration - action.time, 1e-6)));
        sample();
      }
      if (eventCount !== eventsBefore + 1 || !action.paused || action.time !== clip.duration) {
        throw new Error(`Occurrence ${index} did not complete once at its end pose.`);
      }
      // A clamped clip must hold its final pose and emit no extra completion.
      mixer.update(stepSeconds);
      sample();
      if (eventCount !== eventsBefore + 1) throw new Error('Duplicate completion event.');
      completed.push({ index, clip: clip.name, durationSeconds: clip.duration });
    }
    return { occurrences: clips.length, uniqueActions: cachedActions.size, completionEvents: eventCount, sampledPoses: samples, completed };
  } finally {
    mixer.removeEventListener('finished', finished);
    mixer.stopAllAction();
    mixer.uncacheRoot(root);
  }
}
