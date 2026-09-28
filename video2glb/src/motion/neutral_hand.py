from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from src.avatar.bone_mapping import (
    load_avatar_profile,
    load_bone_map,
    validate_avatar_bone_map_topology,
)
from src.metadata.production_metadata import atomic_write_json, sha256_file
from src.motion.quaternion_utils import (
    normalize_quaternion,
    normalize_vector,
    quaternion_from_matrix,
    quaternion_from_axis_angle,
    quaternion_slerp,
    quaternion_from_vectors,
    quaternion_to_matrix,
)


FINGERS = ("Thumb", "Index", "Middle", "Ring", "Little")
NEUTRAL_SCHEMA_VERSION = "1.3"


def ensure_neutral_hand_pose(profile_path: str | Path, bone_map_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    output = Path(output_path)
    current_binding = {
        "avatar_profile_sha256": sha256_file(profile_path),
        "bone_map_sha256": sha256_file(bone_map_path),
    }
    if output.exists():
        payload = json.loads(output.read_text(encoding="utf-8"))
        if (
            payload.get("schema_version") == NEUTRAL_SCHEMA_VERSION
            and payload.get("calibration_binding") == current_binding
        ):
            validate_neutral_hand_pose(payload)
            return payload
    payload = generate_neutral_hand_pose(profile_path, bone_map_path)
    payload["calibration_binding"] = current_binding
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output, payload)
    return payload


def generate_neutral_hand_pose(profile_path: str | Path, bone_map_path: str | Path) -> dict[str, Any]:
    profile = load_avatar_profile(profile_path)
    bone_map = load_bone_map(bone_map_path)
    validate_avatar_bone_map_topology(bone_map, profile)
    bones = {str(item["name"]): item for item in profile.get("bones", [])}
    payload: dict[str, Any] = {
        "schema_version": NEUTRAL_SCHEMA_VERSION,
        "quaternion_order": "WXYZ",
        "rotation_space": "LOCAL_POSE_DELTA_FROM_AVATAR_REST",
        "generation_method": "Hierarchy-aware finger FK straightening and signed anatomical palm alignment after lowering the rest arm.",
        "wrist_pose": {},
        "left_hand": {},
        "right_hand": {},
        "validation": {},
    }
    correction_angles: dict[str, list[float]] = {"left_hand": [], "right_hand": []}
    target_directions: dict[str, list[np.ndarray]] = {"left_hand": [], "right_hand": []}
    target_segments: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {"left_hand": [], "right_hand": []}
    solved_rotations: dict[str, np.ndarray] = {}
    straightness_errors: dict[str, list[float]] = {"left_hand": [], "right_hand": []}

    for side, hand_key in (("Left", "left_hand"), ("Right", "right_hand")):
        hand_bone_name = bone_map.get(f"{side}Hand")
        if not hand_bone_name or hand_bone_name not in bones:
            raise ValueError(f"Neutral hand calibration is missing {side}Hand.")
        hand = bones[hand_bone_name]
        wrist = np.asarray(hand["head_local"], dtype=np.float64)
        index = np.asarray(bones[bone_map[f"{side}Index1"]]["head_local"], dtype=np.float64)
        middle = np.asarray(bones[bone_map[f"{side}Middle1"]]["head_local"], dtype=np.float64)
        little = np.asarray(bones[bone_map[f"{side}Little1"]]["head_local"], dtype=np.float64)
        across = normalize_vector(index - little)
        forward = normalize_vector(middle - wrist)
        palm_normal = normalize_vector(np.cross(across, forward))
        wrist_quaternion, roll_degrees = _calibrate_medial_wrist(bones, bone_map, side, palm_normal)
        payload["wrist_pose"][hand_key] = {
            "canonical_bone": f"{side}Hand",
            "avatar_bone": hand_bone_name,
            "rotation_wxyz": [float(value) for value in wrist_quaternion],
            "roll_degrees": roll_degrees,
            "calibration_basis": "signed_palmar_normal_in_lowered_arm_frame",
        }
        for finger in FINGERS:
            canonicals = [f"{side}{finger}{joint}" for joint in (1, 2, 3)]
            avatar_names = [bone_map.get(name) for name in canonicals]
            if any(not name or name not in bones for name in avatar_names):
                raise ValueError(f"Neutral hand calibration is missing mapped bones for {side}{finger}: {avatar_names}")
            chain = [bones[str(name)] for name in avatar_names]
            chain_start = np.asarray(chain[0]["head_local"], dtype=np.float64)
            target = np.asarray(chain[0]["tail_local"], dtype=np.float64) - chain_start
            if finger != "Thumb":
                # Open the proximal phalanx into the palm plane.  Using a curled
                # chain's endpoint as its target preserves the unwanted MCP curl.
                target -= palm_normal * float(np.dot(target, palm_normal))
            target = normalize_vector(target)
            if np.linalg.norm(target) < 1e-8:
                raise ValueError(f"Zero-length neutral target for {side}{finger}")
            target_directions[hand_key].append(target)
            chain_length = float(sum(float(item.get("length", 0.0)) for item in chain))
            target_segments[hand_key].append((chain_start, chain_start + target * chain_length))
            for joint_index, (canonical, bone) in enumerate(zip(canonicals, chain), start=1):
                # Blender composes pose deltas through all ancestors.  Solve in
                # the already posed parent frame instead of applying the same
                # world-space correction independently to every child joint.
                posed = evaluate_profile_pose(bones, solved_rotations)
                base_rotation = posed[str(bone["name"])][:3, :3]
                rest_rotation = np.asarray(bone["matrix_local"], dtype=np.float64)[:3, :3]
                rest_direction = normalize_vector(np.asarray(bone["tail_local"]) - np.asarray(bone["head_local"]))
                local_direction = np.linalg.solve(rest_rotation, rest_direction)
                current_direction = normalize_vector(base_rotation @ local_direction)
                swing = quaternion_to_matrix(quaternion_from_vectors(current_direction, target))
                local_matrix = np.linalg.solve(base_rotation, swing @ base_rotation)
                local_quaternion = normalize_quaternion(quaternion_from_matrix(local_matrix))
                solved_rotations[str(bone["name"])] = local_quaternion
                actual_rotation = evaluate_profile_pose(bones, solved_rotations)[str(bone["name"])][:3, :3]
                actual_direction = normalize_vector(actual_rotation @ local_direction)
                straightness_errors[hand_key].append(float(np.degrees(np.arccos(np.clip(np.dot(actual_direction, target), -1.0, 1.0)))))
                correction = float(np.degrees(2.0 * np.arccos(np.clip(abs(local_quaternion[0]), 0.0, 1.0))))
                correction_angles[hand_key].append(correction)
                key = f"{finger.lower()}_{joint_index}"
                payload[hand_key][key] = {
                    "canonical_bone": canonical,
                    "avatar_bone": bone["name"],
                    "rotation_wxyz": [float(value) for value in local_quaternion],
                    "correction_degrees": correction,
                }

    side_metrics = {}
    for hand_key in ("left_hand", "right_hand"):
        directions = target_directions[hand_key]
        non_thumb = directions[1:]
        separations = [
            float(np.degrees(np.arccos(np.clip(np.dot(non_thumb[i], non_thumb[j]), -1.0, 1.0))))
            for i in range(len(non_thumb)) for j in range(i + 1, len(non_thumb))
        ]
        non_thumb_segments = target_segments[hand_key][1:]
        chain_clearances = [
            _sampled_segment_clearance(non_thumb_segments[i], non_thumb_segments[j])
            for i in range(len(non_thumb_segments)) for j in range(i + 1, len(non_thumb_segments))
        ]
        angles = correction_angles[hand_key]
        hand_name = bone_map["LeftHand" if hand_key == "left_hand" else "RightHand"]
        hand_scale = float(bones[hand_name]["length"])
        side_metrics[hand_key] = {
            "mapped_finger_bones": len(payload[hand_key]),
            "maximum_correction_degrees": max(angles, default=0.0),
            "mean_correction_degrees": float(np.mean(angles)) if angles else 0.0,
            "minimum_non_thumb_spread_degrees": min(separations, default=0.0),
            "minimum_non_thumb_chain_clearance": min(chain_clearances, default=0.0),
            "minimum_non_thumb_chain_clearance_normalized": min(chain_clearances, default=0.0) / max(hand_scale, 1e-8),
            "maximum_fk_straightness_error_degrees": max(straightness_errors[hand_key], default=0.0),
            "excessive_flexion": max(angles, default=0.0) > 85.0,
            "finger_intersection_risk": bool(chain_clearances and min(chain_clearances) / max(hand_scale, 1e-8) < 0.025),
            "impossible_joint_orientation": max(straightness_errors[hand_key], default=0.0) > 0.1,
        }
    symmetry_delta = abs(side_metrics["left_hand"]["mean_correction_degrees"] - side_metrics["right_hand"]["mean_correction_degrees"])
    status = "PASS"
    reasons: list[str] = []
    for side in ("left_hand", "right_hand"):
        if side_metrics[side]["excessive_flexion"] or side_metrics[side]["finger_intersection_risk"] or side_metrics[side]["impossible_joint_orientation"]:
            status = "REVIEW"
            reasons.append(f"{side} neutral calibration needs visual review.")
    if symmetry_delta > 25.0:
        status = "REVIEW"
        reasons.append("Left/right neutral corrections are not sufficiently symmetric.")
    payload["validation"] = {
        "status": status,
        "reasons": reasons,
        "left_hand": side_metrics["left_hand"],
        "right_hand": side_metrics["right_hand"],
        "left_right_mean_correction_delta_degrees": symmetry_delta,
    }
    validate_neutral_hand_pose(payload)
    return payload


def evaluate_profile_pose(bones: dict[str, dict[str, Any]], rotations: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Evaluate local pose deltas through the complete rest hierarchy, including helpers."""
    posed: dict[str, np.ndarray] = {}
    visiting: set[str] = set()

    def evaluate(name: str) -> np.ndarray:
        if name in posed:
            return posed[name]
        if name in visiting:
            raise ValueError(f"Cyclic avatar hierarchy at {name}.")
        visiting.add(name)
        bone = bones[name]
        rest = np.asarray(bone["matrix_local"], dtype=np.float64)
        if rest.shape != (4, 4) or not np.isfinite(rest).all():
            raise ValueError(f"Invalid avatar rest matrix for {name}.")
        parent = bone.get("parent")
        basis = rest if parent is None else evaluate(parent) @ np.linalg.solve(np.asarray(bones[parent]["matrix_local"]), rest)
        delta = np.eye(4, dtype=np.float64)
        if name in rotations:
            delta[:3, :3] = quaternion_to_matrix(rotations[name])
        posed[name] = basis @ delta
        visiting.remove(name)
        return posed[name]

    for bone_name in bones:
        evaluate(bone_name)
    return posed


def anatomical_palm_normal(across: np.ndarray, forward: np.ndarray, side: str) -> np.ndarray:
    """Index-minus-little cross forward is dorsal on Left and palmar on Right."""
    if side not in {"Left", "Right"}:
        raise ValueError(f"Unknown anatomical hand side: {side}")
    normal = normalize_vector(np.cross(across, forward))
    if not np.isfinite(normal).all() or np.linalg.norm(normal) < 1e-8:
        raise ValueError("Degenerate anatomical palm basis.")
    return normal * (-1.0 if side == "Left" else 1.0)


def _calibrate_medial_wrist(bones: dict[str, dict[str, Any]], mapping: dict[str, str], side: str, raw_palm_normal: np.ndarray) -> tuple[np.ndarray, float]:
    required = ("Hips", "Head", "LeftUpperArm", "RightUpperArm", f"{side}ForeArm", f"{side}Hand")
    if any(mapping.get(name) not in bones for name in required):
        raise ValueError("Neutral wrist calibration requires mapped head, hips, upper arms and forearm bones.")
    head = lambda name: np.asarray(bones[mapping[name]]["head_local"], dtype=np.float64)
    up = normalize_vector(head("Head") - head("Hips"))
    lateral = normalize_vector(head("LeftUpperArm") - head("RightUpperArm"))
    forearm_direction = normalize_vector(head(f"{side}Hand") - head(f"{side}ForeArm"))
    lower = quaternion_to_matrix(quaternion_from_vectors(forearm_direction, -up))
    hand_rotation = np.asarray(bones[mapping[f"{side}Hand"]]["matrix_local"], dtype=np.float64)[:3, :3]
    axis = normalize_vector(lower @ hand_rotation[:, 1])
    normal = lower @ raw_palm_normal * (-1.0 if side == "Left" else 1.0)
    inward = lateral * (-1.0 if side == "Left" else 1.0)
    normal = normalize_vector(normal - axis * np.dot(axis, normal))
    inward = normalize_vector(inward - axis * np.dot(axis, inward))
    if min(np.linalg.norm(up), np.linalg.norm(axis), np.linalg.norm(normal), np.linalg.norm(inward)) < 1e-8:
        raise ValueError("Degenerate body axes for anatomical wrist calibration.")
    angle = float(np.arctan2(np.dot(axis, np.cross(normal, inward)), np.dot(normal, inward)))
    return quaternion_from_axis_angle([0.0, 1.0, 0.0], angle), float(np.degrees(angle))


def _sampled_segment_clearance(
    first: tuple[np.ndarray, np.ndarray], second: tuple[np.ndarray, np.ndarray]
) -> float:
    samples = np.linspace(0.0, 1.0, 31, dtype=np.float64)[:, None]
    first_points = first[0] + samples * (first[1] - first[0])
    second_points = second[0] + samples * (second[1] - second[0])
    distances = np.linalg.norm(first_points[:, None, :] - second_points[None, :, :], axis=2)
    return float(np.min(distances))


def validate_neutral_hand_pose(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != NEUTRAL_SCHEMA_VERSION or payload.get("quaternion_order") != "WXYZ":
        raise ValueError(f"Neutral hand pose must use schema {NEUTRAL_SCHEMA_VERSION} and WXYZ quaternions.")
    wrist_pose = payload.get("wrist_pose")
    if not isinstance(wrist_pose, dict) or set(wrist_pose) != {"left_hand", "right_hand"}:
        raise ValueError("Neutral hand pose requires calibrated left/right wrist rotations.")
    expected_wrists = {"left_hand": "LeftHand", "right_hand": "RightHand"}
    canonical_bones: list[str] = []
    avatar_bones: list[str] = []
    for hand_key, expected_canonical in expected_wrists.items():
        entry = wrist_pose[hand_key]
        if not isinstance(entry, dict):
            raise ValueError(f"Neutral hand pose has an invalid {hand_key} wrist record.")
        if entry.get("canonical_bone") != expected_canonical:
            raise ValueError(
                f"Neutral hand pose wrist {hand_key} must map exactly to {expected_canonical}."
            )
        _require_nonblank_avatar_bone(entry, f"wrist_pose.{hand_key}")
        _validate_quaternion(entry.get("rotation_wxyz"), "wrist_pose")
        canonical_bones.append(expected_canonical)
        avatar_bones.append(entry["avatar_bone"])
    for hand_key in ("left_hand", "right_hand"):
        entries = payload.get(hand_key)
        side = "Left" if hand_key == "left_hand" else "Right"
        expected_entries = {
            f"{finger.lower()}_{joint}": f"{side}{finger}{joint}"
            for finger in FINGERS
            for joint in (1, 2, 3)
        }
        if not isinstance(entries, dict) or set(entries) != set(expected_entries):
            raise ValueError(
                f"Neutral hand pose requires the exact 15-key canonical map for {hand_key}."
            )
        for key, expected_canonical in expected_entries.items():
            entry = entries[key]
            if not isinstance(entry, dict):
                raise ValueError(f"Neutral hand pose has an invalid {hand_key}.{key} record.")
            if entry.get("canonical_bone") != expected_canonical:
                raise ValueError(
                    f"Neutral hand pose {hand_key}.{key} must map exactly to {expected_canonical}."
                )
            _require_nonblank_avatar_bone(entry, f"{hand_key}.{key}")
            _validate_quaternion(entry.get("rotation_wxyz"), f"{hand_key}.{key}")
            canonical_bones.append(expected_canonical)
            avatar_bones.append(entry["avatar_bone"])
    if len(canonical_bones) != len(set(canonical_bones)):
        raise ValueError("Neutral hand pose contains duplicate canonical bone mappings.")
    if len(avatar_bones) != len(set(avatar_bones)):
        raise ValueError("Neutral hand pose contains duplicate avatar bone mappings.")


def _require_nonblank_avatar_bone(entry: dict[str, Any], label: str) -> None:
    avatar_bone = entry.get("avatar_bone")
    if not isinstance(avatar_bone, str) or not avatar_bone.strip():
        raise ValueError(f"Neutral hand pose has no avatar bone for {label}.")


def _validate_quaternion(value: Any, label: str) -> None:
    values = np.asarray(value, dtype=np.float64)
    if values.shape != (4,) or not np.isfinite(values).all():
        raise ValueError(f"Invalid neutral quaternion for {label}")
    if abs(float(np.linalg.norm(values)) - 1.0) > 1e-4:
        raise ValueError(f"Non-unit neutral quaternion for {label}")


def neutral_rotations_by_canonical(payload: dict[str, Any]) -> dict[str, np.ndarray]:
    validate_neutral_hand_pose(payload)
    result: dict[str, np.ndarray] = {}
    for hand_key in ("left_hand", "right_hand"):
        for entry in payload[hand_key].values():
            result[str(entry["canonical_bone"])] = np.asarray(entry["rotation_wxyz"], dtype=np.float64)
    return result


def neutral_wrist_rotations_by_canonical(payload: dict[str, Any]) -> dict[str, np.ndarray]:
    validate_neutral_hand_pose(payload)
    return {
        str(entry["canonical_bone"]): np.asarray(entry["rotation_wxyz"], dtype=np.float64)
        for entry in payload["wrist_pose"].values()
    }


def neutral_finger_palm_rotations(payload: dict[str, Any], profile: dict, mapping: dict[str, str]) -> np.ndarray:
    """Evaluate the complete neutral chains in the common palm frame for fades."""
    bones = {str(item["name"]): item for item in profile["bones"]}
    rotations = {mapping[name]: value for name, value in neutral_rotations_by_canonical(payload).items()}
    posed = evaluate_profile_pose(bones, rotations)
    result = np.empty((2, 5, 3, 4), dtype=np.float64)
    for side_index, side in enumerate(("Left", "Right")):
        palm_inverse = np.linalg.inv(posed[mapping[f"{side}Hand"]][:3, :3])
        for finger_index, finger in enumerate(FINGERS):
            for joint_index in range(3):
                rotation = palm_inverse @ posed[mapping[f"{side}{finger}{joint_index + 1}"]][:3, :3]
                result[side_index, finger_index, joint_index] = quaternion_from_matrix(rotation)
    return result


def apply_boundary_neutral_pose(
    rotations: np.ndarray,
    bone_names: list[str],
    finger_quaternions: dict[str, np.ndarray],
    wrist_quaternions: dict[str, np.ndarray],
    left_hand_raw: np.ndarray,
    right_hand_raw: np.ndarray,
    wrist_transition_frames: int = 6,
    finger_transition_frames: int = 4,
) -> dict[str, Any]:
    """Use an anatomical fallback before/after reliable source hand tracking."""
    frame_count = rotations.shape[0]
    wrist_weights = np.zeros((frame_count, 2), dtype=np.float64)
    finger_weights = np.zeros((frame_count, 2), dtype=np.float64)
    details: dict[str, Any] = {}
    for side_index, (side, hand_raw) in enumerate((("Left", left_hand_raw), ("Right", right_hand_raw))):
        valid = np.isfinite(np.asarray(hand_raw)[..., :3]).all(axis=2).sum(axis=1) >= 12
        valid_indices = np.flatnonzero(valid)
        first_valid = int(valid_indices[0]) if valid_indices.size else frame_count
        stable_start = _first_stable_true_run(valid, 4)
        if stable_start is None:
            stable_start = first_valid if first_valid < frame_count else frame_count

        wrist_blend_start = max(0, stable_start - max(1, wrist_transition_frames))
        wrist_weights[:wrist_blend_start, side_index] = 1.0
        blend_length = stable_start - wrist_blend_start
        for frame_index in range(wrist_blend_start, stable_start):
            wrist_weights[frame_index, side_index] = 1.0 - ((frame_index - wrist_blend_start) / max(1, blend_length))

        finger_end = min(frame_count, first_valid + max(1, finger_transition_frames))
        finger_weights[:first_valid, side_index] = 1.0
        if first_valid > 0:
            for frame_index in range(first_valid, finger_end):
                finger_weights[frame_index, side_index] = 1.0 - ((frame_index - first_valid + 1) / max(1, finger_transition_frames))

        last_valid = int(valid_indices[-1]) if valid_indices.size else -1
        if last_valid >= 0:
            wrist_end = min(frame_count, last_valid + 1 + max(1, wrist_transition_frames))
            for frame_index in range(last_valid + 1, wrist_end):
                wrist_weights[frame_index, side_index] = max(
                    wrist_weights[frame_index, side_index],
                    (frame_index - last_valid) / max(1, wrist_transition_frames),
                )
            wrist_weights[wrist_end:, side_index] = 1.0

            finger_end_tail = min(frame_count, last_valid + 1 + max(1, finger_transition_frames))
            for frame_index in range(last_valid + 1, finger_end_tail):
                finger_weights[frame_index, side_index] = max(
                    finger_weights[frame_index, side_index],
                    (frame_index - last_valid) / max(1, finger_transition_frames),
                )
            finger_weights[finger_end_tail:, side_index] = 1.0

        for canonical, neutral in {**finger_quaternions, **wrist_quaternions}.items():
            if not canonical.startswith(side) or canonical not in bone_names:
                continue
            bone_index = bone_names.index(canonical)
            weights = wrist_weights[:, side_index] if canonical == f"{side}Hand" else finger_weights[:, side_index]
            for frame_index, weight in enumerate(weights):
                if weight > 0.0:
                    rotations[frame_index, bone_index] = quaternion_slerp(
                        rotations[frame_index, bone_index], neutral, float(weight)
                    )
        details[side.lower()] = {
            "first_valid_hand_frame": None if first_valid == frame_count else first_valid + 1,
            "first_stable_hand_frame": None if stable_start == frame_count else stable_start + 1,
            "full_neutral_wrist_frames": int(np.sum(wrist_weights[:, side_index] >= 0.999)),
            "wrist_transition_frames": int(np.sum((wrist_weights[:, side_index] > 0.0) & (wrist_weights[:, side_index] < 0.999))),
            "full_neutral_finger_frames": int(np.sum(finger_weights[:, side_index] >= 0.999)),
            "finger_transition_frames": int(np.sum((finger_weights[:, side_index] > 0.0) & (finger_weights[:, side_index] < 0.999))),
            "last_valid_hand_frame": None if last_valid < 0 else last_valid + 1,
            "full_neutral_ending_wrist_frames": int(np.sum(wrist_weights[last_valid + 1 :, side_index] >= 0.999)) if last_valid >= 0 else frame_count,
            "full_neutral_ending_finger_frames": int(np.sum(finger_weights[last_valid + 1 :, side_index] >= 0.999)) if last_valid >= 0 else frame_count,
        }
    return {"wrist_weights": wrist_weights, "finger_weights": finger_weights, "details": details}


# Backwards-compatible name retained for earlier callers.
apply_initial_neutral_pose = apply_boundary_neutral_pose


def _first_stable_true_run(values: np.ndarray, length: int) -> int | None:
    for start in range(max(0, len(values) - length + 1)):
        if bool(np.all(values[start : start + length])):
            return start
    return None


def detect_edge_neutral_frames(pose_points: np.ndarray, curls: np.ndarray, wrist_index: int, fps: float) -> np.ndarray:
    """Detect only quiet, open-hand frames at the beginning/end; never add frames."""
    frame_count = int(len(pose_points))
    mask = np.zeros(frame_count, dtype=bool)
    if frame_count == 0:
        return mask
    wrist = np.asarray(pose_points[:, wrist_index, :2], dtype=np.float64)
    velocity = np.full(frame_count, np.inf, dtype=np.float64)
    if frame_count > 1:
        delta = np.linalg.norm(np.diff(wrist, axis=0), axis=1)
        velocity[1:] = delta
        velocity[0] = delta[0]
    curl_values = np.asarray(curls, dtype=np.float64)
    curl_counts = np.isfinite(curl_values).sum(axis=(1, 2))
    curl_sums = np.nansum(curl_values, axis=(1, 2))
    mean_curl = np.divide(curl_sums, curl_counts, out=np.full(frame_count, np.nan), where=curl_counts > 0)
    valid = np.isfinite(wrist).all(axis=1) & np.isfinite(mean_curl)
    quiet_open = valid & (velocity <= 0.015) & (mean_curl <= np.radians(22.0))
    edge = max(1, min(frame_count, int(round(max(fps, 1.0) * 0.60))))
    mask[:edge] = quiet_open[:edge]
    mask[-edge:] = quiet_open[-edge:]
    return mask


def apply_neutral_pose(
    rotations: np.ndarray,
    bone_names: list[str],
    neutral_quaternions: dict[str, np.ndarray],
    left_mask: np.ndarray,
    right_mask: np.ndarray,
) -> int:
    applied = 0
    for frame_index in range(rotations.shape[0]):
        for side, active in (("Left", bool(left_mask[frame_index])), ("Right", bool(right_mask[frame_index]))):
            if not active:
                continue
            for canonical, quaternion in neutral_quaternions.items():
                if not canonical.startswith(side) or canonical not in bone_names:
                    continue
                rotations[frame_index, bone_names.index(canonical)] = quaternion
                applied += 1
    return applied
