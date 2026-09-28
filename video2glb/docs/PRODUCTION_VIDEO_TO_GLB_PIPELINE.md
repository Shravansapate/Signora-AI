# Production Video-to-Animated-GLB Pipeline

Implementation reference: 4 September 2026

## Executive answer

This project converts a single-person Indian Sign Language MP4 into one rigged, animated GLB. The reusable path is:

```text
MP4 -> inspect/decode -> MediaPipe landmarks -> tracking QC
    -> avatar calibration -> body/palm/finger solve -> Blender bake/export
    -> fresh Blender re-import validation -> Khronos glTF validation
    -> source/avatar comparison -> immutable evidence -> release gate
```

The corrected `Change.glb` passes every implemented machine gate and is an **engineering candidate**. It is not yet a passenger-facing production release because the qualified-signer, full-clip human collision, meaning, licence, and provenance gates have not been completed.

The pipeline applies to compatible inputs without video-specific source-code edits, but it cannot guarantee that every MP4 will pass. It deliberately fails or requests review when tracking, timing, rigging, collision-proxy coverage, standards validation, hashes, or human release evidence are inadequate. A different avatar is calibrated into its own content-addressed profile, so an avatar-specific hand correction is never silently reused for different avatar bytes.

## What FFmpeg does and does not do

FFmpeg is useful around this pipeline, but it is not the MP4-to-GLB motion solver.

| Task | Current implementation | Effect on the GLB |
|---|---|---|
| Read codec, resolution, FPS, frame count, duration, and rotation metadata | `ffprobe` when available; OpenCV fallback | Establishes the source timebase and input diagnostics. |
| Decode frames for landmark tracking | OpenCV `VideoCapture` | Pixel quality, colour conversion, timestamps, and dropped/duplicated frames can affect MediaPipe landmarks. |
| Optional normalization before conversion | An operator may run `ffmpeg`; this is not an automatic converter stage | Can make variable-frame-rate or poorly supported input deterministic, but can also reduce landmark accuracy or change timing if configured badly. |
| Encode pose overlay and source/avatar comparison | OpenCV; Blender uses its FFmpeg-backed H.264 output for the avatar preview | Produces review media only. It does not change the baked animation in the GLB. |
| Detect body, wrist, palm, and finger motion | MediaPipe plus this project's tracking/QC code | FFmpeg does not perform this task. |
| Retarget motion, solve IK, bake the rig, and export GLB | This project's motion solver plus Blender | FFmpeg does not perform this task. |
| Validate glTF structure or sign meaning | Blender re-import, the pinned Khronos validator, and a qualified ISL reviewer | FFmpeg does not perform these tasks. |

If a friend's pipeline uses only FFmpeg, it cannot create an animated human GLB from an MP4. If it uses FFmpeg before its pose/retarget/export stages, that part can be reused as a carefully controlled media front end. Use it only after an A/B test shows equal or better frame/timestamp stability and tracking metrics. Do not transcode a clean constant-rate source repeatedly.

### Safe optional normalization

First inspect the original:

```powershell
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames:format=duration -of json .\input\Change.mp4
```

Normalize only when the source is variable-frame-rate, has an unsupported codec, has problematic rotation metadata, or decodes inconsistently. Write a new file; retain and hash the original:

```powershell
New-Item -ItemType Directory -Force -Path .\input\normalized | Out-Null
ffmpeg -hide_banner -i .\input\Change.mp4 -map 0:v:0 -an -vf "fps=25,format=yuv420p" -c:v libx264 -preset slow -crf 18 -movflags +faststart .\input\normalized\Change.normalized.mp4
```

Important consequences:

- `fps=25` can duplicate or drop frames. Use a product-approved target FPS, not an arbitrary value.
- H.264 at CRF 18 is high quality but still lossy. Compression artefacts around fingers can make hand landmarks worse.
- `-an` removes audio because this converter does not use it. Archive audio separately if it is part of the evidence record.
- The converter hashes the file it actually receives. If normalization is external, record the original hash, normalized hash, exact FFmpeg command, and FFmpeg version in the wider provenance system.
- The qualified reviewer must review the exact source representation bound into the conversion evidence, not an unrelated edit.

For a clean constant-rate 25 or 30 FPS H.264 MP4, feed the original directly to avoid needless loss.

## Production inputs and environment

Required inputs:

- One source MP4 with one visible signer, stable framing and lighting, and both hands visible during the sign.
- A licensed FBX avatar with a stable armature, weighted meshes, left/right wrists, and all 30 mapped finger bones.
- `models/holistic_landmarker.task` or the model configured in `config/settings.yaml`.
- A reviewed `config/settings.yaml` and `config/qc_thresholds.yaml`.
- A motion-catalog row containing at least `gloss`, `meaning`, `license`, `source_reference`, and `provenance_verified=true` for production release.
- A qualified ISL reviewer, an Ed25519 key held outside this repository, and its active public key in `config/trusted_signers.json`.

Minimum catalog shape (replace the bracketed values with verified facts, and set the Boolean to `true` only after provenance has actually been checked):

```csv
gloss,variant_no,meaning,license,source_reference,provenance_verified
CHANGE,1,<verified meaning>,<verified licence or usage rights>,<traceable source reference>,true
```

The currently validated workstation combination is Python 3.12 on Windows and Blender 4.5.11 LTS. The direct Python dependencies are pinned in `requirements.txt`:

```text
mediapipe==0.10.35
numpy==2.2.6
opencv-contrib-python==4.11.0.86
PyYAML==6.0.2
cryptography==50.0.0
```

The official glTF validation package is pinned in `package-lock.json` as `gltf-validator==2.0.0-dev.3.10`. Install the checked-in dependencies before conversion:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\requirements.txt
npm ci
```

Production workers should be built from a locked image, use a clean isolated environment, and run the committed test suite before deployment. Direct pins alone are not a complete transitive software-supply-chain lock.

## Exact 20-stage conversion flow

The following list matches the labels and order in `convert.py`.

1. **Inspect video.** Read codec, size, FPS, duration, frame count, rotation, and timestamp information. Verify that the video, avatar, and MediaPipe model exist.
2. **Extract holistic landmarks.** Decode every source frame and collect pose and left/right hand landmarks at the source timebase. A pose overlay is written when debug output is enabled.
3. **Save raw pose.** Save the unsmoothed observations to `<gloss>.pose.npz` before motion conditioning.
4. **Validate tracking.** Evaluate body availability, hand availability, long missing-hand runs, suspected left/right swaps, and invalid numeric values against `config/qc_thresholds.yaml`.
5. **Calibrate avatar/neutral hands.** Hash the exact FBX and use `config/avatar_calibrations/<avatar_sha256>/`. Validate or create `avatar_profile.json`, `avatar_bone_map.json`, and `neutral_hand_pose.json`, all bound to the avatar hash.
6. **Solve torso/arms.** Produce a diagnostic body-only motion using the canonical coordinate system and avatar-relative wrist targets.
7. **Export body GLB.** Bake and export the body-only diagnostic asset in headless Blender.
8. **Solve palms.** Add the conditioned anatomical palm basis while excluding final finger tracking.
9. **Export palm GLB.** Bake and export the palm diagnostic asset for stage isolation.
10. **Solve fingers/QC motion.** Produce the final motion NPZ with body, palms, local finger directions, neutral boundary weights, masks, and QC metadata. Centered offline smoothing is applied when enabled.
11. **Launch Blender/export GLB.** In Blender `--factory-startup` mode, apply avatar-length arm IK, tracked palms, and local finger retargeting; bake one animation; and export the candidate to `output/<GLOSS>/runs/<run_id>/<Gloss>.glb`.
12. **Re-import GLB.** Start a fresh factory Blender process, import the candidate, and validate the exact GLB hash, motion hash, rig contents, animation/timing, finite unit rotations, wrist error, hand visibility, finger motion, finger retargeting, palm adherence, opening/ending hands, and full-clip collision proxy.
13. **Validate glTF 2.0 compliance.** Run pinned `gltf-validator` tooling, require its expected version, bind the report to the same GLB SHA-256, and fail on validator errors or invalid warning policy.
14. **Render avatar preview.** Render the candidate at the source FPS for review. Production configuration must keep debug/review rendering enabled.
15. **Compare source/avatar.** Generate the side-by-side comparison and verify source/avatar frame count, FPS, and duration.
16. **Publish validated artifacts.** Only after candidate validation passes, atomically copy the GLB and review videos to their stable output paths. A same-output lock prevents concurrent writers.
17. **Write comparison/review reports.** Write the timing comparison and a hash-bound signer-review record. With no approval, the review remains pending.
18. **Write metadata/QC.** Atomically write metadata schema 3.0 and QC, evaluate the centralized production gate, and snapshot the complete release evidence into the run directory.
19. **Record research status.** Record whether a legacy TDPT/BVH/Rokoko baseline exists; never invent comparison results when it is unavailable.
20. **Commit release manifest.** Verify run IDs, stable and immutable GLB hashes, validator bindings, sizes, and every evidence record. Set `releaseable=true` only if production approval and manifest integrity both pass.

Every new conversion uses a random 32-character run ID and invalidates any approval attached to older artifact bytes. A failed candidate is quarantined under `failed/<GLOSS>/<run_id>/` with a hash-bound failure bundle rather than being published over the stable GLB.

## How the rotated initial hands were corrected

The original hand problem came from treating image-derived wrist orientation as if it already matched the avatar's rest-pose axes. At clip boundaries, low-confidence or missing hand observations could also expose an unsuitable FBX wrist roll. A smoothing filter can reduce jitter, but it cannot correct this coordinate-system mismatch by itself.

The implemented correction has four parts:

1. **Avatar-bound neutral pose.** The neutral hand is derived from the exact avatar profile, not from a global hard-coded wrist quaternion. The fallback applies mirrored anatomical wrist pronation (approximately 60 degrees) using each avatar palm normal, and validates all 15 mapped finger bones on each hand.
2. **Boundary blending.** Leading and trailing unreliable hand runs blend smoothly to the calibrated neutral wrist/finger pose. Neutral influence has priority so tracked palm orientation cannot overwrite the boundary pose.
3. **Full palm orientation.** A directed, right-handed palm basis is built from the observed wrist, index, middle, and little-finger landmarks, conditioned in rotation space, and mapped onto the avatar's rest-pose palm basis after arm IK.
4. **Palm-local fingers.** Finger segment directions are retargeted in the animated palm's local coordinates, preventing the fingers from inheriting the wrong global hand axes.

The smoother is centered, zero-phase, NaN-aware, and offline. It uses approximately 0.08 seconds of radius for body landmarks and 0.04 seconds for hand position, palm shape, and finger curls. At 25 FPS this is two frames for the body and one frame for the hands. Because it uses both preceding and following samples, it does not introduce the lag of a causal real-time filter. It is therefore unsuitable for live streaming.

## Hand/body collision handling

The fresh Blender re-import validator evaluates all frames with an anatomical torso/hand clearance proxy. For release-gate purposes it must:

- report `PASS`;
- explicitly declare `evaluated_every_input_frame=true`;
- evaluate every reported frame (`evaluated_coverage=1.0`);
- contain the same total frame count as the source; and
- remain hash-bound to the current GLB and motion evidence.

This proxy tests re-imported bone-head samples against an anatomical torso plane. It is **not mesh-aware**. It does not prove clearance for skinned geometry, clothing, space between landmarks, hand-to-hand contact, or finger self-collision, and monocular depth remains ambiguous during occlusion or intentional body contact. Therefore an independent full-clip human collision review is mandatory even when the proxy passes.

For stronger automatic protection, the next solver upgrade should add avatar-mesh or signed-distance-field collision constraints, capsule/volume constraints for the torso and garments, hand-hand/finger collision checks, temporal monocular depth estimation, and test clips with chest/face contact, crossed arms, occlusion, fast motion, and two-handed signs.

## Commands and exit codes

### Produce an engineering candidate

```powershell
python .\convert.py --video .\input\Change.mp4 --save-debug --motion-catalog .\motion_catalog.csv
```

`--save-debug` is explicit here because the comparison video is required release evidence. The current `config/settings.yaml` also enables it by default. A missing catalog does not stop engineering conversion, but it blocks production provenance.

### Enforce the production gate in automation

```powershell
python .\convert.py --video .\input\Change.mp4 --save-debug --motion-catalog .\motion_catalog.csv --require-production
```

Expected process codes:

- `0`: the requested acceptance condition passed. In normal mode, a valid engineering candidate is sufficient; with `--require-production`, every production gate passed.
- `2`: conversion produced output but the required engineering or production gate was not satisfied.
- `1`: processing, validation, integrity, or tooling failed.

### Batch technical processing

```powershell
python .\convert.py --input-dir .\input --batch --save-debug --motion-catalog .\motion_catalog.csv
```

One video failure does not stop the rest. Results are summarized in `output/batch_summary.json`. A shared `--signer-approval` is rejected in batch mode because every approval must bind one source, one exact GLB, and one exact comparison video.

### Revalidate quarantined candidates

```powershell
python .\convert.py --revalidate-failed --input-dir .\input --save-debug
```

Revalidation accepts only complete, untampered failure bundles and reuses their bundled source, avatar, pose, motion, calibration, settings, and report inputs. It refuses a bare GLB or a bundle with path, size, or hash mismatch. It also protects an intact current engineering/production candidate from accidental replacement.

## Signed two-phase approval workflow

Production approval should happen after conversion so the reviewer signs the exact artifact hashes.

1. Run the engineering conversion and keep its immutable run directory unchanged.
2. Complete the motion-catalog row. At minimum, production needs a verified meaning, licence/usage-rights statement, source reference, and `provenance_verified=true`.
3. Give the reviewer the exact source, immutable GLB, and side-by-side comparison from the evidence bundle.
4. The qualified ISL reviewer checks meaning, handshape, orientation, location, movement, timing, non-manual limitations, and the full clip for visible collisions.
5. The reviewer creates an approval containing the current source, GLB, and comparison SHA-256 values, then signs its canonical JSON bytes with an Ed25519 private key held outside the repository.
6. Provision only the corresponding public key in `config/trusted_signers.json`.
7. Apply the approval to the existing output. The converter verifies all immutable evidence, re-runs both GLB validators under the output lock, verifies the signature and hashes, re-evaluates every gate, and commits a new manifest.

Trusted public-key registry shape:

```json
{
  "schema_version": "1.0",
  "keys": {
    "signer-one-2026": {
      "active": true,
      "reviewer": "Qualified reviewer name",
      "public_key_base64": "BASE64_RAW_32_BYTE_ED25519_PUBLIC_KEY"
    }
  }
}
```

Approval shape:

```json
{
  "schema_version": "1.0",
  "gloss": "CHANGE",
  "signer_verdict": "PASS",
  "isl_verified": true,
  "reviewer": "Qualified reviewer name",
  "reviewed_at": "2026-09-04T15:00:00+05:30",
  "collision_review": {
    "status": "PASS",
    "method": "full-clip source/avatar visual review",
    "notes": "No unintended body or self-intersection observed."
  },
  "hash_binding": {
    "source_video_sha256": "64_HEX_CHARACTERS",
    "glb_sha256": "64_HEX_CHARACTERS",
    "comparison_video_sha256": "64_HEX_CHARACTERS"
  },
  "notes": "Reviewer-owned notes",
  "review_method": "qualified ISL review",
  "signature": {
    "algorithm": "Ed25519",
    "key_id": "signer-one-2026",
    "value_base64": "BASE64_SIGNATURE"
  }
}
```

The signed bytes are the allowlisted fields defined in `src/qc/signer_approval.py`, serialized as UTF-8 JSON with sorted keys and compact separators. Use that module's `canonical_approval_bytes()` when building the reviewer-side signing utility so the signed representation exactly matches verification. Do not store the private key in this repository.

Apply an approval to the existing immutable run:

```powershell
python .\convert.py --video .\input\Change.mp4 --approve-existing --signer-approval .\approvals\Change.approval.json --trusted-signers .\config\trusted_signers.json --motion-catalog .\motion_catalog.csv --require-production
```

Unknown, disabled, mismatched-reviewer, malformed, or tampered keys/signatures fail closed. The checked-in registry is intentionally empty until an operator provisions a real reviewer public key.

## Release gates

### Engineering-candidate gates

All of these must be true:

- Tracking technical QC is `PASS`.
- Fresh Blender re-import validation is `PASS`, has a validation run ID, and matches the current GLB SHA-256.
- Pinned Khronos validation is `PASS`, names the validator version, matches the current GLB SHA-256, and reports integer `numErrors=0`.
- Source/avatar validation is `PASS`, has a run ID, and matches the current source and comparison hashes.
- Full-clip collision proxy is `PASS`, covers and evaluates every source frame, and has a matching frame count.
- Current source and GLB SHA-256 values are valid.

### Additional production-release gates

All of these must also be true:

- `signer_verdict=PASS` and `isl_verified=true`.
- Reviewer identity and timezone-aware review timestamp are present.
- Ed25519 signature verification passes against an active trusted public key.
- Review source, GLB, and comparison hashes match the current artifacts.
- Independent full-clip collision review is `PASS`.
- Provenance is explicitly verified.
- Meaning, licence/usage rights, and source reference are present.
- The release manifest passes every integrity check and sets `releaseable=true`.

The JSON values are checked strictly. A string such as `"true"` does not substitute for Boolean `true`, and a string such as `"0"` does not substitute for integer `0`.

## Immutable output and evidence layout

For `Change.mp4`, the stable deliverables and reports are:

```text
output/CHANGE/Change.glb
output/CHANGE/Change.pose.npz
output/CHANGE/Change.motion.npz
output/CHANGE/Change.qc.json
output/CHANGE/Change.metadata.json
output/CHANGE/Change.glb_validation.json
output/CHANGE/Change.khronos_validation.json
output/CHANGE/Change.source_avatar_validation.json
output/CHANGE/Change.signer_review.json
output/CHANGE/Change.release.json
output/CHANGE/debug/Change_avatar_preview.mp4
output/CHANGE/debug/Change_source_avatar_comparison.mp4
```

The immutable run directory contains the candidate GLB, run-specific previews, and `evidence/` snapshots of:

- source video and avatar FBX;
- raw pose and final motion NPZ;
- QC, Blender validation, Khronos validation, timing comparison, and signer-review JSON;
- source/avatar comparison MP4;
- avatar profile, bone map, and neutral-hand calibration; and
- both the stable GLB record and immutable run GLB record.

Each evidence record stores path, size, and SHA-256. The release manifest verifies the valid/matching run ID, immutable run path, stable alias, sizes, GLB hashes, validator hashes/results, and the complete evidence set before an asset can be releaseable.

## Verified result for `Change.mp4`

The current clean run is `b27e657442b94954835eb8bc51face7e`.

| Result | Verified value |
|---|---|
| Stable GLB | `output/CHANGE/Change.glb` |
| Immutable GLB | `output/CHANGE/runs/b27e657442b94954835eb8bc51face7e/Change.glb` |
| GLB SHA-256 | `35b41cc887384d2814f8c0bf6a2c30fe77f8415a2832644b08255aba4e9f9348` |
| GLB size | 58,589,444 bytes |
| Technical QC | `PASS` |
| Production status | `ENGINEERING_CANDIDATE` |
| Engineering candidate | `true` |
| Production eligible / manifest releaseable | `false` / `false` |
| Timing | 91 source and avatar frames, 25 FPS, 3.60-second animation timeline |
| Contents | 8 meshes, 1 armature, exactly 1 animation |
| Blender | 4.5.11 LTS |
| Khronos validator | `PASS`; 0 errors, 0 warnings, 0 infos, 0 hints |
| Automated regression suite | 127 tests passed |
| Tracking | Body 100%; left hand 57.14%; right hand 56.04%; longest missing-hand run 0.84 seconds; 0 left/right swap events |
| Neutral calibration | `PASS`; 15 mapped finger bones per hand; avatar-derived wrist roll left -60 degrees / right +60 degrees |
| Collision proxy | `PASS`; 91/91 evaluated, coverage 1.0, 0 review frames, 0 fail frames |
| Minimum normalized proxy clearance | 0.0795217 |
| Opening hands | `PASS` at frame 1; neutral weights `[1.0, 1.0]`; left/right medial alignment 0.62737 / 0.80903 |
| Ending hands | `PASS` at frame 91; neutral weights `[1.0, 1.0]`; left/right medial alignment 0.73917 / 0.80796 |
| Finger retargeting | `PASS`; mean direction error 2.3008 degrees, p95 10.1468 degrees |
| Palm adherence | `PASS`; mean error 0.00366 degrees, p95 0.01185 degrees, max 0.01360 degrees |
| Release-manifest integrity checks | All `true`; no integrity reasons |

The generated avatar contact sheet at frames 1, 10, 25, 50, 72, and 91 shows relaxed medial opening/ending hands and a coherent signing trajectory. This is a diagnostic spot check, not the required qualified full-clip collision and linguistic approval.

The exact remaining release blockers are:

1. Qualified signer verdict is not `PASS`.
2. `isl_verified` is not `true`.
3. Reviewer identity is missing.
4. A valid timezone-aware `reviewed_at` is missing.
5. A trusted Ed25519 signer signature has not been verified.
6. Independent full-clip collision review is not `PASS`.
7. Source provenance is not explicitly verified.
8. Verified meaning is missing.
9. Licence or usage-rights statement is missing.
10. Traceable source reference is missing.

No code should set these human/governance fields automatically.

## What is still required for a production service

The conversion core now has fail-closed machine gates, clean Blender invocation, atomic publication, per-output locking, immutable evidence, content-addressed avatar calibration, official glTF validation, and cryptographically trusted approval. Operating it as a reliable production service additionally requires:

- A reproducible worker/container image with fully locked transitive Python/Node dependencies and recorded tool/model hashes.
- CI that runs the full test suite plus representative end-to-end regression clips before deployment.
- A versioned regression corpus covering different bodies, clothing, skin tones, camera distances, lighting, left/right dominance, occlusions, rapid signing, body contact, and two-hand contact.
- Service orchestration: queueing, job isolation, CPU/GPU and Blender resource limits, timeouts, bounded retries, idempotency, storage lifecycle, and recovery drills.
- Monitoring and alerts for failure rate, tracking coverage, collision review rate, processing latency, validator versions, output sizes, and signature/release failures.
- Secure reviewer-key provisioning, rotation, revocation, least-privilege access, and immutable audit logging. Private keys must remain in a managed signing environment.
- Documented reviewer qualification, double-review/escalation rules, sampling policy, and release accountability.
- Dataset consent, privacy/retention controls, licence enforcement, provenance capture, and passenger-safety/product acceptance criteria.
- A mesh-aware collision solution or continued mandatory full-clip visual review; the current landmark proxy alone is insufficient.
- Load, soak, crash-recovery, storage-integrity, and target-player compatibility testing.

Until these operational controls and the outstanding human gates are in place, describe `Change.glb` as a **machine-validated engineering candidate**, not an approved production asset.

## How to compare this pipeline with a friend's pipeline

Run both pipelines from the same archived source (or from the exact same normalized file) and record every version and command. Compare:

| Dimension | Required evidence |
|---|---|
| Input identity | Original and effective-input SHA-256, codec, resolution, FPS, frame count, timestamps, rotation |
| Media preprocessing | FFmpeg version and exact filter/codec command; whether frames were dropped or duplicated |
| Tracking | Body/hand availability, missing-hand runs, swap events, non-finite values |
| Timing | Source/avatar frame count, FPS, duration, and per-frame alignment |
| Body motion | Wrist endpoint error and visual trajectory |
| Hands | Opening/ending orientation, palm error, finger retarget error, continuity, visible flips |
| Collision | Full-frame proxy coverage plus independent full-clip visual or mesh-aware result |
| GLB | Mesh/armature/animation counts, file size, Blender re-import, official Khronos counts |
| Traceability | Run ID, hashes, immutable inputs/reports/calibration, release manifest |
| Meaning and rights | Qualified ISL verdict, verified meaning, provenance, licence, source reference |
| Operations | Repeatability, processing time, failure recovery, concurrency safety, monitoring, cost |

Do not select a pipeline because it merely produces a `.glb` file or because it uses FFmpeg. Select it only when it preserves timing and meaning, passes objective technical checks, provides traceable immutable evidence, and passes independent qualified review.
