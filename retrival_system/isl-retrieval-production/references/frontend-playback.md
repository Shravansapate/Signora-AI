# Persistent avatar playback

## Canonical rig and assets

Inspect the actual avatar and existing player before editing. Load one canonical avatar per display session. Extract the selected animation from each GLB without replacing the visible character or adding its duplicate mesh.

Verify hierarchy, rest/local transforms, coordinate/scale convention, root motion, track targets, and required morph mapping. Matching bone names alone is insufficient. Read actual exported body/finger names; do not assume every asset uses a particular mixamorig prefix. Resolve targets to an unambiguous canonical map and reject missing/incompatible bindings before activation.

Preserve required manual and nonmanual channels. A constant handshape may be valid; do not require all fingers to move. Do not retarget an arbitrary skeleton at playback time or treat missing facial information as a search problem.

## State and timing

Use a dedicated controller with IDLE, PRELOADING, READY, PLAYING, TRANSITIONING, COMPLETE, and ERROR or equivalent states. Own message/revision, item index, atomic groups, queue generation, pending priority update, loading work, and completion progress in that controller.

Advance with mixer time and completion events, not arbitrary chained timers. Use one-shot/reset behavior deliberately; preserve the last approved pose until the next transition. Replay repeated occurrences even when they share one cached clip/action. Prevent a late load callback from restarting an obsolete queue using a generation or cancellation token. Dispose listeners and obsolete work at lifecycle boundaries.

Keep Three.js/browser initialization inside the client lifecycle when using Next.js. Do not recreate the avatar because page/React state changes. Keep per-frame scheduling out of component rerenders.

## Complete-message readiness and transitions

For operational announcements, load and decode every required motion and validate bindings before starting signing. Prewarm common assets to reduce latency. Progressive buffering is a later option only for complete reviewed informational units whose interruption is acceptable; do not apply it to a missing train/platform/action segment.

Protect meaningful motion and holds. Trim/blend only within reviewed entry/exit ranges, with a permitted transition policy. Do not impose a universal 80/150 ms crossfade or use greater blending to hide linguistic defects. Prefer a reviewed phrase where isolated signs cannot compose acceptably.

Never interrupt within a sign or an atomic identifier/name merely because a GLB ended. Use template semantic boundaries. On failure, preserve correct text/service status, stop safely where possible, and report a fresh-plan requirement instead of skipping the missing item. Avoid T-pose flashes or idle/breathing actions that interfere with meaningful channels.

## Caching and performance

Cache immutable asset versions together with avatar profile and runtime-representation version. Keep a bounded cache and dispose unused duplicate resources without disposing the persistent avatar or resources shared by active actions. Cache bytes once but preserve multiple sequence occurrences.

Routine replacement affects new plans; let valid published snapshots retain their versions. Deactivation/revocation can invalidate queued or active work through delivery policy. A byte-cache hit does not prove current semantic authorization.

Profile cold/warm download, decoding, frames, queue age, and decoded memory on target displays before optimizing. If duplicate mesh/texture payloads dominate, create an ingestion-time animation-only derivative with required node structure, source hash/version, and processor revision. Preserve the authoritative GLB; do not generate a sentence motion.

Use [GLTFLoader](https://threejs.org/docs/pages/GLTFLoader.html), [AnimationMixer](https://threejs.org/docs/pages/AnimationMixer.html), and [AnimationAction](https://threejs.org/docs/pages/AnimationAction.html) to confirm APIs for the installed Three.js version. Their controls do not certify ISL transitions.
