from __future__ import annotations

import numpy as np


def evaluate_neutral_finger_shape(segment_directions: np.ndarray) -> dict:
    """Check actual posed phalanges, independently of calibration correction angles."""
    values = np.asarray(segment_directions, dtype=np.float64)
    if values.shape != (5, 3, 3) or not np.isfinite(values).all():
        return {"status": "FAIL", "reasons": ["Neutral finger geometry is missing or non-finite."]}
    lengths = np.linalg.norm(values, axis=2, keepdims=True)
    if np.any(lengths < 1e-8):
        return {"status": "FAIL", "reasons": ["Neutral finger geometry has a zero-length segment."]}
    values = values / lengths
    bends = np.degrees(np.arccos(np.clip(np.sum(values[:, 1:] * values[:, :-1], axis=2), -1.0, 1.0)))
    maximum = float(np.max(bends))
    status = "FAIL" if maximum > 35.0 else ("REVIEW" if maximum > 15.0 else "PASS")
    return {
        "status": status,
        "reasons": ["The exported neutral fingers remain bent relative to adjacent phalanges."] if status != "PASS" else [],
        "maximum_adjacent_bend_degrees": maximum,
        "finger_maximum_bend_degrees": {
            finger: float(np.max(bends[index])) for index, finger in enumerate(("Thumb", "Index", "Middle", "Ring", "Little"))
        },
        "thresholds": {"review_bend_degrees": 15.0, "fail_bend_degrees": 35.0},
    }
