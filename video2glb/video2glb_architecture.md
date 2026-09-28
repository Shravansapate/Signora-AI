# video2glb — Complete Architecture & Workflow Guide

> **Project:** ISL Video to GLB Converter
> **Stack:** Python 3.12 · MediaPipe · Blender 4.5 (headless) · NumPy · OpenCV · Node.js (glTF validator)
> **Platform:** Windows (PowerShell) — local, offline pipeline

---

## Table of Contents

1. [What the Pipeline Does](#1-what-the-pipeline-does)
2. [Repository Layout](#2-repository-layout)
3. [Technology Stack & Dependencies](#3-technology-stack--dependencies)
4. [Configuration System](#4-configuration-system)
5. [High-Level Architecture Diagram](#5-high-level-architecture-diagram)
6. [End-to-End Workflow — All 18 Stages](#6-end-to-end-workflow--all-18-stages)
7. [Module Deep-Dives](#7-module-deep-dives)
   - 7.1 [Video Inspection (`src/video`)](#71-video-inspection-srcvideo)
   - 7.2 [Pose Tracking (`src/tracking`)](#72-pose-tracking-srctracking)
   - 7.3 [Motion Solving (`src/motion`)](#73-motion-solving-srcmotion)
   - 7.4 [Avatar System (`src/avatar`)](#74-avatar-system-srcavatar)
   - 7.5 [Blender Integration (`src/blender`)](#75-blender-integration-srcblender)
   - 7.6 [Quality Control (`src/qc`)](#76-quality-control-srcqc)
   - 7.7 [Metadata & Provenance (`src/metadata`)](#77-metadata--provenance-srcmetadata)
   - 7.8 [Pipeline Infrastructure (`src/pipeline`)](#78-pipeline-infrastructure-srcpipeline)
8. [Batch Processing](#8-batch-processing)
9. [Stage Caching & Resumability](#9-stage-caching--resumability)
10. [Output Artifacts Explained](#10-output-artifacts-explained)
11. [QC & Production Gate System](#11-qc--production-gate-system)
12. [Signer Review & Release Workflow](#12-signer-review--release-workflow)
13. [Data Flow Diagram — Per-Video Run](#13-data-flow-diagram--per-video-run)
14. [Error Handling & Failure Quarantine](#14-error-handling--failure-quarantine)
15. [Key Design Decisions](#15-key-design-decisions)

---

## 1. What the Pipeline Does

`video2glb` converts a source **Indian Sign Language (ISL) video** (MP4) into a **production-ready animated 3D GLB file** that drives a rigged avatar character.

The pipeline is **fully local** — no cloud services, no BVH export tools, no Rokoko Studio. It:

1. **Inspects** the source video and normalises its frame rate.
2. **Tracks** body, hand, and face landmarks using MediaPipe Holistic (in plain Python).
3. **Solves** a rigged skeleton from raw 2D/3D landmarks using custom inverse kinematics, quaternion math, and smoothing algorithms.
4. **Applies** the solved animation onto a custom avatar FBX by launching Blender headlessly.
5. **Exports** the animated avatar to a binary glTF 2.0 file (`.glb`).
6. **Validates** the GLB file in a fresh Blender process + via the official Khronos glTF validator.
7. **Generates** review artefacts — pose overlay video, avatar preview video, and a side-by-side source/avatar comparison.
8. **Gates** every output through deterministic engineering QC and a mandatory signer review workflow before production release.

---

## 2. Repository Layout

```
video2glb/
├── convert.py                  # ← MAIN ENTRY POINT (4 290 lines, 18-stage pipeline)
├── calibrate_avatar.py         # One-off avatar calibration helper
├── validate_match.py           # Standalone comparison validator
│
├── config/
│   ├── settings.yaml           # All tunable pipeline settings
│   ├── qc_thresholds.yaml      # Numeric pass/fail thresholds for QC checks
│   ├── avatar_bone_map.json    # Canonical ↔ avatar bone name translation
│   ├── avatar_profile.json     # Avatar rest-pose bone data (auto-generated)
│   ├── neutral_hand_pose.json  # Per-avatar neutral hand joint rotations
│   └── trusted_signers.json    # Signer public-key registry
│
├── src/
│   ├── video/                  # Video inspection and frame-rate normalisation
│   ├── tracking/               # MediaPipe holistic landmark extraction
│   ├── motion/                 # Skeleton solving, IK, smoothing, quaternions
│   ├── avatar/                 # Bone-mapping and avatar profile loading
│   ├── blender/                # All Blender subprocess scripts
│   ├── qc/                     # Quality control checks and production gate
│   ├── metadata/               # Provenance, SHA-256 binding, release metadata
│   └── pipeline/               # Batch runner, stage cache, run recorder
│
├── input/                      # Source MP4 videos (batch_2/, batch_3/, …)
├── output/                     # One subfolder per video (e.g., output/TRAIN/)
├── assets/
│   └── character.fbx           # The rigged avatar character
├── models/
│   └── holistic_landmarker.task # MediaPipe model weights
├── temp/                       # Stage cache storage
├── failed/                     # Quarantined failed runs with full evidence
├── logs/                       # Per-video conversion logs (batch mode)
│
├── run_batch2.ps1 … run_batch7.ps1  # PowerShell wrappers for batch folders
├── requirements.txt            # Python dependencies (pinned)
├── package.json                # Node.js (Khronos glTF validator)
└── docs/
    └── PRODUCTION_VIDEO_TO_GLB_PIPELINE.md
```

---

## 3. Technology Stack & Dependencies

| Layer | Tool/Library | Version | Role |
|---|---|---|---|
| Python runtime | CPython | 3.12 | Entire pipeline logic |
| Pose tracking | `mediapipe` | 0.10.35 | HolisticLandmarker (body + hands) |
| Matrix math | `numpy` | 2.2.6 | All landmark/quaternion arithmetic |
| Video I/O | `opencv-contrib-python` | 4.11.0.86 | Frame decoding, overlay writing |
| 3D authoring | Blender | 4.5 | Avatar import, IK solving, GLB export, re-import validation |
| Config parsing | `PyYAML` | 6.0.2 | `settings.yaml` / `qc_thresholds.yaml` |
| Crypto | `cryptography` | 50.0.0 | Signer ECDSA signature verification |
| glTF validation | Khronos validator (npm) | 2.0.0-dev.3.10 | Official glTF 2.0 spec compliance |
| Shell wrapper | PowerShell | — | Batch launch scripts |
| Video probing | `ffprobe` | system | Duration/FPS metadata inspection |

---

## 4. Configuration System

All pipeline behaviour is driven by **`config/settings.yaml`**. No code changes are needed to adapt to a new video.

```yaml
avatar:
  path: "./assets/character.fbx"        # FBX avatar used for all signs

blender:
  executable: "C:/…/blender.exe"        # Full path to Blender 4.5

pose:
  backend: "mediapipe_holistic"
  model_path: "./models/holistic_landmarker.task"
  min_face_detection_confidence: 0.5    # MediaPipe confidence gates
  min_pose_detection_confidence: 0.5
  min_pose_landmarks_confidence: 0.5
  min_hand_landmarks_confidence: 0.5

processing:
  preserve_source_fps: true             # Never re-encode at a fixed FPS
  mirror_input: false
  auto_trim: false
  smoothing: true                       # Temporal smoothing on all landmarks

tracking:
  body: true
  hands: true
  face: false                           # Face tracking currently disabled

output:
  save_pose: true                       # Save .pose.npz raw landmark archive
  save_motion: true                     # Save .motion.npz solved motion archive
  save_debug: true                      # Save pose-overlay and preview videos
  save_intermediates: false             # Body-only / palms-only intermediate GLBs
  uppercase_output_dir: true            # output/TRAIN/ not output/Train/
```

**`config/qc_thresholds.yaml`** holds numeric pass/fail thresholds (e.g., minimum hand visibility frames, maximum wrist position error) that are loaded separately and hashed at start-up so no silent threshold drift is possible.

---

## 5. High-Level Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  INPUT LAYER                                                        │
│  ┌──────────────┐   ┌───────────────┐   ┌────────────────────────┐ │
│  │  Source MP4  │   │ character.fbx │   │  settings.yaml /       │ │
│  │  (ISL video) │   │  (avatar rig) │   │  qc_thresholds.yaml    │ │
│  └──────┬───────┘   └───────┬───────┘   └──────────┬─────────────┘ │
└─────────┼───────────────────┼──────────────────────┼───────────────┘
          │                   │                      │
          ▼                   ▼                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PYTHON PIPELINE  (convert.py)                                      │
│                                                                     │
│  Stage 1: Inspect/Prepare Video     (src/video/inspector.py)        │
│      └─► Normalise FPS · Validate frame count · Compute timestamps  │
│                                                                     │
│  Stage 2: Extract Holistic Landmarks (src/tracking/)                │
│      └─► MediaPipe HolisticLandmarker → .pose.npz                   │
│           Pose (33 kpts) · Left hand (21 kpts) · Right hand (21 kpts)│
│                                                                     │
│  Stage 3: Verify Tracked Timeline                                   │
│  Stage 4: Validate Tracking Quality  (src/tracking/tracking_qc.py)  │
│                                                                     │
│  Stage 5: Calibrate Avatar + Neutral Hands                          │
│      └─► Blender headless: blender_calibrate_avatar.py             │
│           → avatar_profile.json · neutral_hand_pose.json            │
│                                                                     │
│  Stage 10: Solve Full Skeleton Motion (src/motion/)                 │
│      └─► Coordinate transform · Arm IK · Palm orientation          │
│           Finger curl solving · Quaternion smoothing → .motion.npz  │
│                                                                     │
│  Stage 11: Export GLB               (src/blender/)                  │
│      └─► Blender headless: blender_apply_motion.py                 │
│           Import FBX · Apply rotations · IK solve · Export .glb    │
│                                                                     │
│  Stage 12: Re-import Validate GLB   (src/blender/blender_validate_glb.py)│
│  Stage 13: Khronos glTF 2.0 Validate (npm run validate)             │
│                                                                     │
│  Stage 14: Render Avatar Preview    (blender_render_animation.py)   │
│  Stage 15: Compare Source/Avatar    (src/qc/source_avatar_comparison)│
│  Stage 16: Publish Validated Artifacts                              │
│  Stage 17: Write Comparison/Review Reports                          │
│  Stage 18: Write Metadata + QC JSON                                 │
│                                                                     │
│  ── Production Gate (src/qc/production_gate.py) ──────────────────  │
│  Stage only releases if ALL engineering checks pass                  │
└─────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  OUTPUT LAYER  output/<GLOSS>/                                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌───────────────────────┐  │
│  │ Sign.glb │ │ Sign.qc  │ │ Sign.meta│ │ debug/ (preview vids) │  │
│  └──────────┘ └──────────┘ └──────────┘ └───────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 6. End-to-End Workflow — All 18 Stages

Each stage is wrapped in a `RunRecorder.execute()` call that writes live status to `runs/<run_id>/execution.json`. Computationally expensive stages (tracking, motion solving, GLB export, preview rendering) are additionally wrapped in `StageCache.execute()` to skip re-computation when inputs have not changed.

### Stage 1 — Inspect / Prepare Video Timing
**File:** `src/video/inspector.py` → `inspect_video()` + `prepare_video()`

- Probes the source MP4 with `ffprobe` to determine its true FPS, frame count, width, height, and whether the video has a variable frame rate (VFR).
- If VFR is detected, FFmpeg re-encodes to a constant-frame-rate (CFR) copy in `runs/<run_id>/working/`. The original source is **never modified**.
- Computes the exact millisecond timestamp array for every frame; this array drives MediaPipe in VIDEO mode (timestamps must be strictly increasing).
- Saves `preparation.json` with source and working video metadata.

> **Why this matters:** MediaPipe's VIDEO running mode requires strictly increasing timestamps. A VFR video or incorrect timestamps causes subtle landmark drift. The pipeline fails hard rather than producing silently wrong poses.

---

### Stage 2 — Extract Holistic Landmarks
**File:** `src/tracking/holistic_tracker.py` → `MediaPipeHolisticBackend`

- Opens the (CFR) working video via OpenCV `VideoCapture`.
- Instantiates MediaPipe `HolisticLandmarker` in VIDEO mode with the `.task` model file.
- Processes each frame and collects:
  - `pose_image` (33 landmarks × 4 values: x, y, z, visibility) — normalised image coordinates
  - `pose_world` (33 landmarks) — metric world coordinates from MediaPipe's monocular depth estimate
  - `left_hand_image` / `right_hand_image` (21 × 3 each) — image-space hand landmarks
  - `left_hand_world` / `right_hand_world` — world-space hand landmarks
- Optionally writes a **pose overlay debug video** with coloured skeleton/hand wireframes drawn on each frame.
- Performs `assess_hand_assignment()` quality check per frame (verifies L/R hands are consistent with body pose).
- Saves the full sequence as **`<GLOSS>.pose.npz`** — a compressed NumPy archive.

> **Key landmark indices:** Pose: shoulders (11, 12), elbows (13, 14), wrists (15, 16), hips (23, 24). Hands: wrist (0), finger tips (4, 8, 12, 16, 20).

---

### Stage 3 — Verify Tracked Timeline
Asserts that the extracted `PoseSequence` has the same frame count and FPS as the inspected video. Fails immediately if any mismatch is found — the conversion should never proceed with misaligned data.

---

### Stage 4 — Validate Tracking Quality
**File:** `src/tracking/tracking_qc.py` → `evaluate_tracking()`

- Checks what percentage of frames have valid pose / left-hand / right-hand landmarks.
- Compares against thresholds in `config/qc_thresholds.yaml`.
- Sets `technical_qc = "PASS"` or `"FAIL"` in the QC record.
- A failure here **does not abort** the pipeline — all later stages still run and produce a candidate GLB, but the QC JSON flags the result as failed.

---

### Stage 5 — Calibrate Avatar + Neutral Hands
**Files:** `src/blender/blender_calibrate_avatar.py`, `src/motion/neutral_hand.py`

This stage runs **once per avatar FBX** (result is cached to `config/avatar_calibrations/<sha256>/`):

1. Launches Blender headlessly to import `character.fbx`.
2. Reads each pose bone's rest-pose matrix and bone length from the armature.
3. Saves **`avatar_profile.json`** — the complete rest-pose skeleton data used by the skeleton solver.
4. Saves **`avatar_bone_map.json`** — translation table from canonical bone names (`LeftUpperArm`, `RightForeArm`, etc.) to the actual names in the FBX armature.
5. Calls `ensure_neutral_hand_pose()` to compute per-finger neutral joint rotations from the avatar's rest pose and saves **`neutral_hand_pose.json`**.

> The calibration is SHA-256-bound to the FBX file. If the avatar FBX changes, calibration automatically re-runs.

---

### Stage 10 — Solve Full Skeleton Motion
**File:** `src/motion/skeleton_solver.py` → `solve_motion_from_pose()`

This is the heart of the pipeline. It converts 2D/3D landmark sequences into a stream of quaternion bone rotations ready for Blender.

**Sub-steps inside the solver:**

| Sub-step | What happens |
|---|---|
| **Coordinate transform** | `mediapipe_image_to_canonical()` — flips Y axis, scales to metric, removes camera-space offsets |
| **Gap interpolation** | Fills short tracking dropouts (≤ 25% of 1 s) with linear interpolation — prevents snapping |
| **Temporal smoothing** | Centred moving-average with FPS-relative radius on body (8% of 1 s) and hands (4% of 1 s) |
| **Finger curl computation** | Angles between successive finger joints → per-joint curl scalars |
| **Finger direction conditioning** | Derives 3D finger direction vectors from world-space hand landmarks; applies SO(3) continuity enforcement |
| **Arm IK (upper arm / forearm)** | `arm_ik.py` — builds rotation from shoulder-to-elbow and elbow-to-wrist vectors; respects avatar bone lengths |
| **Depth retargeting** | `depth_retargeting.py` — adjusts monocular depth estimates to match avatar's physical limb reach |
| **Palm orientation** | Palm coordinate frame (normal, radial, distal) derived from 4 metacarpal landmarks |
| **Neutral hand blending** | Frames where hands are not visible smoothly blend finger/wrist rotations toward neutral pose |
| **Quaternion continuity** | `enforce_quaternion_continuity()` — flips sign of quaternions where interpolation would cross the 4D sphere boundary |
| **Output assembly** | Packed into `rotations` (F × B × 4), `ik_targets` (F × 2 × 3), `finger_directions` (F × 2 × 5 × 3), `palm_basis` (F × 2 × 3 × 3) → saved as **`.motion.npz`** |

> **Stages 6–9** (optional, controlled by `save_intermediates: true`) run partial solvers to export body-only and palms-only intermediate GLBs for diagnostic comparison.

---

### Stage 11 — Launch Blender / Export GLB
**File:** `src/blender/blender_apply_motion.py`

Blender is launched as a **headless subprocess** (`blender --background --python blender_apply_motion.py`). The script:

1. Resets the scene and imports `character.fbx` via `import_avatar()`.
2. Normalises skin weights (`normalize_export_skin_weights()`) to ensure clean deformation.
3. Suspends mesh modifier evaluation during rig solving (performance optimisation).
4. Reads the `.motion.npz` archive — frame count, FPS, bone names, quaternions, IK targets, finger directions, palm basis.
5. Sets Blender scene FPS from the source video FPS.
6. Creates a new Blender `Action` and iterates every frame:
   - Applies bone quaternion rotations from the solved motion.
   - If IK is enabled: updates IK target Empty objects and lets Blender solve the IK chain.
   - If palm tracking is enabled: applies palm orientation constraints.
   - If finger tracking is enabled: applies finger direction constraints with continuity checking.
   - Applies neutral-hand blending for frames with low hand confidence.
   - Inserts keyframes for every animated bone.
7. Restores mesh modifier states and exports the animated scene to **`.glb`** (binary glTF 2.0).
8. Writes **`<GLOSS>.ik_calibration.json`** with arm IK pole angles and skin weight preparation report.

> Blender runs in a **completely isolated subprocess** — it cannot corrupt the main Python process if it crashes.

---

### Stage 12 — Re-import GLB Validation
**File:** `src/blender/blender_validate_glb.py`

A **second, completely fresh Blender process** is launched to re-import the just-exported GLB. This catches any corruption that could occur during export. It checks:

- Armature and mesh objects are both present.
- Exactly **one** animation action exists.
- Animation timing matches expected frame count and FPS.
- Wrist target positions from the GLB match IK targets within tolerance.
- Hands are visible (not clipped into the body) in a majority of frames.
- Local finger bone rotation continuity — no sudden joint flips between frames.
- Source-to-avatar finger direction agreement (local coordinate frame comparison).

> **Critical:** If re-import validation fails, the candidate GLB is **quarantined** to `failed/` — it is never promoted to `output/<GLOSS>/`.

---

### Stage 13 — Khronos glTF 2.0 Compliance Validation
**File:** `src/qc/gltf_compliance.py`

Runs the official **Khronos glTF Validator** (installed via `npm ci` from `package.json`) as a Node.js subprocess against the candidate GLB. The JSON validation report is classified:

- Zero `numErrors` = PASS.
- Any errors = FAIL (blocks promotion).
- Warnings are checked against a per-project allowlist in `settings.yaml`.

The validator version (`2.0.0-dev.3.10`) is pinned and recorded in every validation report.

---

### Stage 14 — Render Avatar Preview
**File:** `src/blender/blender_render_animation.py`

Launches Blender to render an **MP4 preview video** of the animated avatar using a high-contrast diagnostic material (not the production texture). This video is used for visual review — it does **not** alter the exported GLB.

---

### Stage 15 — Compare Source / Avatar
**File:** `src/qc/source_avatar_comparison.py` → `create_source_avatar_comparison()`

Creates a **side-by-side MP4** placing the source ISL video (left) alongside the avatar preview (right), with timing normalised so the sign is in sync. This is the primary artefact for signer review.

Validation checks:
- Both clips have the same frame count after timing normalisation.
- Timeline error is within acceptable bounds.

SHA-256 hashes of the source video, working video, GLB, and comparison video are recorded in `source_avatar_validation.json` for provenance binding.

---

### Stage 16 — Publish Validated Artifacts
Only if both GLB validation (stage 12/13) and source/avatar comparison (stage 15) pass:

- Atomically moves the candidate GLB from `runs/<run_id>/` to `output/<GLOSS>/<GLOSS>.glb`.
- Copies avatar preview and comparison videos to `output/<GLOSS>/debug/`.

> **Atomic copy:** Uses a write-to-temp + `os.replace()` pattern — if the process is killed mid-copy, no partial file is left in the output directory.

---

### Stage 17 — Write Comparison / Review Reports
Writes:
- **`<GLOSS>.source_avatar_validation.json`** — comparison results + hashes.
- **`<GLOSS>.match_validation.json`** — timeline alignment details.
- **`<GLOSS>.signer_review.json`** — initial review record with `isl_verified: false`, `production_status: PENDING_SIGNER_REVIEW`.

---

### Stage 18 — Write Metadata / QC
Writes:
- **`<GLOSS>.metadata.json`** — complete provenance: source video hash, avatar hash, all tool versions, all intermediate file hashes, run ID, timestamps, execution stage log.
- **`<GLOSS>.qc.json`** — flat summary of every QC check result.

At this point the pipeline completes successfully with exit code `0`.

---

## 7. Module Deep-Dives

### 7.1 Video Inspection (`src/video`)

| File | Responsibility |
|---|---|
| `inspector.py` | `inspect_video()` — probes metadata; `prepare_video()` — normalises to CFR if needed; `VideoInfo` dataclass |
| `reader.py` | Low-level frame-by-frame OpenCV reader with bounds checking |

**Key class:** `VideoInfo` holds `fps`, `frame_count`, `width`, `height`, `variable_frame_rate`, `timestamps_ms[]`.

---

### 7.2 Pose Tracking (`src/tracking`)

| File | Responsibility |
|---|---|
| `holistic_tracker.py` | `PoseBackend` abstract class; `MediaPipeHolisticBackend` implementation; overlay drawing |
| `pose_schema.py` | `PoseFrame` / `PoseSequence` dataclasses; `.save_npz()` / `.load_npz()` |
| `tracking_qc.py` | `assess_hand_assignment()` per frame; `evaluate_tracking()` over whole sequence |

**`PoseSequence.save_npz()` saves these arrays:**

```
fps, width, height
pose_image     [F × 33 × 4]   image-space (x, y, z, visibility)
pose_world     [F × 33 × 4]   metric world
left_hand_image  [F × 21 × 3]  (NaN where not tracked)
right_hand_image [F × 21 × 3]
left_hand_world  [F × 21 × 3]
right_hand_world [F × 21 × 3]
```

---

### 7.3 Motion Solving (`src/motion`)

| File | Responsibility |
|---|---|
| `skeleton_solver.py` | Master solver — orchestrates all sub-solvers; saves `.motion.npz` |
| `coordinate_system.py` | `mediapipe_image_to_canonical()` / `mediapipe_world_to_canonical()` — axis flip and scale |
| `arm_ik.py` | `ArmIKSolver` — upper arm and forearm quaternion from 3D joint positions |
| `depth_retargeting.py` | Retargets Z depth of wrist/elbow to avatar limb reach |
| `quaternion_utils.py` | `quaternion_slerp`, `quaternion_from_matrix`, `quaternion_from_vectors`, continuity enforcement |
| `smoothing.py` | `smooth_landmarks_centered()` — centred moving average respecting NaN gaps |
| `interpolation.py` | `interpolate_short_gaps()` — linear fill of NaN dropouts up to `max_gap` frames |
| `neutral_hand.py` | `neutral_rotations_by_canonical()` — builds neutral quaternions per finger; `apply_boundary_neutral_pose()` — blends toward neutral at tracking boundaries |
| `arm_solver.py` / `torso_solver.py` / `finger_solver.py` / `palm_solver.py` | Thin delegation stubs (reserved for future decomposition) |

**`.motion.npz` arrays saved:**

```
fps, frame_count, action_name, gloss
bone_names              [B]       canonical names
avatar_bone_names       [B]       actual FBX bone names
rotations               [F × B × 4]  quaternion per bone per frame
root_translation        [F × 3]   root bone world position
ik_targets              [F × 2 × 3]  wrist IK target positions (L, R)
finger_directions       [F × 2 × 5 × 3]  finger direction vectors
finger_direction_constraint_valid [F × 2 × 5]
finger_direction_influence        [F × 2 × 5]
palm_basis              [F × 2 × 3 × 3]  palm coordinate frames
palm_basis_valid        [F × 2]
palm_basis_influence    [F × 2]
neutral_wrist_weights   [F × 2]   blend weight toward neutral wrist
neutral_finger_weights  [F × 2]   blend weight toward neutral fingers
neutral_finger_palm_rotations [2 × 5 × 3 × 4]  avatar-space neutral rotations
```

---

### 7.4 Avatar System (`src/avatar`)

| File | Responsibility |
|---|---|
| `bone_mapping.py` | `load_avatar_profile()` → `AvatarProfile`; `load_bone_map()` → canonical-to-avatar dict |

The **canonical bone name set** used throughout the pipeline:

```
Hips, Spine, Spine1, Spine2, Neck, Head
LeftShoulder, LeftUpperArm, LeftForeArm, LeftHand
RightShoulder, RightUpperArm, RightForeArm, RightHand
Left/Right + Thumb/Index/Middle/Ring/Little + 1/2/3
```

These are translated to the actual FBX bone names via `avatar_bone_map.json`.

---

### 7.5 Blender Integration (`src/blender`)

All Blender operations are **subprocess calls**. Blender is launched with:

```powershell
blender.exe --background --python <script.py> -- <args…>
```

| Script | Purpose |
|---|---|
| `blender_apply_motion.py` | Main animation application + GLB export (runs inside Blender Python) |
| `blender_calibrate_avatar.py` | Reads rest-pose bone data from FBX armature |
| `blender_validate_glb.py` | Re-imports exported GLB and runs all animation checks |
| `blender_render_animation.py` | Renders avatar preview MP4 |
| `blender_render_preview.py` | Quick single-frame preview helper |
| `blender_utils.py` | Shared helpers: `import_avatar()`, `export_avatar_glb()`, `reset_scene()`, `purge_orphans()`, `suspend_mesh_deformation()` |
| `blender_export.py` | Thin GLB export wrapper |
| `mesh_contact.py` | Mesh clearance / collision detection helpers used in GLB validation |

---

### 7.6 Quality Control (`src/qc`)

| File | Responsibility |
|---|---|
| `production_gate.py` | `evaluate_production_gate()` — deterministic go/no-go for release |
| `gltf_compliance.py` | Parses Khronos validator JSON output; `classify_gltf_validator_report()` |
| `source_avatar_comparison.py` | Side-by-side video generation; timing validation |
| `collision_safety.py` | Inter-mesh collision detection metrics |
| `mesh_clearance.py` | Clearance distance measurement between avatar meshes |
| `motion_stability.py` | Jitter and sudden-motion detection in bone rotations |
| `animation_channels.py` | Checks expected bone channels are present in the GLB |
| `root_drift.py` | Detects unintended root bone world translation |
| `neutral_shape.py` | Validates avatar returns to neutral at clip boundaries |
| `signer_approval.py` | ECDSA signature verification for signer sign-off |

---

### 7.7 Metadata & Provenance (`src/metadata`)

| File | Responsibility |
|---|---|
| `production_metadata.py` | `build_production_metadata()`, `sha256_file()`, `atomic_write_json()`, `runtime_versions()`, `utc_now_iso()`, `write_review_delivery()` |

Every output file's SHA-256 hash is recorded in `metadata.json`. The pipeline verifies hashes **before and after** every critical stage to detect file tampering or corruption.

---

### 7.8 Pipeline Infrastructure (`src/pipeline`)

| File | Responsibility |
|---|---|
| `batch_runner.py` | `discover_mp4_files()`, `run_isolated_jobs()`, `BatchJob`, `BatchProcessResult` |
| `stage_cache.py` | `StageCache` — content-addressable cache keyed on SHA-256 of all inputs; `RunRecorder` — live execution log |

---

## 8. Batch Processing

### How Batches Work

```
run_batch2.ps1
    └─► python convert.py --batch --input-dir ./input/batch_2 --batch-workers N
            └─► batch_runner.py → run_isolated_jobs()
                    ├─► Worker 1: python convert.py --video ./input/batch_2/Hello.mp4
                    ├─► Worker 2: python convert.py --video ./input/batch_2/Thank.mp4
                    └─► Worker N: …
```

- Each video is converted in a **separate subprocess** — one failure never kills the batch.
- Up to **8 parallel workers** (`MAX_BATCH_WORKERS = 8`), configurable via `-Workers` parameter.
- **Up to 3 retries** per video (`MAX_BATCH_RETRIES = 3`) for transient failures (memory exhaustion, timeouts).
- **24-hour timeout** per video (`MAX_BATCH_TIMEOUT_SECONDS = 86400`).
- Output goes to `output/<GLOSS_NAME>/` for each sign.
- Batch summary saved to `output/batch_summary.json`.

### Failure Categories (auto-classified from log tail)

| Category | Retryable | Cause |
|---|---|---|
| `TIMEOUT` | ✅ | Video took > 24h |
| `RESOURCE_EXHAUSTED` | ✅ | OOM / too many file handles |
| `CONTEXT_CHANGED` | ❌ | Code/config changed mid-batch |
| `DEPENDENCY_MISSING` | ❌ | Missing Python module or file |
| `INVALID_INPUT` | ❌ | Unreadable/corrupt MP4 |
| `TECHNICAL_QC` | ❌ | Tracking or GLB validation failed |
| `CONVERSION_ERROR` | ❌ | Unclassified pipeline failure |

### Process Isolation (Windows)
Blender and its child processes are managed via `taskkill /PID /T /F` on timeout or Ctrl-C, so the parent Python process is never left waiting for orphaned Blender instances.

---

## 9. Stage Caching & Resumability

`StageCache` (`src/pipeline/stage_cache.py`) implements a **content-addressable, dependency-fingerprinted cache** stored under `temp/stage_cache/<GLOSS>/`.

### Cache Key
```python
fingerprint = SHA256(JSON({
    "schema": 1,
    "inputs": {name: SHA256(file) for each input},
    "settings": {pipeline settings relevant to this stage}
}))
```

### What is cached

| Stage | Cache key includes |
|---|---|
| Tracking (stage 2) | Working video SHA-256 + model SHA-256 + source code SHA-256 + settings |
| Motion solving (stage 10) | Pose NPZ + avatar profile + bone map + neutral hand + source code |
| GLB export (stage 11) | Motion NPZ + avatar FBX + all Blender scripts + IK/finger/palm settings |
| Preview render (stage 14) | GLB SHA-256 + Blender path + FPS |

### Cache behaviour
- On cache **hit**: artifacts are copied back to the output directory; computation is skipped entirely.
- On cache **miss**: the stage runs, artifacts are SHA-256 verified, then stored in the cache.
- Validation and human-review stages are **never cached** — they always re-run.
- If inputs change mid-run (e.g., someone edits a file), the cache guard raises `RuntimeError` and aborts rather than silently using stale data.

---

## 10. Output Artifacts Explained

For a video named `Train.mp4`, output goes to `output/TRAIN/`:

```
output/TRAIN/
├── Train.glb                          ← Final production-ready animated GLB
├── Train.pose.npz                     ← Raw MediaPipe landmark data (all frames)
├── Train.motion.npz                   ← Solved skeleton quaternions (all frames)
├── Train.metadata.json                ← Complete provenance record
├── Train.qc.json                      ← Engineering QC pass/fail summary
├── Train.glb_validation.json          ← Re-import + Khronos validator results
├── Train.khronos_validation.json      ← Raw Khronos validator output
├── Train.ik_calibration.json          ← Arm IK pole calibration + skin weight report
├── Train.source_avatar_validation.json← Source-vs-avatar comparison metrics
├── Train.match_validation.json        ← Timeline alignment details
├── Train.signer_review.json           ← Review status (PENDING → APPROVED)
├── Train.release.json                 ← (Created after signer approval)
│
└── debug/
    ├── Train_pose_overlay.mp4         ← Source video with landmark wireframes
    ├── Train_avatar_preview.mp4       ← Rendered avatar animation
    └── Train_source_avatar_comparison.mp4  ← Side-by-side for signer review
```

---

## 11. QC & Production Gate System

### Two-Tier QC Model

```
TIER 1 — Engineering QC (automated)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
technical_qc_pass              Pose tracking quality above thresholds
glb_validation_pass            Re-import validation passed
khronos_validation_pass        Zero glTF spec errors
source_avatar_validation_pass  Comparison video generated successfully
neutral_hand_calibration_pass  Neutral pose calibration valid
collision_metric_pass          No mesh self-intersections detected
glb_sha256_valid               GLB file hash matches published hash
source_sha256_valid            Source video not modified after hashing

TIER 2 — Production QC (human)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
signer_verdict_pass            Qualified ISL signer approved the sign
isl_verified                   Sign meaning is linguistically correct
reviewer_present               Named reviewer recorded
reviewed_at_valid              Review timestamp present
signer_signature_verified      ECDSA cryptographic signature valid
review_source_hash_matches     Source video unchanged since review
review_glb_hash_matches        GLB unchanged since review
provenance_verified            Full hash chain verified
```

### Gate Logic
- `technical_qc: PASS` → file is **engineering-valid** but **not production-approved**.
- `production_status: APPROVED` → **all** engineering + signer checks passed. Safe for production use.
- Every output starts as `isl_verified: false` / `production_status: PENDING_SIGNER_REVIEW`.

---

## 12. Signer Review & Release Workflow

1. Conversion completes → `Train.signer_review.json` created with `PENDING_SIGNER_REVIEW`.
2. A qualified ISL signer watches `Train_source_avatar_comparison.mp4`.
3. Signer signs a review JSON document with their ECDSA private key.
4. Operator runs:
   ```powershell
   python convert.py --approve-existing --video .\input\Train.mp4 --signer-approval .\review\Train_signed.json
   ```
5. `verify_signer_approval()` checks:
   - Signer public key is in `config/trusted_signers.json`.
   - Cryptographic signature is valid.
   - Review was performed against the correct source video and GLB hashes.
6. If approved → `Train.release.json` is written and `production_status` is set to `APPROVED`.

---

## 13. Data Flow Diagram — Per-Video Run

```
Train.mp4 (source)
      │
      ▼  Stage 1
working/Train_cfr.mp4 ──────────────────────────────────────────┐
      │                                                         │
      ▼  Stage 2 (MediaPipe)                                    │
Train.pose.npz                                                  │
 [pose_image  F×33×4]                                           │
 [pose_world  F×33×4]                                           │
 [left_hand   F×21×3]  ──────────┐                              │
 [right_hand  F×21×3]            │                              │
                                  │                              │
      ▼  Stages 3,4 (QC)          │                              │
  tracking_qc result              │                              │
                                  │                              │
      ▼  Stage 5 (Blender cal)    │                              │
avatar_profile.json               │                              │
avatar_bone_map.json              │                              │
neutral_hand_pose.json            │                              │
                                  │                              │
      ▼  Stage 10 (Skeleton Solver)│                             │
Train.motion.npz ◄────────────────┘                             │
 [rotations  F×B×4]                                             │
 [ik_targets F×2×3]                                             │
 [finger_directions F×2×5×3]                                    │
                    │                                            │
                    ▼  Stage 11 (Blender headless)              │
              Train.glb  (candidate)                            │
                    │                                            │
                    ▼  Stage 12 (Blender re-import)             │
              glb_validation.json                               │
                    │                                            │
                    ▼  Stage 13 (Khronos validator)             │
              khronos_validation.json                           │
                    │                                            │
                    ▼  Stage 14 (Blender render)                │
              avatar_preview.mp4                                │
                    │                                            │
                    ▼  Stage 15 (compare) ◄─────────────────────┘
              source_avatar_comparison.mp4
              source_avatar_validation.json
                    │
                    ▼  Stage 16 (publish)
              output/TRAIN/Train.glb  ← final
                    │
                    ▼  Stages 17,18
              .signer_review.json
              .metadata.json
              .qc.json
```

---

## 14. Error Handling & Failure Quarantine

| Scenario | What Happens |
|---|---|
| GLB re-import fails (stage 12) | Candidate GLB is moved to `failed/<GLOSS>/<run_id>/` with all evidence files; error is raised |
| Tracking QC fails (stage 4) | Pipeline continues but sets `technical_qc: FAIL` in QC JSON |
| Blender subprocess crashes | Exit code captured, stderr written to log, `BatchProcessResult` records `CONVERSION_ERROR` |
| Input file changes mid-run | `guard_pipeline_context()` detects changed SHA-256 and raises `RuntimeError` |
| Keyboard interrupt | `cancellation.set()` signals all worker threads; active Blender processes killed via `taskkill /T /F` |
| Timeout (> 24h) | Process tree killed; job marked `TIMEOUT` and retried up to 3 times |

The `failed/` directory preserves:
- The quarantined candidate `.glb`
- All input files (symlinked / copied)
- All intermediate reports and logs

This allows post-mortem analysis without re-running the pipeline.

---

## 15. Key Design Decisions

| Decision | Rationale |
|---|---|
| **No BVH / Rokoko** | BVH export adds latency, requires external software licensing, and loses the left/right hand distinction. Direct MediaPipe → Blender is fully local and preserves semantic precision. |
| **MediaPipe VIDEO mode** (not IMAGE) | VIDEO mode uses temporal filtering across frames; image mode processes each frame independently and produces noisier landmarks. |
| **Image-space arm directions** (not world depth) | For front-facing ISL signing, 2D image-space directions match the visual signing space better than noisy monocular depth. |
| **Separate quaternion continuity pass** | Naïve quaternion interpolation can alias across the 4D sphere. The continuity pass flips quaternion signs so `slerp` always takes the short arc. |
| **Atomic writes everywhere** | All JSON and file copies use write-to-temp + `os.replace()` — no partial files if the process is killed. |
| **Blender launched as subprocess** | Blender embeds CPython but at a different version. Subprocess isolation means Blender crashes cannot corrupt the pipeline process. |
| **SHA-256 bound provenance** | Every artifact's hash is recorded in `metadata.json` before and after every stage. This makes the entire conversion reproducible and auditable. |
| **Two-tier QC (engineering + signer)** | Engineering QC can be fully automated. Linguistic correctness of ISL cannot — it requires a human expert reviewer whose sign-off is cryptographically recorded. |
| **Content-addressable stage cache** | Avoids re-running slow stages (MediaPipe: ~5–15 min; Blender export: ~2–8 min) when only downstream code changes. |
| **Per-video output isolation** | Each video's outputs live in their own directory. Batch workers run as completely separate processes — one corrupt video cannot affect others. |
