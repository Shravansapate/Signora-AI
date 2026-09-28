from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np

from src.avatar.bone_mapping import load_avatar_profile, load_bone_map
from src.motion.coordinate_system import mediapipe_image_to_canonical, mediapipe_world_to_canonical
from src.motion.interpolation import interpolate_short_gaps
from src.motion.quaternion_utils import (
    enforce_quaternion_continuity,
    normalize_quaternion,
    normalize_vector,
    limit_quaternion_angle,
    quaternion_from_axis_angle,
    quaternion_from_matrix,
    quaternion_slerp,
    quaternion_from_vectors,
    quaternion_to_matrix,
    validate_quaternions,
)
from src.motion.smoothing import smooth_landmarks_centered
from src.motion.depth_retargeting import retarget_arm_depth
from src.motion.neutral_hand import (
    apply_boundary_neutral_pose,
    neutral_rotations_by_canonical,
    neutral_wrist_rotations_by_canonical,
    neutral_finger_palm_rotations,
)


LEFT = {
    "shoulder": 11,
    "elbow": 13,
    "wrist": 15,
}
RIGHT = {
    "shoulder": 12,
    "elbow": 14,
    "wrist": 16,
}
HAND_CHAINS = {
    "Thumb": [1, 2, 3, 4],
    "Index": [5, 6, 7, 8],
    "Middle": [9, 10, 11, 12],
    "Ring": [13, 14, 15, 16],
    "Little": [17, 18, 19, 20],
}


@dataclass(frozen=True)
class MotionBuildResult:
    motion_path: Path
    metadata: dict


def solve_motion_from_pose(
    pose_path: str | Path,
    profile_path: str | Path,
    bone_map_path: str | Path,
    output_path: str | Path,
    gloss: str,
    body_only: bool = False,
    include_palms: bool = True,
    include_fingers: bool = True,
    smoothing: bool = True,
    neutral_hand_pose_path: str | Path | None = None,
) -> MotionBuildResult:
    data = np.load(pose_path)
    fps = float(data["fps"])
    width = float(data["width"])
    height = float(data["height"])
    aspect_ratio = width / max(height, 1.0)
    pose_world = data["pose_world"][:, :, :3].astype(np.float64)
    pose_image = data["pose_image"][:, :, :3].astype(np.float64)
    left_hand = data["left_hand_image"].astype(np.float64)
    right_hand = data["right_hand_image"].astype(np.float64)
    left_hand_world = data["left_hand_world"].astype(np.float64)
    right_hand_world = data["right_hand_world"].astype(np.float64)

    world_points = mediapipe_world_to_canonical(pose_world)
    image_points = mediapipe_image_to_canonical(pose_image, aspect_ratio=aspect_ratio)
    # For front-facing dictionary signs, image-space arm directions match the visual signing space
    # more reliably than monocular world-depth estimates.
    pose_points = image_points
    pose_points = interpolate_short_gaps(pose_points, max_gap=max(2, int(round(fps * 0.25))))
    # Palm orientation must be derived from genuinely observed landmarks.  A
    # Cartesian interpolation of the four palm points can shear or invert the
    # hand plane, so keep an untouched copy for the SO(3) conditioning path.
    left_hand_observed = mediapipe_image_to_canonical(left_hand, aspect_ratio=aspect_ratio)
    right_hand_observed = mediapipe_image_to_canonical(right_hand, aspect_ratio=aspect_ratio)
    left_points = interpolate_short_gaps(left_hand_observed, max_gap=max(2, int(round(fps * 0.20))))
    right_points = interpolate_short_gaps(right_hand_observed, max_gap=max(2, int(round(fps * 0.20))))
    if smoothing:
        pose_points = smooth_landmarks_centered(pose_points, radius=max(1, int(round(fps * 0.08))))
        left_points = smooth_landmarks_centered(left_points, radius=max(1, int(round(fps * 0.04))))
        right_points = smooth_landmarks_centered(right_points, radius=max(1, int(round(fps * 0.04))))
    left_finger_curls = _compute_finger_curls(left_points)
    right_finger_curls = _compute_finger_curls(right_points)
    if smoothing:
        # Finger bend is filtered separately from wrist/palm position so fast
        # handshapes remain responsive while landmark noise cannot flip joints.
        left_finger_curls = smooth_landmarks_centered(left_finger_curls, radius=max(1, int(round(fps * 0.04))))
        right_finger_curls = smooth_landmarks_centered(right_finger_curls, radius=max(1, int(round(fps * 0.04))))
    left_finger_curls = _hold_last_finger_curls(left_finger_curls)
    right_finger_curls = _hold_last_finger_curls(right_finger_curls)
    left_finger_curls = _limit_finger_curl_steps(left_finger_curls, max_delta=np.radians(35.0))
    right_finger_curls = _limit_finger_curl_steps(right_finger_curls, max_delta=np.radians(35.0))
    # Derive hand shape before any temporal mixing: averaging hand-world XYZ
    # from differently rotated palms shears the fingers and their local basis.
    raw_fingers = [_build_finger_directions(mediapipe_world_to_canonical(hand))
                   for hand in (left_hand_world, right_hand_world)]
    finger_direction_valid = np.stack([item[1] for item in raw_fingers], axis=1)
    finger_directions, finger_direction_usable, finger_conditioning = _condition_finger_directions(
        np.stack([item[0] for item in raw_fingers], axis=1),
        finger_direction_valid,
        max_gap=max(2, int(round(fps * 0.20))),
        smoothing_radius=max(1, int(round(fps * 0.04))) if smoothing else 0,
    )
    finger_directions = _limit_finger_direction_steps(finger_directions, finger_direction_usable, max_delta=np.radians(24.0))
    finger_directions, finger_direction_constraint_valid = _hold_finger_directions_for_release(
        finger_directions,
        finger_direction_usable,
        release_frames=max(4, int(round(fps * 0.18))),
    )
    finger_direction_influence = _ramp_finger_direction_activation(
        finger_direction_usable,
        finger_direction_constraint_valid,
        ramp_frames=max(4, int(round(fps * 0.18))),
    )
    raw_palm_bases, palm_basis_observed = _build_palm_bases(left_hand_observed, right_hand_observed)
    palm_bases, palm_basis_usable = _condition_palm_bases(
        raw_palm_bases,
        palm_basis_observed,
        max_gap=max(1, int(round(fps * 0.10))),
        smoothing_radius=max(1, int(round(fps * 0.08))) if smoothing else 0,
        max_step=np.radians(24.0),
    )
    palm_bases, palm_basis_valid = _hold_palm_bases_for_release(
        palm_bases,
        palm_basis_usable,
        release_frames=max(3, int(round(fps * 0.16))),
    )
    palm_basis_influence = _ramp_palm_basis_activation(
        palm_basis_usable,
        ramp_frames=max(3, int(round(fps * 0.16))),
        constraint_valid=palm_basis_valid,
    )

    profile = load_avatar_profile(profile_path)
    bone_map = load_bone_map(bone_map_path)
    rest = RestPose.from_profile(profile)
    bone_names = list(bone_map.keys())
    frame_count = int(data["frame_count"])
    rotations = np.tile(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64), (frame_count, len(bone_names), 1))

    for frame_index in range(frame_count):
        frame_pose = pose_points[frame_index]
        _solve_body_frame(rotations[frame_index], bone_names, bone_map, rest, frame_pose, body_only)
        if not body_only and include_palms:
            _solve_palm_frame(rotations[frame_index], bone_names, bone_map, rest, frame_pose, left_points[frame_index], "Left")
            _solve_palm_frame(rotations[frame_index], bone_names, bone_map, rest, frame_pose, right_points[frame_index], "Right")
        if not body_only and include_fingers:
            _solve_finger_frame(rotations[frame_index], bone_names, left_finger_curls[frame_index], "Left")
            _solve_finger_frame(rotations[frame_index], bone_names, right_finger_curls[frame_index], "Right")

    neutral_validation: dict = {"status": "NOT_APPLIED", "applied": False}
    neutral_palm_rotations = np.tile(np.array([1.0, 0.0, 0.0, 0.0]), (2, 5, 3, 1))
    left_neutral = np.zeros(frame_count, dtype=bool)
    right_neutral = np.zeros(frame_count, dtype=bool)
    if include_fingers and not body_only and neutral_hand_pose_path:
        neutral_payload = json.loads(Path(neutral_hand_pose_path).read_text(encoding="utf-8"))
        finger_neutral = neutral_rotations_by_canonical(neutral_payload)
        neutral_palm_rotations = neutral_finger_palm_rotations(neutral_payload, profile, bone_map)
        initial_neutral = apply_boundary_neutral_pose(
            rotations,
            bone_names,
            finger_neutral,
            neutral_wrist_rotations_by_canonical(neutral_payload),
            left_hand,
            right_hand,
            wrist_transition_frames=max(4, int(round(fps * 0.24))),
            finger_transition_frames=max(4, int(round(fps * 0.24))),
        )
        # Preserve observed opening/closing handshapes, including quiet signs.
        # Isolated "quiet/open" frames are not proof the sign has ended; forcing
        # them to neutral caused 0->1->0 weight jumps within active motion.
        # Only the explicit unobserved boundary fallback above uses neutral.
        wrist_weights = initial_neutral["wrist_weights"]
        finger_weights = initial_neutral["finger_weights"]
        palm_basis_influence *= 1.0 - wrist_weights
        # A fully neutral local pose must not be overwritten by a still-active
        # hand-world direction track (including held observations on exit).
        finger_direction_influence *= 1.0 - finger_weights[:, :, None, None]
        left_neutral = (wrist_weights[:, 0] > 0.0) | (finger_weights[:, 0] > 0.0)
        right_neutral = (wrist_weights[:, 1] > 0.0) | (finger_weights[:, 1] > 0.0)
        neutral_validation = {
            **neutral_payload.get("validation", {}),
            "schema_version": neutral_payload.get("schema_version"),
            "applied": bool(left_neutral.any() or right_neutral.any()),
            "applied_frames_left": int(left_neutral.sum()),
            "applied_frames_right": int(right_neutral.sum()),
            "ending_finger_rotation_keys_applied": 0,
            "initial_fallback": initial_neutral["details"],
            "signing_frames_preserved": int(frame_count * 2 - left_neutral.sum() - right_neutral.sum()),
        }
    else:
        wrist_weights = np.zeros((frame_count, 2), dtype=np.float64)
        finger_weights = np.zeros((frame_count, 2), dtype=np.float64)

    rotations = enforce_quaternion_continuity(rotations)
    validate_quaternions(rotations)
    segment_lengths = np.array([[float(rest.bone_info[bone_map[f"{side}{part}"]]["length"])
                                 for part in ("UpperArm", "ForeArm")] for side in ("Left", "Right")])
    raw_body = np.asarray(data["pose_world"], dtype=np.float64)
    visibility = raw_body[:, :, 3] if raw_body.shape[2] > 3 else np.ones(raw_body.shape[:2])
    depth_offsets, depth_observed, depth_report = retarget_arm_depth(
        world_points, visibility, segment_lengths, fps, smoothing=smoothing,
    )
    ik_diagnostics = {"source_depth": depth_report}
    ik_targets = _build_ik_targets(
        _canonical_to_image(pose_points, aspect_ratio),
        rest,
        bone_map,
        diagnostics=ik_diagnostics,
        depth_offsets=depth_offsets,
        left_hand_image=left_hand,
        right_hand_image=right_hand,
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    root_translation = np.zeros((frame_count, 3), dtype=np.float32)
    np.savez_compressed(
        output,
        fps=np.array(fps, dtype=np.float32),
        frame_count=np.array(frame_count, dtype=np.int32),
        bone_names=np.array(bone_names, dtype=np.str_),
        avatar_bone_names=np.array([bone_map[name] for name in bone_names], dtype=np.str_),
        ik_target_names=np.array(["LeftWrist", "LeftElbow", "RightWrist", "RightElbow"], dtype=np.str_),
        ik_targets=ik_targets.astype(np.float32),
        depth_observed=depth_observed.astype(np.uint8),
        ik_diagnostics_json=np.array(json.dumps(ik_diagnostics), dtype=np.str_),
        finger_direction_basis=np.array("PALM_ACROSS_FORWARD_NORMAL", dtype=np.str_),
        finger_direction_conditioning_json=np.array(json.dumps(finger_conditioning), dtype=np.str_),
        finger_directions=finger_directions.astype(np.float32),
        finger_direction_valid=finger_direction_valid.astype(np.uint8),
        finger_direction_usable=finger_direction_usable.astype(np.uint8),
        finger_direction_constraint_valid=finger_direction_constraint_valid.astype(np.uint8),
        finger_direction_influence=finger_direction_influence.astype(np.float32),
        palm_basis=palm_bases.astype(np.float32),
        palm_basis_observed=palm_basis_observed.astype(np.uint8),
        palm_basis_usable=palm_basis_usable.astype(np.uint8),
        palm_basis_valid=palm_basis_valid.astype(np.uint8),
        palm_basis_influence=palm_basis_influence.astype(np.float32),
        neutral_left_mask=left_neutral.astype(np.uint8),
        neutral_right_mask=right_neutral.astype(np.uint8),
        neutral_wrist_weights=wrist_weights.astype(np.float32),
        neutral_finger_weights=finger_weights.astype(np.float32),
        neutral_finger_palm_rotations=neutral_palm_rotations.astype(np.float32),
        root_translation=root_translation,
        rotations=rotations.astype(np.float32),
        quaternion_format=np.array("WXYZ", dtype=np.str_),
        action_name=np.array(gloss.upper(), dtype=np.str_),
        start_pose=rotations[0].astype(np.float32),
        end_pose=rotations[-1].astype(np.float32),
        solver_notes=np.array(
            [
                "V1 direct solver; avatar FBX bone lengths remain authoritative.",
                "Body and hand landmarks are stored and solved separately.",
                "Finger joints use locally applied, anatomically bounded curl rotations.",
                "Finger direction targets use local MediaPipe hand-world geometry, not global hand placement.",
                "Palm orientation uses observed image landmarks with SO(3) gap filling, robust centered smoothing, and a 24 degree/frame limit.",
            ],
            dtype=np.str_,
        ),
    )
    return MotionBuildResult(
        motion_path=output,
        metadata={
            "frame_count": frame_count,
            "fps": fps,
            "bone_count": len(bone_names),
            "body_only": body_only,
            "include_palms": include_palms,
            "include_fingers": include_fingers,
            "finger_direction_conditioning": finger_conditioning,
            "neutral_hand_validation": neutral_validation,
            "ik_diagnostics": ik_diagnostics,
        },
    )


def _canonical_to_image(points: np.ndarray, aspect_ratio: float = 1.0) -> np.ndarray:
    """Invert the image-to-canonical transform after interpolation/smoothing."""
    result = np.asarray(points, dtype=np.float64).copy()
    result[..., 0] = result[..., 0] / aspect_ratio + 0.5
    result[..., 1] = 0.5 - result[..., 1]
    result[..., 2] *= -1.0 / aspect_ratio
    return result


class RestPose:
    def __init__(self, bone_info: dict[str, dict]) -> None:
        self.bone_info = bone_info

    @classmethod
    def from_profile(cls, profile: dict) -> "RestPose":
        return cls({bone["name"]: bone for bone in profile["bones"]})

    def direction(self, bone_name: str) -> np.ndarray:
        bone = self.bone_info[bone_name]
        return normalize_vector(np.array(bone["tail_local"], dtype=np.float64) - np.array(bone["head_local"], dtype=np.float64))

    def rotation_matrix(self, bone_name: str) -> np.ndarray:
        matrix = np.array(self.bone_info[bone_name]["matrix_local"], dtype=np.float64)
        return matrix[:3, :3]


def _solve_body_frame(rotations, bone_names, bone_map, rest: RestPose, pose, body_only: bool) -> None:
    for side, indices in (("Left", LEFT), ("Right", RIGHT)):
        shoulder = pose[indices["shoulder"]]
        elbow = pose[indices["elbow"]]
        wrist = pose[indices["wrist"]]
        upper_vector = elbow - shoulder
        forearm_vector = wrist - elbow
        _set_swing(rotations, bone_names, bone_map, rest, f"{side}UpperArm", upper_vector)
        _set_swing(rotations, bone_names, bone_map, rest, f"{side}ForeArm", forearm_vector)
        if body_only:
            _set_identity(rotations, bone_names, f"{side}Hand")

    left_shoulder = pose[LEFT["shoulder"]]
    right_shoulder = pose[RIGHT["shoulder"]]
    left_hip = pose[23]
    right_hip = pose[24]
    if all(np.isfinite(v).all() for v in [left_shoulder, right_shoulder, left_hip, right_hip]):
        shoulder_axis = normalize_vector(right_shoulder - left_shoulder)
        hip_center = (left_hip + right_hip) * 0.5
        shoulder_center = (left_shoulder + right_shoulder) * 0.5
        up_axis = normalize_vector(shoulder_center - hip_center)
        forward_axis = normalize_vector(np.cross(shoulder_axis, up_axis))
        torso_twist = float(np.clip(forward_axis[0], -0.35, 0.35))
        _set_if_present(rotations, bone_names, "Chest", quaternion_from_axis_angle([0, 0, 1], torso_twist * 0.35))


def _solve_palm_frame(rotations, bone_names, bone_map, rest: RestPose, pose, hand, side: str) -> None:
    if not np.isfinite(hand[[0, 5, 9, 17], :3]).all():
        return
    wrist = hand[0]
    middle = hand[9]
    index = hand[5]
    pinky = hand[17]
    palm_forward = middle - wrist
    palm_horizontal = index - pinky if side == "Left" else pinky - index
    normal = normalize_vector(np.cross(palm_horizontal, palm_forward))
    target = normalize_vector(palm_forward + normal * 0.18)
    _set_swing(rotations, bone_names, bone_map, rest, f"{side}Hand", target, strength=0.45, max_angle=np.radians(35.0))


def _compute_finger_curls(hand_points: np.ndarray) -> np.ndarray:
    """Return non-negative MCP/PIP/DIP flexion angles for each tracked hand."""
    curls = np.full((hand_points.shape[0], len(HAND_CHAINS), 3), np.nan, dtype=np.float64)
    for frame_index, hand in enumerate(hand_points):
        for finger_index, (_, chain) in enumerate(HAND_CHAINS.items()):
            points = [0] + chain
            for joint_index in range(3):
                a, b, c = points[joint_index], points[joint_index + 1], points[joint_index + 2]
                if not np.isfinite(hand[[a, b, c], :3]).all():
                    continue
                parent = hand[a] - hand[b]
                child = hand[c] - hand[b]
                parent_length = np.linalg.norm(parent)
                child_length = np.linalg.norm(child)
                if parent_length < 1e-8 or child_length < 1e-8:
                    continue
                cosine = float(np.clip(np.dot(parent, child) / (parent_length * child_length), -1.0, 1.0))
                curls[frame_index, finger_index, joint_index] = np.pi - np.arccos(cosine)
    return curls


def _build_finger_directions(hand_points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return unit finger-segment directions in each frame's palm coordinate system.

    The hand-world landmarks are intentionally used only for *local hand shape*.
    A palm basis makes the directions independent of the hand's global position;
    Blender later attaches them to the tracked body wrist and avatar palm.
    """
    frame_count = hand_points.shape[0]
    directions = np.full((frame_count, len(HAND_CHAINS), 3, 3), np.nan, dtype=np.float64)
    valid = np.zeros((frame_count, len(HAND_CHAINS), 3), dtype=bool)

    for frame_index, hand in enumerate(hand_points):
        if not np.isfinite(hand[[0, 5, 9, 17], :3]).all():
            continue
        across = _unit_or_none(hand[5] - hand[17])
        forward = _unit_or_none(hand[9] - hand[0])
        if across is None or forward is None:
            continue
        normal = _unit_or_none(np.cross(across, forward))
        if normal is None:
            continue
        # Rebuild forward after the cross product to keep the basis orthogonal.
        forward = _unit_or_none(np.cross(normal, across))
        if forward is None:
            continue
        basis = np.stack([across, forward, normal], axis=0)

        for finger_index, chain in enumerate(HAND_CHAINS.values()):
            for segment_index, (start, end) in enumerate(zip(chain[:-1], chain[1:])):
                if not np.isfinite(hand[[start, end], :3]).all():
                    continue
                direction = _unit_or_none(hand[end] - hand[start])
                if direction is None:
                    continue
                directions[frame_index, finger_index, segment_index] = basis @ direction
                valid[frame_index, finger_index, segment_index] = True

    return directions, valid


def _build_palm_bases(left_points: np.ndarray, right_points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return right-handed palm bases for directly observed landmark frames.

    Callers must pass the raw detector output, before Cartesian landmark gap
    filling or smoothing.  Invalid frames deliberately remain invalid here;
    rotations are filled and filtered later on the SO(3) manifold.
    """
    frame_count = left_points.shape[0]
    bases = np.tile(np.eye(3, dtype=np.float64), (frame_count, 2, 1, 1))
    valid = np.zeros((frame_count, 2), dtype=bool)
    for side_index, points in enumerate((left_points, right_points)):
        for frame_index, hand in enumerate(points):
            if not np.isfinite(hand[[0, 5, 9, 17], :3]).all():
                continue
            across_vector = hand[5] - hand[17]
            forward_vector = hand[9] - hand[0]
            if np.linalg.norm(across_vector) < 1e-3 or np.linalg.norm(forward_vector) < 1e-3:
                continue
            across = _unit_or_none(across_vector)
            forward = _unit_or_none(forward_vector)
            if across is None or forward is None:
                continue
            raw_normal = np.cross(across, forward)
            # Near-collinear palm axes are an edge-on/degenerate detector
            # result; normalizing them would amplify tiny depth noise.
            if np.linalg.norm(raw_normal) < 0.15:
                continue
            normal = _unit_or_none(raw_normal)
            if normal is None:
                continue
            forward = _unit_or_none(np.cross(normal, across))
            if forward is None:
                continue
            basis = np.stack([across, forward, normal], axis=1)
            bases[frame_index, side_index] = basis
            valid[frame_index, side_index] = True
    return bases, valid


def _condition_palm_bases(
    bases: np.ndarray,
    observed: np.ndarray,
    max_gap: int,
    smoothing_radius: int,
    max_step: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Condition observed palm rotations without interpolating landmark XYZ.

    Only short, internally bracketed gaps are filled, using quaternion SLERP.
    The resulting contiguous SO(3) tracks are robustly centered-smoothed and
    finally rate-limited so one bad detector frame cannot spin the hand.
    """
    bases = np.asarray(bases, dtype=np.float64)
    observed = np.asarray(observed, dtype=bool)
    if bases.ndim != 4 or bases.shape[1:] != (2, 3, 3):
        raise ValueError("Palm bases must have shape (frames, 2, 3, 3).")
    if observed.shape != bases.shape[:2]:
        raise ValueError("Palm observation mask shape does not match palm bases.")

    output = np.tile(np.eye(3, dtype=np.float64), (bases.shape[0], 2, 1, 1))
    usable = observed.copy()
    max_gap = max(0, int(max_gap))
    smoothing_radius = max(0, int(smoothing_radius))
    max_step = float(max_step)
    if not np.isfinite(max_step) or max_step <= 0.0:
        raise ValueError("Palm rotation max_step must be finite and positive.")

    for side_index in range(2):
        quaternions = np.tile(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64), (bases.shape[0], 1))
        for frame_index in np.flatnonzero(observed[:, side_index]):
            basis = bases[frame_index, side_index]
            if not np.isfinite(basis).all() or abs(float(np.linalg.det(basis))) < 1e-6:
                usable[frame_index, side_index] = False
                continue
            quaternions[frame_index] = quaternion_from_matrix(basis)

        side_usable = usable[:, side_index]
        quaternions, side_usable = _fill_short_quaternion_gaps(quaternions, side_usable, max_gap=max_gap)
        if smoothing_radius:
            quaternions = _smooth_quaternion_track_centered(quaternions, side_usable, radius=smoothing_radius)
        quaternions = _limit_quaternion_track_steps(quaternions, side_usable, max_step=max_step)
        usable[:, side_index] = side_usable
        for frame_index in np.flatnonzero(side_usable):
            output[frame_index, side_index] = quaternion_to_matrix(quaternions[frame_index])

    return output, usable


def _fill_short_quaternion_gaps(
    quaternions: np.ndarray,
    valid: np.ndarray,
    max_gap: int,
) -> tuple[np.ndarray, np.ndarray]:
    """SLERP across short internal gaps; never extrapolate a track boundary."""
    output = np.asarray(quaternions, dtype=np.float64).copy()
    usable = np.asarray(valid, dtype=bool).copy()
    max_gap = max(0, int(max_gap))
    frame_count = output.shape[0]
    frame_index = 0
    while frame_index < frame_count:
        if usable[frame_index]:
            frame_index += 1
            continue
        gap_start = frame_index
        while frame_index < frame_count and not usable[frame_index]:
            frame_index += 1
        gap_end = frame_index
        gap_length = gap_end - gap_start
        if gap_start == 0 or gap_end == frame_count or gap_length > max_gap:
            continue
        first = output[gap_start - 1]
        second = output[gap_end]
        for offset in range(1, gap_length + 1):
            amount = offset / (gap_length + 1)
            output[gap_start + offset - 1] = quaternion_slerp(first, second, amount)
            usable[gap_start + offset - 1] = True
    return output, usable


def _smooth_quaternion_track_centered(
    quaternions: np.ndarray,
    valid: np.ndarray,
    radius: int,
) -> np.ndarray:
    """Robust zero-phase quaternion smoothing within each usable run.

    A geodesic medoid anchors a Tukey-weighted Markley mean.  This rejects a
    single flipped/noisy palm frame while using samples on both sides, avoiding
    the visible temporal lag produced by a causal filter.
    """
    source = np.asarray(quaternions, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    output = source.copy()
    radius = max(0, int(radius))
    if radius == 0:
        return output

    frame_count = source.shape[0]
    for frame_index in range(frame_count):
        if not valid[frame_index]:
            continue
        run_start = frame_index
        while run_start > 0 and valid[run_start - 1]:
            run_start -= 1
        run_end = frame_index + 1
        while run_end < frame_count and valid[run_end]:
            run_end += 1
        first = max(run_start, frame_index - radius)
        last = min(run_end, frame_index + radius + 1)
        sample_indices = np.arange(first, last, dtype=np.int64)
        samples = np.array([normalize_quaternion(source[index]) for index in sample_indices], dtype=np.float64)
        if samples.shape[0] == 1:
            output[frame_index] = samples[0]
            continue

        pairwise = np.empty((samples.shape[0], samples.shape[0]), dtype=np.float64)
        for row in range(samples.shape[0]):
            for column in range(samples.shape[0]):
                pairwise[row, column] = _quaternion_geodesic_angle(samples[row], samples[column])
        medoid = samples[int(np.argmin(pairwise.sum(axis=1)))]
        distances = np.array([_quaternion_geodesic_angle(medoid, sample) for sample in samples])
        median_distance = float(np.median(distances))
        mad = float(np.median(np.abs(distances - median_distance)))
        robust_scale = max(np.radians(3.0), 1.4826 * mad)
        normalized_distance = distances / (4.685 * robust_scale)
        robust_weights = np.where(
            normalized_distance < 1.0,
            (1.0 - normalized_distance * normalized_distance) ** 2,
            0.0,
        )
        temporal_weights = (radius + 1 - np.abs(sample_indices - frame_index)).astype(np.float64)
        weights = robust_weights * temporal_weights
        if float(weights.sum()) < 1e-8:
            output[frame_index] = medoid
            continue
        accumulator = np.zeros((4, 4), dtype=np.float64)
        for sample, weight in zip(samples, weights):
            accumulator += float(weight) * np.outer(sample, sample)
        eigenvalues, eigenvectors = np.linalg.eigh(accumulator)
        mean = normalize_quaternion(eigenvectors[:, int(np.argmax(eigenvalues))])
        if np.dot(mean, source[frame_index]) < 0.0:
            mean = -mean
        output[frame_index] = mean
    return output


def _limit_quaternion_track_steps(quaternions: np.ndarray, valid: np.ndarray, max_step: float) -> np.ndarray:
    """Cap geodesic rotation per frame within each contiguous usable run."""
    output = np.asarray(quaternions, dtype=np.float64).copy()
    valid = np.asarray(valid, dtype=bool)
    # Leave a tiny numerical margin so downstream strict ``<=`` QC does not
    # report 35.0000000000001 degrees after matrix/quaternion round trips.
    safe_max_step = max_step * (1.0 - 1e-12)
    previous: np.ndarray | None = None
    for frame_index in range(output.shape[0]):
        if not valid[frame_index]:
            previous = None
            continue
        current = normalize_quaternion(output[frame_index])
        if previous is not None:
            angle = _quaternion_geodesic_angle(previous, current)
            if angle > safe_max_step:
                current = quaternion_slerp(previous, current, safe_max_step / angle)
        if previous is not None and np.dot(previous, current) < 0.0:
            current = -current
        output[frame_index] = current
        previous = current
    return output


def _quaternion_geodesic_angle(first: np.ndarray, second: np.ndarray) -> float:
    dot = abs(float(np.dot(normalize_quaternion(first), normalize_quaternion(second))))
    return 2.0 * float(np.arccos(np.clip(dot, 0.0, 1.0)))


def _hold_palm_bases_for_release(
    bases: np.ndarray,
    source_valid: np.ndarray,
    release_frames: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Carry the final finite palm orientation while its influence fades out."""
    output = np.asarray(bases, dtype=np.float64).copy()
    source_valid = np.asarray(source_valid, dtype=bool)
    constraint_valid = source_valid.copy()
    release_frames = max(1, int(release_frames))
    for side_index in range(output.shape[1]):
        previous: np.ndarray | None = None
        remaining = 0
        for frame_index in range(output.shape[0]):
            if source_valid[frame_index, side_index]:
                previous = output[frame_index, side_index].copy()
                remaining = release_frames
            elif previous is not None and remaining > 0:
                output[frame_index, side_index] = previous
                constraint_valid[frame_index, side_index] = True
                remaining -= 1
            else:
                previous = None
                remaining = 0
    return output, constraint_valid


def _ramp_palm_basis_activation(
    source_valid: np.ndarray,
    ramp_frames: int,
    constraint_valid: np.ndarray | None = None,
) -> np.ndarray:
    source_valid = np.asarray(source_valid, dtype=bool)
    constraint_valid = source_valid if constraint_valid is None else np.asarray(constraint_valid, dtype=bool)
    if constraint_valid.shape != source_valid.shape:
        raise ValueError("Palm constraint mask shape does not match source mask.")
    output = np.zeros(source_valid.shape, dtype=np.float64)
    ramp_frames = max(1, int(ramp_frames))
    for side_index in range(source_valid.shape[1]):
        consecutive = 0
        release_frame = 0
        last_influence = 0.0
        for frame_index in range(source_valid.shape[0]):
            if source_valid[frame_index, side_index]:
                consecutive += 1
                release_frame = 0
                last_influence = min(1.0, consecutive / ramp_frames)
                output[frame_index, side_index] = last_influence
            elif constraint_valid[frame_index, side_index]:
                release_frame += 1
                output[frame_index, side_index] = max(0.0, last_influence - release_frame / (ramp_frames + 1))
            else:
                consecutive = 0
                release_frame = 0
                last_influence = 0.0
    return output


def _unit_or_none(vector: np.ndarray) -> np.ndarray | None:
    vector = np.asarray(vector, dtype=np.float64)
    length = float(np.linalg.norm(vector))
    if not np.isfinite(length) or length < 1e-8:
        return None
    return vector / length


def _condition_finger_directions(
    directions: np.ndarray,
    observed: np.ndarray,
    max_gap: int,
    smoothing_radius: int,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Remove isolated local-shape spikes without mixing world hand rotations.

    Only a single-frame excursion with two large steps and agreeing neighbors
    is rejected. Sustained handshapes remain intact. Missing directions are
    spherically interpolated only inside short bracketed gaps; the caller's
    detector-observation mask is never changed by filling or conditioning.
    """
    source = np.asarray(directions, dtype=np.float64)
    observed = np.asarray(observed, dtype=bool)
    if source.ndim != 5 or source.shape[-1] != 3 or observed.shape != source.shape[:-1]:
        raise ValueError("Finger direction and observation shapes do not match.")
    frame_count = source.shape[0]
    output = source.copy()
    usable = observed.copy() & np.isfinite(source).all(axis=-1)
    lengths = np.linalg.norm(source, axis=-1)
    usable &= lengths > 1e-8
    output = np.divide(output, lengths[..., None], out=np.full_like(output, np.nan),
                       where=usable[..., None])
    tracks = output.reshape(frame_count, -1, 3)
    masks = usable.reshape(frame_count, -1)
    corrections = []
    interpolated_count = 0
    radius = max(0, int(smoothing_radius))
    for channel in range(tracks.shape[1]):
        values = tracks[:, channel]
        valid = masks[:, channel]
        if radius:
            # Evaluate all candidates against the original track, never against
            # already repaired neighbors (which would spread a correction).
            original = values.copy()
            for frame in range(1, frame_count - 1):
                if not valid[frame - 1:frame + 2].all():
                    continue
                before, current, after = original[frame - 1:frame + 2]
                angles = np.arccos(np.clip([before @ current, current @ after, before @ after], -1.0, 1.0))
                if min(angles[:2]) > np.radians(45.0) and angles[2] < np.radians(30.0):
                    values[frame] = _slerp_direction(before, after, 0.5)
                    side, finger, segment = np.unravel_index(channel, source.shape[1:-1])
                    corrections.append({"frame": frame + 1, "side": int(side),
                                        "finger": int(finger), "segment": int(segment),
                                        "adjustment_degrees": float(np.degrees(np.arccos(np.clip(current @ values[frame], -1.0, 1.0))))})
        frame = 0
        while frame < frame_count:
            if valid[frame]:
                frame += 1
                continue
            first = frame
            while frame < frame_count and not valid[frame]:
                frame += 1
            gap = frame - first
            if first == 0 or frame == frame_count or gap > max(0, int(max_gap)):
                continue
            before, after = values[first - 1].copy(), values[frame].copy()
            if before @ after < -0.9995:
                continue  # Antipodal samples do not define a unique arc.
            for offset in range(gap):
                values[first + offset] = _slerp_direction(before, after, (offset + 1) / (gap + 1))
                valid[first + offset] = True
                interpolated_count += 1
        if radius:
            original = values.copy()
            for frame in range(1, frame_count - 1):
                # Preserve run endpoints and never smooth across an unfilled gap.
                if not valid[frame - 1:frame + 2].all():
                    continue
                reach = 1
                while reach < radius and frame - reach > 0 and frame + reach + 1 < frame_count:
                    if not (valid[frame - reach - 1] and valid[frame + reach + 1]):
                        break
                    reach += 1
                offsets = np.arange(-reach, reach + 1)
                mean = np.sum(original[frame + offsets] * (reach + 1 - np.abs(offsets))[:, None], axis=0)
                normalized = _unit_or_none(mean)
                if normalized is not None:
                    values[frame] = normalized
    report = {
        "method": "palm_local_isolated_spike_rejection_and_centered_direction_smoothing",
        "status": "REVIEW" if corrections else "PASS",
        "observed_samples": int(observed.sum()),
        "interpolated_samples": interpolated_count,
        "corrected_sample_count": len(corrections),
        "sample_corrections": corrections,
        "observation_mask_preserved": True,
    }
    return output, usable, report


def _limit_finger_direction_steps(directions: np.ndarray, valid: np.ndarray, max_delta: float) -> np.ndarray:
    """Spherically limit each tracked local finger direction between frames."""
    output = np.asarray(directions, dtype=np.float64).copy()
    valid = np.asarray(valid, dtype=bool)
    for side_index in range(output.shape[1]):
        for finger_index in range(output.shape[2]):
            for segment_index in range(output.shape[3]):
                previous: np.ndarray | None = None
                for frame_index in range(output.shape[0]):
                    if not valid[frame_index, side_index, finger_index, segment_index]:
                        previous = None
                        continue
                    current = _unit_or_none(output[frame_index, side_index, finger_index, segment_index])
                    if current is None:
                        valid[frame_index, side_index, finger_index, segment_index] = False
                        previous = None
                        continue
                    if previous is not None:
                        dot = float(np.clip(np.dot(previous, current), -1.0, 1.0))
                        angle = float(np.arccos(dot))
                        if angle > max_delta:
                            current = _slerp_direction(previous, current, max_delta / angle)
                    output[frame_index, side_index, finger_index, segment_index] = current
                    previous = current
    return output


def _slerp_direction(first: np.ndarray, second: np.ndarray, amount: float) -> np.ndarray:
    first = _unit_or_none(first)
    second = _unit_or_none(second)
    if first is None or second is None:
        return np.array([0.0, 1.0, 0.0], dtype=np.float64)
    amount = float(np.clip(amount, 0.0, 1.0))
    dot = float(np.clip(np.dot(first, second), -1.0, 1.0))
    if dot > 0.9995:
        result = _unit_or_none(first + amount * (second - first))
        return first if result is None else result
    angle = float(np.arccos(dot))
    sine = float(np.sin(angle))
    if abs(sine) < 1e-8:
        return first
    result = _unit_or_none((np.sin((1.0 - amount) * angle) / sine) * first + (np.sin(amount * angle) / sine) * second)
    return first if result is None else result


def _hold_finger_directions_for_release(
    directions: np.ndarray,
    source_valid: np.ndarray,
    release_frames: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Freeze the last known handshape briefly while a lost hand fades out."""
    output = np.asarray(directions, dtype=np.float64).copy()
    source_valid = np.asarray(source_valid, dtype=bool)
    constraint_valid = source_valid.copy()
    release_frames = max(1, int(release_frames))
    for side_index in range(output.shape[1]):
        for finger_index in range(output.shape[2]):
            for segment_index in range(output.shape[3]):
                previous: np.ndarray | None = None
                remaining = 0
                for frame_index in range(output.shape[0]):
                    if source_valid[frame_index, side_index, finger_index, segment_index]:
                        previous = output[frame_index, side_index, finger_index, segment_index].copy()
                        remaining = release_frames
                    elif previous is not None and remaining > 0:
                        output[frame_index, side_index, finger_index, segment_index] = previous
                        constraint_valid[frame_index, side_index, finger_index, segment_index] = True
                        remaining -= 1
                    else:
                        previous = None
                        remaining = 0
    return output, constraint_valid


def _ramp_finger_direction_activation(
    source_valid: np.ndarray,
    constraint_valid: np.ndarray,
    ramp_frames: int,
) -> np.ndarray:
    """Fade a re-acquired or lost handshape between direct-curl and target modes."""
    source_valid = np.asarray(source_valid, dtype=bool)
    constraint_valid = np.asarray(constraint_valid, dtype=bool)
    output = np.zeros(source_valid.shape, dtype=np.float64)
    ramp_frames = max(1, int(ramp_frames))
    for side_index in range(source_valid.shape[1]):
        for finger_index in range(source_valid.shape[2]):
            for segment_index in range(source_valid.shape[3]):
                consecutive = 0
                release_frame = 0
                last_influence = 0.0
                for frame_index in range(source_valid.shape[0]):
                    if source_valid[frame_index, side_index, finger_index, segment_index]:
                        consecutive += 1
                        release_frame = 0
                        last_influence = min(1.0, consecutive / ramp_frames)
                        output[frame_index, side_index, finger_index, segment_index] = last_influence
                    elif constraint_valid[frame_index, side_index, finger_index, segment_index]:
                        consecutive = 0
                        release_frame += 1
                        output[frame_index, side_index, finger_index, segment_index] = max(0.0, last_influence - release_frame / (ramp_frames + 1))
                    else:
                        consecutive = 0
                        release_frame = 0
                        last_influence = 0.0
    return output


def _solve_finger_frame(rotations, bone_names, curls: np.ndarray, side: str) -> None:
    for finger_index, finger in enumerate(HAND_CHAINS):
        for joint_index in range(3):
            canonical = f"{side}{finger}{joint_index + 1}"
            if canonical not in bone_names:
                continue
            raw_curl = curls[finger_index, joint_index]
            if not np.isfinite(raw_curl):
                continue
            curl = _bounded_finger_curl(finger, joint_index, float(raw_curl))
            # The calibrated Mixamo-like avatar uses local +X as anatomical
            # flexion for both hands. Local rotations keep palm orientation
            # independent from the finger's curl.
            _set_if_present(rotations, bone_names, canonical, quaternion_from_axis_angle([1.0, 0.0, 0.0], curl))


def _bounded_finger_curl(finger: str, joint_index: int, raw_curl: float) -> float:
    if finger == "Thumb":
        limits_degrees = (48.0, 65.0, 55.0)
        scale = 0.72
    else:
        limits_degrees = (70.0, 100.0, 80.0)
        scale = 0.72
    return float(np.clip(raw_curl * scale, 0.0, np.radians(limits_degrees[joint_index])))


def _limit_finger_curl_steps(curls: np.ndarray, max_delta: float) -> np.ndarray:
    """Prevent a detected hand from producing a one-frame finger snap."""
    output = np.asarray(curls, dtype=np.float64).copy()
    for finger_index in range(output.shape[1]):
        for joint_index in range(output.shape[2]):
            previous = np.nan
            for frame_index in range(output.shape[0]):
                current = output[frame_index, finger_index, joint_index]
                if not np.isfinite(current):
                    previous = np.nan
                    continue
                if np.isfinite(previous):
                    current = float(np.clip(current, previous - max_delta, previous + max_delta))
                    output[frame_index, finger_index, joint_index] = current
                else:
                    # A re-acquired hand begins from the unanimated/rest hand
                    # instead of snapping directly from identity to a noisy pose.
                    current = float(np.clip(current, 0.0, max_delta))
                    output[frame_index, finger_index, joint_index] = current
                previous = current
    return output


def _hold_last_finger_curls(curls: np.ndarray) -> np.ndarray:
    """Freeze the last observed handshape during a tracking gap, never inventing new motion."""
    output = np.asarray(curls, dtype=np.float64).copy()
    for finger_index in range(output.shape[1]):
        for joint_index in range(output.shape[2]):
            previous = np.nan
            for frame_index in range(output.shape[0]):
                current = output[frame_index, finger_index, joint_index]
                if np.isfinite(current):
                    previous = current
                elif np.isfinite(previous):
                    output[frame_index, finger_index, joint_index] = previous
    return output


def _set_swing(
    rotations,
    bone_names,
    bone_map,
    rest: RestPose,
    canonical_name: str,
    target_vector,
    strength: float = 0.65,
    max_angle: float = np.radians(75.0),
) -> None:
    if canonical_name not in bone_map or not np.isfinite(target_vector).all():
        return
    avatar_bone = bone_map[canonical_name]
    rest_direction = rest.direction(avatar_bone)
    target = normalize_vector(target_vector)
    swing_armature = quaternion_from_vectors(rest_direction, target)
    rest_rotation = rest.rotation_matrix(avatar_bone)
    local_matrix = rest_rotation.T @ quaternion_to_matrix(swing_armature) @ rest_rotation
    q = quaternion_from_matrix(local_matrix)
    q = limit_quaternion_angle(q, max_angle)
    q = quaternion_slerp(np.array([1.0, 0.0, 0.0, 0.0]), q, strength)
    _set_if_present(rotations, bone_names, canonical_name, q)


def _set_if_present(rotations, bone_names, canonical_name: str, q) -> None:
    if canonical_name in bone_names:
        rotations[bone_names.index(canonical_name)] = normalize_quaternion(q)


def _set_identity(rotations, bone_names, canonical_name: str) -> None:
    _set_if_present(rotations, bone_names, canonical_name, np.array([1.0, 0.0, 0.0, 0.0]))


def _torso_clearance_depth(
    x: float,
    y: float,
    *,
    hips_rest: np.ndarray,
    shoulder_center: np.ndarray,
    shoulder_width: float,
    torso_height: float,
    margin_ratio: float = 0.08,
) -> float | None:
    """Return minimum safe front depth (Z) for targets near the torso.

    Uses an elliptical cross-section of the torso based on anatomical rest
    proportions, ensuring wrists and hands never penetrate inside the body mesh.
    """
    y_hips = float(hips_rest[1])
    y_shoulders = float(shoulder_center[1])
    y_bottom = y_hips - 0.15 * torso_height
    y_top = y_shoulders + 0.20 * torso_height
    if y < y_bottom or y > y_top:
        return None

    t = float(np.clip((y - y_hips) / max(torso_height, 1e-4), 0.0, 1.0))
    rx = shoulder_width * (0.42 + 0.04 * t)
    dx = float(x - shoulder_center[0])
    if abs(dx) >= rx:
        return None

    z_spine = (1.0 - t) * float(hips_rest[2]) + t * float(shoulder_center[2])
    rz = shoulder_width * (0.23 + 0.09 * t)
    radial = float(np.sqrt(max(0.0, 1.0 - (dx / rx) ** 2)))
    surface_z = z_spine + rz * radial
    return surface_z + shoulder_width * margin_ratio


def _build_ik_targets(
    pose_image: np.ndarray,
    rest: RestPose,
    bone_map: dict[str, str],
    *,
    diagnostics: dict | None = None,
    depth_offsets: np.ndarray | None = None,
    left_hand_image: np.ndarray | None = None,
    right_hand_image: np.ndarray | None = None,
) -> np.ndarray:
    frame_count = pose_image.shape[0]
    targets = np.zeros((frame_count, 4, 3), dtype=np.float64)
    left_shoulder_rest = np.array(rest.bone_info[bone_map["LeftUpperArm"]]["head_local"], dtype=np.float64)
    right_shoulder_rest = np.array(rest.bone_info[bone_map["RightUpperArm"]]["head_local"], dtype=np.float64)
    hips_rest = np.array(rest.bone_info[bone_map["Hips"]]["head_local"], dtype=np.float64)
    avatar_shoulder_center = (left_shoulder_rest + right_shoulder_rest) * 0.5
    avatar_shoulder_width = abs(left_shoulder_rest[0] - right_shoulder_rest[0])
    avatar_torso_height = abs(avatar_shoulder_center[1] - hips_rest[1])
    rest_depth = float((left_shoulder_rest[2] + right_shoulder_rest[2]) * 0.5)
    arm_lengths = np.array([
        sum(float(rest.bone_info[bone_map[f"{side}{part}"]]["length"]) for part in ("UpperArm", "ForeArm"))
        for side in ("Left", "Right")
    ])
    if depth_offsets is not None:
        depth_offsets = np.asarray(depth_offsets, dtype=np.float64)
        if depth_offsets.shape != (frame_count, 4) or not np.isfinite(depth_offsets).all():
            raise ValueError("Arm-depth offsets must match all frames and four targets.")

    previous = None
    for frame_index in range(frame_count):
        frame = pose_image[frame_index]
        if not np.isfinite(frame[[11, 12], :2]).all():
            if previous is not None:
                targets[frame_index] = previous
            continue
        source_left_shoulder = frame[11]
        source_right_shoulder = frame[12]
        source_left_hip = frame[23]
        source_right_hip = frame[24]
        source_center_x = float((source_left_shoulder[0] + source_right_shoulder[0]) * 0.5)
        source_center_y = float((source_left_shoulder[1] + source_right_shoulder[1]) * 0.5)
        source_width = abs(float(source_left_shoulder[0] - source_right_shoulder[0]))
        source_hip_center_y = float((source_left_hip[1] + source_right_hip[1]) * 0.5) if np.isfinite(frame[[23, 24], 1]).all() else source_center_y + 0.32
        source_torso_height = abs(source_hip_center_y - source_center_y)
        scale_x = avatar_shoulder_width / max(source_width, 0.05)
        scale_y = avatar_torso_height / max(source_torso_height, 0.18)

        mapped = []
        for landmark_index in [15, 13, 16, 14]:
            landmark = frame[landmark_index]
            if not np.isfinite(landmark[:2]).all():
                mapped.append(previous[len(mapped)] if previous is not None else avatar_shoulder_center)
                continue
            x = avatar_shoulder_center[0] + (float(landmark[0]) - source_center_x) * scale_x
            y = avatar_shoulder_center[1] + (source_center_y - float(landmark[1])) * scale_y
            target_index = len(mapped)
            side = "Left" if target_index < 2 else "Right"
            shoulder_depth = float(rest.bone_info[bone_map[f"{side}UpperArm"]]["head_local"][2])
            shoulder_origin = left_shoulder_rest if side == "Left" else right_shoulder_rest
            side_reach = arm_lengths[0 if side == "Left" else 1]

            if depth_offsets is None:
                # Legacy/helper callers without observations use explicit rest
                # geometry, never a fixed number in arbitrary avatar units.
                part = "Hand" if target_index % 2 == 0 else "ForeArm"
                z = float(rest.bone_info[bone_map[f"{side}{part}"]]["head_local"][2])
            else:
                z = shoulder_depth + depth_offsets[frame_index, target_index]

            if target_index in (0, 2):  # Wrists
                min_z = _torso_clearance_depth(
                    x, y,
                    hips_rest=hips_rest,
                    shoulder_center=avatar_shoulder_center,
                    shoulder_width=avatar_shoulder_width,
                    torso_height=avatar_torso_height,
                    margin_ratio=0.08,
                )
                if min_z is not None:
                    z = max(z, min_z)
                # Keep target wrist within comfortable reach interval
                max_reach = side_reach * 0.98
                lateral_sq = (x - shoulder_origin[0]) ** 2 + (y - shoulder_origin[1]) ** 2
                if lateral_sq < max_reach ** 2:
                    max_z = shoulder_origin[2] + float(np.sqrt(max_reach ** 2 - lateral_sq))
                    z = min(z, max_z)
            elif target_index in (1, 3):  # Elbows
                min_z = _torso_clearance_depth(
                    x, y,
                    hips_rest=hips_rest,
                    shoulder_center=avatar_shoulder_center,
                    shoulder_width=avatar_shoulder_width,
                    torso_height=avatar_torso_height,
                    margin_ratio=0.01,
                )
                if min_z is not None:
                    z = max(z, min_z)

            mapped.append(np.array([x, y, z], dtype=np.float64))
        targets[frame_index] = np.array(mapped, dtype=np.float64)
        previous = targets[frame_index].copy()

    # --- Wrist proximity correction ---
    # Independent shoulder-based scaling can amplify the lateral gap between
    # wrists when hands are close together (e.g., ISL letter signs). When hands
    # are close in image space (e.g., thumb tips touching), pull both wrists inward
    # toward their midpoint to restore the observed closeness on the avatar.
    pull_factors = np.zeros(frame_count, dtype=np.float64)
    proximity_diagnostics: list[dict] = []

    for frame_index in range(frame_count):
        frame = pose_image[frame_index]
        if not np.isfinite(frame[[11, 12, 15, 16], :2]).all():
            continue
        source_sw = abs(float(frame[11, 0] - frame[12, 0]))
        if source_sw < 0.03:
            continue

        has_hands = False
        if left_hand_image is not None and right_hand_image is not None:
            lh = left_hand_image[frame_index, :, :2]
            rh = right_hand_image[frame_index, :, :2]
            if np.isfinite(lh).all() and np.isfinite(rh).all():
                diff = lh[:, None, :] - rh[None, :, :]
                hand_dist = float(np.min(np.linalg.norm(diff, axis=-1)))
                hand_ratio = hand_dist / source_sw
                has_hands = True
                # When hands are close (< 0.35 shoulder widths), pull wrists inward.
                # Max pull is 0.48 when hands are touching (hand_ratio ~ 0).
                if hand_ratio < 0.35:
                    pull_factors[frame_index] = 0.48 * (1.0 - hand_ratio / 0.35)

        if not has_hands:
            # Fallback using pose wrist distance
            source_wrist_dx = float(frame[15, 0] - frame[16, 0])
            source_wrist_dy = float(frame[15, 1] - frame[16, 1])
            source_wrist_dist = np.sqrt(source_wrist_dx ** 2 + source_wrist_dy ** 2)
            source_ratio = source_wrist_dist / source_sw
            if source_ratio < 0.85:
                pull_factors[frame_index] = 0.40 * max(0.0, 1.0 - (source_ratio - 0.25) / 0.60)

    # Smooth pull factors temporally to avoid jitter
    if frame_count > 4:
        pull_factors = smooth_landmarks_centered(pull_factors[:, None], radius=2)[:, 0]

    for frame_index in range(frame_count):
        blend = pull_factors[frame_index]
        if blend <= 0.0:
            continue
        lw = targets[frame_index, 0]
        rw = targets[frame_index, 2]
        old_lateral = float(np.sqrt((lw[0] - rw[0]) ** 2 + (lw[1] - rw[1]) ** 2))
        midpoint_x = (lw[0] + rw[0]) * 0.5
        midpoint_y = (lw[1] + rw[1]) * 0.5
        targets[frame_index, 0, 0] = lw[0] - (lw[0] - midpoint_x) * blend
        targets[frame_index, 0, 1] = lw[1] - (lw[1] - midpoint_y) * blend
        targets[frame_index, 2, 0] = rw[0] - (rw[0] - midpoint_x) * blend
        targets[frame_index, 2, 1] = rw[1] - (rw[1] - midpoint_y) * blend

        # Ensure torso clearance is maintained after inward pull
        for wrist_idx in (0, 2):
            min_z = _torso_clearance_depth(
                targets[frame_index, wrist_idx, 0],
                targets[frame_index, wrist_idx, 1],
                hips_rest=hips_rest,
                shoulder_center=avatar_shoulder_center,
                shoulder_width=avatar_shoulder_width,
                torso_height=avatar_torso_height,
                margin_ratio=0.08,
            )
            if min_z is not None:
                targets[frame_index, wrist_idx, 2] = max(targets[frame_index, wrist_idx, 2], min_z)

        if len(proximity_diagnostics) < 8:
            new_lateral = float(np.sqrt(
                (targets[frame_index, 0, 0] - targets[frame_index, 2, 0]) ** 2 +
                (targets[frame_index, 0, 1] - targets[frame_index, 2, 1]) ** 2
            ))
            proximity_diagnostics.append({
                "frame": frame_index,
                "correction_blend": float(blend),
                "old_lateral": old_lateral,
                "new_lateral": new_lateral,
            })

    if diagnostics is not None:
        diagnostics["wrist_proximity_correction"] = {
            "corrected_frames": int(np.sum(pull_factors > 0.01)),
            "max_pull_blend": float(np.max(pull_factors)) if len(pull_factors) else 0.0,
            "samples": proximity_diagnostics,
        }
    conditioned, pole_diagnostics = _condition_arm_pole_targets(
        targets, np.array([left_shoulder_rest, right_shoulder_rest]), arm_lengths,
    )
    if diagnostics is not None:
        diagnostics["arm_pole_conditioning"] = pole_diagnostics
    return conditioned


def _condition_arm_pole_targets(
    targets: np.ndarray,
    shoulders: np.ndarray,
    arm_lengths: np.ndarray,
    *,
    reliability_radius_ratio: float = 0.08,
) -> tuple[np.ndarray, dict]:
    """Resolve an ambiguous IK bend plane without altering wrist trajectories.

    An elbow on the shoulder/wrist axis has no usable pole direction. Tiny
    tracking changes then create a 180-degree IK flip, even when the wrist is
    stationary. Only these poorly conditioned spans use directions interpolated
    between reliable neighbors. All reliable elbow targets remain untouched.
    The radius is relative to arm length so the rule is independent of rig units.
    """
    values = np.asarray(targets, dtype=np.float64)
    shoulder_points = np.asarray(shoulders, dtype=np.float64)
    lengths = np.asarray(arm_lengths, dtype=np.float64)
    if values.ndim != 3 or values.shape[1:] != (4, 3) or shoulder_points.shape != (2, 3) or lengths.shape != (2,):
        raise ValueError("Arm pole conditioning requires targets [frames,4,3], shoulders [2,3], and two arm lengths.")
    if not np.isfinite(values).all() or not np.isfinite(shoulder_points).all() or not np.isfinite(lengths).all() or np.any(lengths <= 0):
        raise ValueError("Arm pole conditioning requires finite targets and positive arm lengths.")
    if not 0 < reliability_radius_ratio < 0.5:
        raise ValueError("Arm pole reliability radius must be between zero and half the arm length.")
    result = values.copy()
    report = {"method": "interpolate_geometrically_ambiguous_poles", "reliability_radius_ratio": reliability_radius_ratio, "sides": {}}
    for side_index, side in enumerate(("Left", "Right")):
        wrist_index, elbow_index = 2 * side_index, 2 * side_index + 1
        shoulder = shoulder_points[side_index]
        shoulder_to_wrist = values[:, wrist_index] - shoulder
        axis_lengths = np.linalg.norm(shoulder_to_wrist, axis=1)
        axes = np.divide(shoulder_to_wrist, axis_lengths[:, None], out=np.zeros_like(shoulder_to_wrist), where=axis_lengths[:, None] > 1e-9)
        # A wrist exactly at the shoulder is also singular. Reuse an adjacent
        # axis solely for pole conditioning; the original wrist is never moved.
        usable_axes = np.flatnonzero(axis_lengths > 1e-9)
        for index in np.flatnonzero(axis_lengths <= 1e-9):
            axes[index] = axes[usable_axes[np.argmin(np.abs(usable_axes - index))]] if len(usable_axes) else np.array([0.0, -1.0, 0.0])
        elbow_offsets = values[:, elbow_index] - shoulder
        axial = np.sum(elbow_offsets * axes, axis=1)
        projected = elbow_offsets - axial[:, None] * axes
        radii = np.linalg.norm(projected, axis=1)
        minimum_radius = float(lengths[side_index] * reliability_radius_ratio)
        reliable = (radii >= minimum_radius) & (axis_lengths > 1e-9)
        spans = []
        index = 0
        while index < len(values):
            if reliable[index]:
                index += 1
                continue
            start = index
            while index < len(values) and not reliable[index]:
                index += 1
            stop = index
            before = start - 1 if start else None
            after = stop if stop < len(values) else None
            spans.append({"start_frame": start, "end_frame": stop - 1,
                          "frame_count": stop - start, "reliable_boundary_count": int(before is not None) + int(after is not None)})
            for frame_index in range(start, stop):
                axis = axes[frame_index]

                def project_direction(anchor: int | None) -> np.ndarray | None:
                    if anchor is None:
                        return None
                    direction = projected[anchor] - axis * np.dot(projected[anchor], axis)
                    norm = float(np.linalg.norm(direction))
                    return direction / norm if norm > 1e-9 else None

                first, last = project_direction(before), project_direction(after)
                if first is None and last is None:
                    # No measured bend plane is available. Choose a finite
                    # deterministic fallback and expose the unanchored span.
                    basis = np.eye(3)[int(np.argmin(np.abs(axis)))]
                    direction = basis - axis * np.dot(basis, axis)
                    direction /= np.linalg.norm(direction)
                elif first is None:
                    direction = last
                elif last is None:
                    direction = first
                else:
                    fraction = (frame_index - before) / (after - before)
                    signed_angle = float(np.arctan2(np.dot(axis, np.cross(first, last)), np.clip(np.dot(first, last), -1.0, 1.0)))
                    direction = first * np.cos(fraction * signed_angle) + np.cross(axis, first) * np.sin(fraction * signed_angle)
                result[frame_index, elbow_index] = shoulder + axial[frame_index] * axis + minimum_radius * direction
        report["sides"][side] = {
            "conditioned_frames": int((~reliable).sum()),
            "minimum_pole_radius": minimum_radius,
            "minimum_observed_radius": float(radii.min()) if len(radii) else None,
            "spans": spans,
            "unanchored_spans": sum(span["reliable_boundary_count"] == 0 for span in spans),
        }
    return result, report
