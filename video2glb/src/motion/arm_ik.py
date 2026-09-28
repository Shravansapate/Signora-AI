"""Analytic two-bone endpoint correction without arm stretching."""

from __future__ import annotations

from typing import Any

import numpy as np


def solve_two_bone_endpoint(
    shoulder: np.ndarray,
    old_elbow: np.ndarray,
    old_wrist: np.ndarray,
    desired_wrist: np.ndarray,
    upper_length: float,
    forearm_length: float,
    *,
    bend_direction_hint: np.ndarray | None = None,
) -> dict[str, Any]:
    """Return a length-preserving elbow/wrist pose for a requested endpoint.

    The old bend direction is parallel-transported with the shoulder-to-wrist
    axis, preserving its side without a pole-angle flip.  Near a straight arm,
    a caller may carry the returned ``bend_direction`` into the next call.
    Without observable bend or a usable hint the fallback is deterministic and
    explicitly marked ambiguous.  Unreachable endpoints are clamped to the
    exact reach interval, reported as REVIEW, and never silently stretched.
    """
    origin = _vector(shoulder, "shoulder")
    previous_elbow = _vector(old_elbow, "old_elbow")
    previous_wrist = _vector(old_wrist, "old_wrist")
    requested = _vector(desired_wrist, "desired_wrist")
    lengths = np.asarray([upper_length, forearm_length], dtype=np.float64)
    if not np.isfinite(lengths).all() or np.any(lengths <= 0.0):
        raise ValueError("Upper-arm and forearm lengths must be finite and positive.")
    upper, forearm = (float(value) for value in lengths)
    scale = max(upper, forearm)
    tolerance = max(scale * 1e-10, np.finfo(np.float64).tiny)
    hint = _vector(bend_direction_hint, "bend_direction_hint") if bend_direction_hint is not None else None

    old_offset = previous_wrist - origin
    old_distance = float(np.linalg.norm(old_offset))
    elbow_offset = previous_elbow - origin
    old_axis = old_offset / old_distance if old_distance > tolerance else None
    target_offset = requested - origin
    requested_distance = float(np.linalg.norm(target_offset))
    if requested_distance > tolerance:
        target_axis = target_offset / requested_distance
    elif old_axis is not None:
        target_axis = old_axis.copy()
    elif np.linalg.norm(elbow_offset) > tolerance:
        target_axis = elbow_offset / np.linalg.norm(elbow_offset)
    else:
        target_axis = np.array([1.0, 0.0, 0.0])
    if old_axis is None:
        old_axis = target_axis.copy()

    old_bend = elbow_offset - old_axis * float(np.dot(elbow_offset, old_axis))
    bend_length = float(np.linalg.norm(old_bend))
    ambiguity_limit = upper * 1e-6
    ambiguous = False
    bend_source = "old_elbow_plane"
    if bend_length <= ambiguity_limit:
        if hint is not None:
            old_bend = hint - old_axis * float(np.dot(hint, old_axis))
            bend_length = float(np.linalg.norm(old_bend))
        if hint is not None and bend_length > max(float(np.linalg.norm(hint)) * 1e-8, tolerance):
            bend_source = "previous_bend_direction_hint"
        else:
            # There is no measured pole side in an exactly straight arm.  A
            # deterministic transverse axis keeps results finite; reporting the
            # ambiguity prevents treating that arbitrary side as source evidence.
            seed = np.eye(3)[int(np.argmin(np.abs(old_axis)))]
            old_bend = seed - old_axis * float(np.dot(seed, old_axis))
            bend_length = float(np.linalg.norm(old_bend))
            bend_source = "unobserved_deterministic_fallback"
            ambiguous = True
    old_bend /= bend_length
    bend = _transport_bend(old_axis, target_axis, old_bend)
    bend -= target_axis * float(np.dot(bend, target_axis))
    bend /= np.linalg.norm(bend)

    minimum_reach = abs(upper - forearm)
    maximum_reach = upper + forearm
    distance = float(np.clip(requested_distance, minimum_reach, maximum_reach))
    reachable = minimum_reach - tolerance <= requested_distance <= maximum_reach + tolerance
    solved_wrist = origin + target_axis * distance
    singularity = None
    if distance <= tolerance:
        # Equal-length bones can fold completely with wrist at shoulder.  Keep
        # the previous elbow direction instead of dividing by zero in the
        # triangle-intersection formula.
        elbow_direction = elbow_offset.copy()
        if np.linalg.norm(elbow_direction) <= tolerance:
            elbow_direction = bend.copy()
        elbow_direction /= np.linalg.norm(elbow_direction)
        solved_elbow = origin + elbow_direction * upper
        singularity = "wrist_at_shoulder"
    else:
        # Work in normalized units to avoid squaring very large rig scales.
        normalized_distance = distance / scale
        normalized_upper, normalized_forearm = upper / scale, forearm / scale
        along = (normalized_upper**2 - normalized_forearm**2 + normalized_distance**2) / (2.0 * normalized_distance)
        radius = np.sqrt(max(0.0, normalized_upper**2 - along**2))
        solved_elbow = origin + scale * (target_axis * along + bend * radius)
        if radius <= 1e-8:
            singularity = "collinear_arm"

    actual_upper = float(np.linalg.norm(solved_elbow - origin))
    actual_forearm = float(np.linalg.norm(solved_wrist - solved_elbow))
    endpoint_error = float(np.linalg.norm(solved_wrist - requested))
    reasons = []
    if not reachable:
        reasons.append("Requested wrist lies outside the exact two-bone reach interval; the returned wrist is the nearest feasible endpoint.")
    if ambiguous:
        reasons.append("The old arm has no observable bend side and no usable bend-direction hint.")
    return {
        "status": "REVIEW" if reasons else "PASS",
        "reasons": reasons,
        "reachable": bool(reachable),
        "elbow": solved_elbow.tolist(),
        "wrist": solved_wrist.tolist(),
        "requested_wrist": requested.tolist(),
        "endpoint_error": endpoint_error,
        "endpoint_error_normalized": endpoint_error / (upper + forearm),
        "bend_direction": bend.tolist(),
        "bend_source": bend_source,
        "bend_plane_ambiguous": bool(ambiguous),
        "singularity": singularity,
        "upper_length": upper,
        "forearm_length": forearm,
        "actual_upper_length": actual_upper,
        "actual_forearm_length": actual_forearm,
        "maximum_length_error": max(abs(actual_upper - upper), abs(actual_forearm - forearm)),
        "reach_interval": [minimum_reach, maximum_reach],
    }


def _transport_bend(old_axis: np.ndarray, target_axis: np.ndarray, old_bend: np.ndarray) -> np.ndarray:
    cosine = float(np.clip(np.dot(old_axis, target_axis), -1.0, 1.0))
    if cosine < -1.0 + 1e-6:
        # The shortest rotation axis is indeterminate at 180 degrees.  Preserve
        # the old radial side directly; its projected length is near one here.
        return old_bend.copy()
    cross = np.cross(old_axis, target_axis)
    return old_bend + np.cross(cross, old_bend) + np.cross(cross, np.cross(cross, old_bend)) / (1.0 + cosine)


def _vector(value: np.ndarray, name: str) -> np.ndarray:
    result = np.array(value, dtype=np.float64, copy=True)
    if result.shape != (3,) or not np.isfinite(result).all():
        raise ValueError(f"{name} must be a finite three-dimensional vector.")
    return result
