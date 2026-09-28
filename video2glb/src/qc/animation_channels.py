from __future__ import annotations

from typing import Any, Sequence

import numpy as np


def evaluate_required_rotation_channels(
    rotations_wxyz: np.ndarray,
    canonical_names: Sequence[str],
    animated_names: set[str],
    procedural_motion_names: set[str] | None = None,
) -> dict[str, Any]:
    """Require varying joints to be animated; glTF may store constant poses on nodes."""
    values = np.asarray(rotations_wxyz, dtype=np.float64)
    if values.ndim != 3 or values.shape[1:] != (len(canonical_names), 4) or len(values) == 0 or not np.isfinite(values).all():
        return {"status": "FAIL", "reasons": ["Invalid expected rotations for animation channel coverage."]}
    norms = np.linalg.norm(values, axis=2, keepdims=True)
    if np.any(norms < 1e-8):
        return {"status": "FAIL", "reasons": ["Zero expected quaternion in animation channel coverage."]}
    values /= norms
    variation = np.degrees(2.0 * np.arccos(np.clip(np.abs(np.sum(values * values[:1], axis=2)), 0.0, 1.0)))
    varying = {name for index, name in enumerate(canonical_names) if float(np.max(variation[:, index])) > 0.1}
    varying.update(procedural_motion_names or set())
    missing = sorted(varying - animated_names)
    return {
        "status": "FAIL" if missing else "PASS",
        "reasons": [f"Required varying rotation channels are missing: {missing}"] if missing else [],
        "required_varying_channels": sorted(varying),
        "missing_varying_channels": missing,
        "constant_pose_nodes": sorted(set(canonical_names) - varying - animated_names),
        "animated_channel_count": len(set(canonical_names) & animated_names),
        "constant_pose_policy": "Constant joints may be represented by static glTF node transforms; geometric retargeting checks still apply.",
    }
