"""Small, measured depth corrections for confirmed hand/torso intersections.

This helper does not decide whether a point is inside the body.  Its caller
must supply that evidence from the evaluated mesh collision check.  It solves
only a translation along the anatomical front axis, keeping the sign's image
plane trajectory and hand shape intact.  Large or unsupported corrections are
returned for review without permission to apply them automatically.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def supported_face_projection(point, nearest, normal, shoulder_width: float) -> bool:
    """A triangle's extended plane is not evidence past its finite surface.

    Closest points on edges/corners can have large tangential displacement.
    Their signed normal distance alone cannot prove penetration of an open
    torso selection; keep these samples uncertain, never silently passing.
    """
    delta = np.asarray(point, dtype=np.float64) - np.asarray(nearest, dtype=np.float64)
    axis = np.asarray(normal, dtype=np.float64)
    if not np.isfinite(delta).all() or not np.isfinite(axis).all() or shoulder_width <= 0:
        return False
    length = np.linalg.norm(axis)
    if length < 1e-8:
        return False
    axis = axis / length
    tangent = delta - axis * np.dot(delta, axis)
    return bool(np.linalg.norm(tangent) <= shoulder_width * 0.05)


def clearance_envelope(required: np.ndarray, radius: int) -> np.ndarray:
    """A compact raised-cosine upper envelope: never undershoot required depth."""
    values = np.asarray(required, dtype=np.float64)
    if values.ndim != 2 or not np.isfinite(values).all() or np.any(values < 0) or radius < 1:
        raise ValueError("Clearance envelope requires finite nonnegative FxC values and a positive radius.")
    result = values.copy()
    for offset in range(1, min(int(radius) + 1, len(values))):
        weight = 0.5 * (1.0 + np.cos(np.pi * offset / radius))
        result[offset:] = np.maximum(result[offset:], values[:-offset] * weight)
        result[:-offset] = np.maximum(result[:-offset], values[offset:] * weight)
    return result


def minimum_front_surface_correction(
    hand_points: np.ndarray,
    torso_vertices: np.ndarray,
    torso_triangles: np.ndarray,
    anatomical_front: np.ndarray,
    shoulder_width: float,
    *,
    penetrating_mask: np.ndarray,
    maximum_automatic_shift_ratio: float = 0.025,
    clearance_ratio: float = 0.0,
) -> dict[str, Any]:
    """Find the least common forward shift clearing confirmed penetrating points.

    All geometry and ``anatomical_front`` must share one Cartesian space.
    Triangle winding must point outward.  At each affected point's lateral and
    vertical position the outermost front-facing triangle supplies the target
    depth.  Only the *whole hand's* translation is proposed; fingers are never
    independently pushed apart.  Zero clearance preserves intentional contact.
    """
    points = _points(hand_points, "hand_points")
    vertices = _points(torso_vertices, "torso_vertices")
    raw_triangles = np.asarray(torso_triangles)
    if raw_triangles.ndim != 2 or raw_triangles.shape[1:] != (3,) or not np.issubdtype(raw_triangles.dtype, np.integer):
        raise ValueError("torso_triangles must contain integer triangle indices with shape Tx3.")
    triangles = raw_triangles.astype(np.int64, copy=False)
    if np.any(triangles < 0) or np.any(triangles >= len(vertices)):
        raise ValueError("Torso triangle indices are outside the vertex array.")
    mask = np.asarray(penetrating_mask)
    if mask.dtype != np.bool_ or mask.shape != (len(points),):
        raise ValueError("penetrating_mask must be a Boolean value for every hand point.")
    front = np.array(anatomical_front, dtype=np.float64, copy=True)
    if front.shape != (3,) or not np.isfinite(front).all() or np.linalg.norm(front) < 1e-8:
        raise ValueError("anatomical_front must be a finite nonzero direction.")
    front /= np.linalg.norm(front)
    if not np.isfinite(shoulder_width) or shoulder_width <= 0.0:
        raise ValueError("shoulder_width must be finite and positive.")
    if not np.isfinite(maximum_automatic_shift_ratio) or maximum_automatic_shift_ratio < 0.0:
        raise ValueError("maximum_automatic_shift_ratio must be finite and nonnegative.")
    if not np.isfinite(clearance_ratio) or clearance_ratio < 0.0:
        raise ValueError("clearance_ratio must be finite and nonnegative.")

    selected = points[mask]
    result: dict[str, Any] = {
        "status": "PASS",
        "reasons": [],
        "confirmed_penetrating_point_count": int(len(selected)),
        "supported_point_count": 0,
        "correction_required": False,
        "automatic_correction_allowed": True,
        "translation": [0.0, 0.0, 0.0],
        "proposed_translation": [0.0, 0.0, 0.0],
        "required_shift": 0.0,
        "required_shift_shoulder_widths": 0.0,
        "preserves_lateral_and_vertical_coordinates": True,
        "requires_evaluated_mesh_revalidation_after_application": True,
        "thresholds": {
            "maximum_automatic_shift_shoulder_widths": float(maximum_automatic_shift_ratio),
            "surface_clearance_shoulder_widths": float(clearance_ratio),
        },
    }
    if not len(selected):
        return result

    # A stable transverse basis avoids assuming the avatar's front is world Z.
    seed = np.eye(3)[int(np.argmin(np.abs(front)))]
    horizontal = np.cross(front, seed)
    horizontal /= np.linalg.norm(horizontal)
    vertical = np.cross(front, horizontal)
    basis = np.stack((horizontal, vertical, front), axis=1)
    # Subtract an origin before projecting to retain precision in translated scenes.
    origin = vertices[0] if len(vertices) else np.zeros(3)
    projected_vertices = (vertices - origin) @ basis
    projected_points = (selected - origin) @ basis
    faces = projected_vertices[triangles]
    first = faces[:, 1, :2] - faces[:, 0, :2]
    second = faces[:, 2, :2] - faces[:, 0, :2]
    determinants = first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]
    # This signed projected area is dot(outward triangle normal, front).
    facing = determinants > max(shoulder_width * shoulder_width * 1e-12, 1e-16)
    faces, first, second, determinants = faces[facing], first[facing], second[facing], determinants[facing]
    depths = np.full(len(selected), np.nan, dtype=np.float64)
    for start in range(0, len(selected), 128):
        stop = min(start + 128, len(selected))
        delta = projected_points[start:stop, None, :2] - faces[None, :, 0, :2]
        u = (delta[:, :, 0] * second[None, :, 1] - delta[:, :, 1] * second[None, :, 0]) / determinants[None, :]
        v = (first[None, :, 0] * delta[:, :, 1] - first[None, :, 1] * delta[:, :, 0]) / determinants[None, :]
        covered = (u >= -1e-8) & (v >= -1e-8) & (u + v <= 1.0 + 1e-8)
        hit_depths = faces[None, :, 0, 2] + u * (faces[None, :, 1, 2] - faces[None, :, 0, 2]) + v * (faces[None, :, 2, 2] - faces[None, :, 0, 2])
        for index, has_hit in enumerate(np.any(covered, axis=1)):
            if has_hit:
                depths[start + index] = float(np.max(hit_depths[index, covered[index]]))

    supported = np.isfinite(depths)
    result["supported_point_count"] = int(supported.sum())
    if not supported.any():
        result.update({
            "status": "REVIEW",
            "reasons": ["Confirmed penetrating hand points have no reliable front-facing torso surface at the same lateral/vertical coordinates."],
            "automatic_correction_allowed": False,
            "required_shift": None,
            "required_shift_shoulder_widths": None,
        })
        return result

    valid_depths = depths[supported]
    valid_points = projected_points[supported, 2]
    required = max(0.0, float(np.max(valid_depths + clearance_ratio * shoulder_width - valid_points)))
    ratio = required / shoulder_width
    translation = front * required
    result.update({
        "correction_required": required > shoulder_width * 1e-8,
        "proposed_translation": translation.tolist(),
        "required_shift": required,
        "required_shift_shoulder_widths": ratio,
    })
    reasons = []
    if not supported.all():
        reasons.append("Some penetrating hand points extend past front-facing torso surface boundaries.")
    if ratio > maximum_automatic_shift_ratio + 1e-8:
        reasons.append("The required forward shift exceeds the automatic correction limit; source depth or manual animation needs review.")
        result.update({
            "status": "REVIEW",
            "reasons": reasons,
            "automatic_correction_allowed": False,
        })
    else:
        result.update({
            "status": "REVIEW" if reasons else "PASS",
            "reasons": reasons,
        })
        result["translation"] = translation.tolist()
    return result


def _points(value: np.ndarray, name: str) -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if result.ndim != 2 or result.shape[1:] != (3,) or not np.isfinite(result).all():
        raise ValueError(f"{name} must have finite shape Nx3.")
    return result
