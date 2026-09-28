from __future__ import annotations

from typing import Any

import numpy as np


def evaluate_root_drift(
    expected_root_translation: np.ndarray,
    armature_origins: np.ndarray,
    hip_positions: np.ndarray,
    normalization_scale: float,
    *,
    maximum_normalized_drift: float = 1e-4,
) -> dict[str, Any]:
    """Fail when a nominally in-place animation translates its rig or hips."""

    arrays = {
        "expected_root_translation": np.asarray(expected_root_translation, dtype=np.float64),
        "armature_origins": np.asarray(armature_origins, dtype=np.float64),
        "hip_positions": np.asarray(hip_positions, dtype=np.float64),
    }
    reasons: list[str] = []
    frame_count: int | None = None
    for name, values in arrays.items():
        if values.ndim != 2 or values.shape[1:] != (3,) or values.shape[0] <= 0:
            reasons.append(f"{name} must contain at least one Nx3 sample.")
            continue
        if not np.isfinite(values).all():
            reasons.append(f"{name} contains NaN or Infinity.")
        if frame_count is None:
            frame_count = int(values.shape[0])
        elif values.shape[0] != frame_count:
            reasons.append("Root-drift sample counts do not match.")

    scale = float(normalization_scale)
    if not np.isfinite(scale) or scale <= 1e-8:
        reasons.append("Root-drift normalization scale is invalid.")
        scale = 1.0

    def maximum_displacement(values: np.ndarray) -> float | None:
        if values.ndim != 2 or values.shape[1:] != (3,) or values.shape[0] <= 0:
            return None
        if not np.isfinite(values).all():
            return None
        return float(np.max(np.linalg.norm(values - values[0], axis=1)))

    expected = maximum_displacement(arrays["expected_root_translation"])
    armature = maximum_displacement(arrays["armature_origins"])
    hips = maximum_displacement(arrays["hip_positions"])
    normalized = {
        "expected": expected / scale if expected is not None else None,
        "armature": armature / scale if armature is not None else None,
        "hips": hips / scale if hips is not None else None,
    }
    for name, value in normalized.items():
        if value is not None and value > maximum_normalized_drift:
            reasons.append(
                f"{name} root drift {value:.8f} exceeds {maximum_normalized_drift:.8f}."
            )

    return {
        "status": "FAIL" if reasons else "PASS",
        "reasons": reasons,
        "frame_count": frame_count,
        "normalization_scale": scale,
        "maximum_normalized_drift": normalized,
        "threshold": maximum_normalized_drift,
        "root_motion_policy": "IN_PLACE",
    }
