"""Full-clip hand-versus-torso landmark clearance checks.

This module is deliberately a *non-mesh-aware* safety proxy.  It can detect a
hand landmark that is implausibly far behind an anatomical torso plane, but it
cannot prove that a skinned avatar mesh is collision free.  Production release
therefore still needs a mesh-aware or visual collision gate.

All input points must use the same Cartesian coordinate space.  The torso
normal is derived from labelled left/right shoulders and hips, so the check is
invariant to rigid camera/avatar rotation and translation.  Clearance is
normalised by the per-frame shoulder width.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


LEFT_SHOULDER = 11
RIGHT_SHOULDER = 12
LEFT_WRIST = 15
RIGHT_WRIST = 16
LEFT_HIP = 23
RIGHT_HIP = 24


@dataclass(frozen=True)
class TorsoFrame:
    """Orthonormal anatomical frame derived from one pose frame.

    ``origin`` is the hip midpoint.  ``lateral`` points from the labelled
    right shoulder toward the labelled left shoulder, ``up`` points from hips
    toward shoulders, and ``front`` is ``cross(lateral, up)``.
    """

    origin: np.ndarray
    lateral: np.ndarray
    up: np.ndarray
    front: np.ndarray
    shoulder_width: float
    torso_height: float

    def project(self, points: np.ndarray) -> np.ndarray:
        """Return lateral, vertical, and front clearance coordinates.

        Lateral and front coordinates are measured in shoulder widths.
        Vertical coordinates are measured in torso heights, where the hip
        midpoint is approximately 0 and shoulder midpoint approximately 1.
        """

        values = np.asarray(points, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] < 3:
            raise ValueError("points must have shape (point_count, 3+)")
        offsets = values[:, :3] - self.origin
        return np.column_stack(
            (
                offsets @ self.lateral / self.shoulder_width,
                offsets @ self.up / self.torso_height,
                offsets @ self.front / self.shoulder_width,
            )
        )


@dataclass(frozen=True)
class CollisionSafetyConfig:
    """Thresholds for the conservative torso-plane proxy.

    A positive clearance is in front of the torso plane.  A point in the
    central torso region below ``review_clearance`` needs inspection.  A point
    below ``fail_clearance`` is treated as deep penetration.
    """

    central_half_width: float = 0.45
    vertical_min: float = 0.0
    vertical_max: float = 1.0
    review_clearance: float = 0.04
    fail_clearance: float = -0.08
    minimum_evaluated_coverage: float = 0.90
    minimum_pose_visibility: float = 0.50
    minimum_axis_length: float = 1.0e-6
    use_pose_wrist_fallback: bool = False

    def validate(self) -> None:
        if self.central_half_width <= 0.0:
            raise ValueError("central_half_width must be positive")
        if self.vertical_min >= self.vertical_max:
            raise ValueError("vertical_min must be smaller than vertical_max")
        if self.fail_clearance >= self.review_clearance:
            raise ValueError("fail_clearance must be smaller than review_clearance")
        if not 0.0 <= self.minimum_evaluated_coverage <= 1.0:
            raise ValueError("minimum_evaluated_coverage must be between 0 and 1")
        if not 0.0 <= self.minimum_pose_visibility <= 1.0:
            raise ValueError("minimum_pose_visibility must be between 0 and 1")
        if self.minimum_axis_length <= 0.0:
            raise ValueError("minimum_axis_length must be positive")


def derive_anatomical_torso_frame(
    pose_landmarks: np.ndarray,
    *,
    minimum_visibility: float = 0.50,
    minimum_axis_length: float = 1.0e-6,
) -> TorsoFrame | None:
    """Build an anatomical torso frame, or return ``None`` if unavailable.

    MediaPipe pose indices 11, 12, 23, and 24 are used.  A fourth landmark
    column, when present and finite, is interpreted as visibility.
    """

    pose = np.asarray(pose_landmarks, dtype=np.float64)
    if pose.ndim != 2 or pose.shape[0] <= RIGHT_HIP or pose.shape[1] < 3:
        raise ValueError("pose_landmarks must have shape (at least 25, 3+)")

    anchor_indices = (LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_HIP, RIGHT_HIP)
    anchors: list[np.ndarray] = []
    for index in anchor_indices:
        landmark = pose[index]
        if not np.isfinite(landmark[:3]).all():
            return None
        if landmark.shape[0] >= 4 and np.isfinite(landmark[3]) and landmark[3] < minimum_visibility:
            return None
        anchors.append(landmark[:3])

    left_shoulder, right_shoulder, left_hip, right_hip = anchors
    shoulder_midpoint = 0.5 * (left_shoulder + right_shoulder)
    hip_midpoint = 0.5 * (left_hip + right_hip)

    lateral_seed = left_shoulder - right_shoulder
    shoulder_width = float(np.linalg.norm(lateral_seed))
    if not np.isfinite(shoulder_width) or shoulder_width <= minimum_axis_length:
        return None
    lateral = lateral_seed / shoulder_width

    up_seed = shoulder_midpoint - hip_midpoint
    up_orthogonal = up_seed - lateral * float(np.dot(up_seed, lateral))
    torso_height = float(np.linalg.norm(up_orthogonal))
    if not np.isfinite(torso_height) or torso_height <= minimum_axis_length:
        return None
    up = up_orthogonal / torso_height

    front = np.cross(lateral, up)
    front_length = float(np.linalg.norm(front))
    if not np.isfinite(front_length) or front_length <= minimum_axis_length:
        return None
    front /= front_length

    return TorsoFrame(
        origin=hip_midpoint.copy(),
        lateral=lateral,
        up=up,
        front=front,
        shoulder_width=shoulder_width,
        torso_height=torso_height,
    )


def evaluate_torso_hand_clearance(
    pose_points: np.ndarray,
    left_hand_points: np.ndarray | Sequence[np.ndarray | None] | None = None,
    right_hand_points: np.ndarray | Sequence[np.ndarray | None] | None = None,
    *,
    frame_indices: Sequence[int] | np.ndarray | None = None,
    config: CollisionSafetyConfig | None = None,
) -> dict[str, object]:
    """Evaluate every input frame for central-torso hand penetration.

    ``pose_points`` has shape ``(frames, 25+, 3+)``.  Hand inputs have shape
    ``(frames, points, 3+)`` or may be per-frame sequences containing ``None``.
    Palm and finger landmarks are all evaluated when supplied.  Optionally, a
    pose wrist can be used only when that side's detailed hand is unavailable.

    The returned dictionary is JSON-safe.  Its overall status is ``FAIL`` for
    any deep-penetration frame, ``REVIEW`` for shallow clearance or inadequate
    evaluable coverage, and otherwise ``PASS``.
    """

    settings = config or CollisionSafetyConfig()
    settings.validate()

    poses = np.asarray(pose_points, dtype=np.float64)
    if poses.ndim != 3 or poses.shape[1] <= RIGHT_HIP or poses.shape[2] < 3:
        raise ValueError("pose_points must have shape (frames, at least 25, 3+)")
    frame_count = int(poses.shape[0])
    left_frames = _normalise_hand_frames(left_hand_points, frame_count, "left_hand_points")
    right_frames = _normalise_hand_frames(right_hand_points, frame_count, "right_hand_points")
    indices = _normalise_frame_indices(frame_indices, frame_count)

    side_metrics: dict[str, dict[str, object]] = {
        "left": _empty_side_metrics(),
        "right": _empty_side_metrics(),
    }
    frame_results: list[dict[str, object]] = []
    review_frames: list[int] = []
    fail_frames: list[int] = []
    torso_valid_count = 0
    hand_observed_count = 0
    evaluated_count = 0
    central_frame_count = 0
    pass_count = 0
    review_count = 0
    fail_count = 0
    minimum_clearance: float | None = None

    for position in range(frame_count):
        frame_index = indices[position]
        left, left_source = _points_for_side(
            left_frames[position],
            poses[position],
            LEFT_WRIST,
            settings.use_pose_wrist_fallback,
            settings.minimum_pose_visibility,
        )
        right, right_source = _points_for_side(
            right_frames[position],
            poses[position],
            RIGHT_WRIST,
            settings.use_pose_wrist_fallback,
            settings.minimum_pose_visibility,
        )
        observed = bool(left.size or right.size)
        if observed:
            hand_observed_count += 1

        torso = derive_anatomical_torso_frame(
            poses[position],
            minimum_visibility=settings.minimum_pose_visibility,
            minimum_axis_length=settings.minimum_axis_length,
        )
        if torso is None:
            frame_results.append(
                _frame_result(frame_index, "NOT_EVALUATED", "invalid_or_low_visibility_torso", 0, None)
            )
            continue
        torso_valid_count += 1

        if not observed:
            frame_results.append(_frame_result(frame_index, "NOT_EVALUATED", "no_finite_hand_points", 0, None))
            continue

        evaluated_count += 1
        frame_central_count = 0
        frame_minimum: float | None = None

        for side, points, source in (("left", left, left_source), ("right", right, right_source)):
            if not points.size:
                continue
            side_data = side_metrics[side]
            side_data["observed_points"] = int(side_data["observed_points"]) + int(points.shape[0])
            side_data[f"{source}_frames"] = int(side_data[f"{source}_frames"]) + 1

            projected = torso.project(points)
            central_mask = (
                (np.abs(projected[:, 0]) <= settings.central_half_width)
                & (projected[:, 1] >= settings.vertical_min)
                & (projected[:, 1] <= settings.vertical_max)
            )
            clearances = projected[central_mask, 2]
            central_count = int(clearances.size)
            side_data["central_points"] = int(side_data["central_points"]) + central_count
            frame_central_count += central_count
            if central_count == 0:
                continue

            side_minimum = float(np.min(clearances))
            current_side_minimum = side_data["minimum_normalized_clearance"]
            if current_side_minimum is None or side_minimum < float(current_side_minimum):
                side_data["minimum_normalized_clearance"] = side_minimum
            if frame_minimum is None or side_minimum < frame_minimum:
                frame_minimum = side_minimum
            if minimum_clearance is None or side_minimum < minimum_clearance:
                minimum_clearance = side_minimum

            if side_minimum < settings.review_clearance:
                _append_unique(side_data["risky_frame_indices"], frame_index)
            if side_minimum < settings.fail_clearance:
                _append_unique(side_data["fail_frame_indices"], frame_index)

        if frame_central_count:
            central_frame_count += 1
        if frame_minimum is not None and frame_minimum < settings.fail_clearance:
            frame_status = "FAIL"
            reason = "deep_landmark_penetration"
            fail_count += 1
            fail_frames.append(frame_index)
        elif frame_minimum is not None and frame_minimum < settings.review_clearance:
            frame_status = "REVIEW"
            reason = "low_front_depth_clearance"
            review_count += 1
            review_frames.append(frame_index)
        else:
            frame_status = "PASS"
            reason = "no_central_points" if frame_minimum is None else "clearance_above_review_threshold"
            pass_count += 1
        frame_results.append(
            _frame_result(frame_index, frame_status, reason, frame_central_count, frame_minimum)
        )

    evaluated_coverage = _coverage(evaluated_count, frame_count)
    warnings: list[str] = []
    if evaluated_coverage < settings.minimum_evaluated_coverage:
        warnings.append(
            "Evaluated frame coverage is below the configured minimum; unobserved frames require review."
        )
    if evaluated_count > 0 and central_frame_count == 0:
        warnings.append("No supplied hand landmarks entered the configured central torso region.")
    if settings.use_pose_wrist_fallback:
        warnings.append("Pose-wrist fallback does not provide palm or finger collision coverage.")

    if fail_frames:
        overall_status = "FAIL"
    elif review_frames or evaluated_coverage < settings.minimum_evaluated_coverage:
        overall_status = "REVIEW"
    else:
        overall_status = "PASS"

    risky_frames = sorted(set(review_frames + fail_frames))
    return {
        "schema_version": "1.0",
        "check_name": "full_clip_torso_hand_clearance_proxy",
        "status": overall_status,
        "mesh_aware": False,
        "evaluated_every_input_frame": True,
        "coordinate_space_note": (
            "Pose, palm, and finger points must share one Cartesian coordinate space; "
            "positive clearance follows cross(right-to-left shoulder, hips-to-shoulders)."
        ),
        "frames": {
            "total": frame_count,
            "torso_frame_valid": torso_valid_count,
            "hand_observed": hand_observed_count,
            "evaluated": evaluated_count,
            "central_region": central_frame_count,
            "pass": pass_count,
            "review": review_count,
            "fail": fail_count,
            "torso_frame_coverage": _coverage(torso_valid_count, frame_count),
            "hand_observation_coverage": _coverage(hand_observed_count, frame_count),
            "evaluated_coverage": evaluated_coverage,
        },
        "thresholds": {
            "normalization": "per_frame_shoulder_width",
            "central_half_width": settings.central_half_width,
            "vertical_min": settings.vertical_min,
            "vertical_max": settings.vertical_max,
            "review_clearance": settings.review_clearance,
            "fail_clearance": settings.fail_clearance,
            "minimum_evaluated_coverage": settings.minimum_evaluated_coverage,
        },
        "minimum_normalized_clearance": minimum_clearance,
        "risky_frame_indices": risky_frames,
        "review_frame_indices": review_frames,
        "fail_frame_indices": fail_frames,
        "sides": side_metrics,
        "frame_results": frame_results,
        "warnings": warnings,
        "limitations": [
            "This is an anatomical landmark proxy, not a mesh-aware collision test.",
            "It does not test hand-hand, finger-finger, clothing, skinning, or geometry between landmarks.",
            "Monocular landmark depth is ambiguous during occlusion and intentional body contact.",
            "A PASS cannot by itself certify a production GLB as collision free.",
        ],
    }


def _normalise_hand_frames(
    values: np.ndarray | Sequence[np.ndarray | None] | None,
    frame_count: int,
    name: str,
) -> list[np.ndarray | None]:
    if values is None:
        return [None] * frame_count
    if isinstance(values, np.ndarray):
        array = np.asarray(values, dtype=np.float64)
        if array.ndim != 3 or array.shape[0] != frame_count or array.shape[2] < 3:
            raise ValueError(f"{name} must have shape (frames, points, 3+)")
        return [array[index] for index in range(frame_count)]

    frames = list(values)
    if len(frames) != frame_count:
        raise ValueError(f"{name} must contain exactly {frame_count} frames")
    normalised: list[np.ndarray | None] = []
    for value in frames:
        if value is None:
            normalised.append(None)
            continue
        array = np.asarray(value, dtype=np.float64)
        if array.ndim != 2 or array.shape[1] < 3:
            raise ValueError(f"each {name} frame must have shape (points, 3+)")
        normalised.append(array)
    return normalised


def _normalise_frame_indices(
    frame_indices: Sequence[int] | np.ndarray | None,
    frame_count: int,
) -> list[int]:
    if frame_indices is None:
        return list(range(frame_count))
    if len(frame_indices) != frame_count:
        raise ValueError(f"frame_indices must contain exactly {frame_count} values")
    return [int(value) for value in frame_indices]


def _points_for_side(
    hand: np.ndarray | None,
    pose: np.ndarray,
    wrist_index: int,
    use_pose_wrist_fallback: bool,
    minimum_pose_visibility: float,
) -> tuple[np.ndarray, str]:
    if hand is not None:
        finite = np.isfinite(hand[:, :3]).all(axis=1)
        if finite.any():
            return np.asarray(hand[finite, :3], dtype=np.float64), "detailed_hand"
    wrist = pose[wrist_index]
    wrist_visible = not (
        wrist.shape[0] >= 4
        and np.isfinite(wrist[3])
        and wrist[3] < minimum_pose_visibility
    )
    if use_pose_wrist_fallback and np.isfinite(wrist[:3]).all() and wrist_visible:
        return np.asarray(pose[wrist_index : wrist_index + 1, :3], dtype=np.float64), "pose_wrist_fallback"
    return np.empty((0, 3), dtype=np.float64), "detailed_hand"


def _empty_side_metrics() -> dict[str, object]:
    return {
        "observed_points": 0,
        "central_points": 0,
        "detailed_hand_frames": 0,
        "pose_wrist_fallback_frames": 0,
        "minimum_normalized_clearance": None,
        "risky_frame_indices": [],
        "fail_frame_indices": [],
    }


def _frame_result(
    frame_index: int,
    status: str,
    reason: str,
    central_point_count: int,
    minimum_clearance: float | None,
) -> dict[str, object]:
    return {
        "frame_index": int(frame_index),
        "status": status,
        "reason": reason,
        "central_point_count": int(central_point_count),
        "minimum_normalized_clearance": minimum_clearance,
    }


def _append_unique(values: object, frame_index: int) -> None:
    if not isinstance(values, list):
        raise TypeError("internal frame-index collection must be a list")
    if not values or values[-1] != frame_index:
        values.append(frame_index)


def _coverage(count: int, total: int) -> float:
    return float(count / total) if total else 0.0
