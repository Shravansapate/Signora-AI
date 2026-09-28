"""Retarget source arm depth using avatar segment lengths, without changing XY."""
from __future__ import annotations

import numpy as np

from src.motion.interpolation import interpolate_short_gaps
from src.motion.smoothing import smooth_landmarks_centered


def retarget_arm_depth(world_points, visibility, segment_lengths, fps, *, smoothing=True):
    """Input is canonical camera-facing +Z, with shoulder/elbow/wrist landmarks.

    Scale upper arm and forearm independently. Monocular depth is an estimate;
    unsupported intervals stay explicitly marked even when held for playback.
    """
    points = np.asarray(world_points, dtype=np.float64)
    visible = np.asarray(visibility, dtype=np.float64)
    lengths = np.asarray(segment_lengths, dtype=np.float64)
    if points.ndim != 3 or points.shape[1:] != (33, 3) or visible.shape != points.shape[:2]:
        raise ValueError("Depth requires canonical body landmarks Fx33x3 and visibility Fx33.")
    if lengths.shape != (2, 2) or not np.isfinite(lengths).all() or np.any(lengths <= 0):
        raise ValueError("Depth requires positive avatar upper/forearm lengths for both sides.")
    if len(points) == 0 or not np.isfinite(fps) or fps <= 0:
        raise ValueError("Depth requires a nonempty, positive-FPS timeline.")
    offsets = np.full((len(points), 4), np.nan)
    observed = np.zeros((len(points), 2), dtype=bool)
    reports = {}
    for side, indices in enumerate(((11, 13, 15), (12, 14, 16))):
        chain = points[:, indices]
        vectors = np.diff(chain, axis=1)
        source_lengths = np.linalg.norm(vectors, axis=2)
        valid = np.isfinite(chain).all(axis=(1, 2)) & np.isfinite(visible[:, indices]).all(axis=1)
        valid &= (visible[:, indices] >= 0.5).all(axis=1) & (source_lengths > 1e-6).all(axis=1)
        if valid.any():
            typical = np.median(source_lengths[valid], axis=0)
            valid &= ((source_lengths >= typical * 0.5) & (source_lengths <= typical * 1.5)).all(axis=1)
        if not valid.any():
            raise ValueError(f"No reliable {'Left' if side == 0 else 'Right'} source arm depth; clearer visible arm footage is required.")
        observed[:, side] = valid
        segment_depth = np.divide(vectors[:, :, 2], source_lengths, out=np.zeros_like(source_lengths), where=source_lengths > 1e-6) * lengths[side]
        values = np.column_stack((segment_depth.sum(axis=1), segment_depth[:, 0]))
        values[~valid] = np.nan
        filled = interpolate_short_gaps(values, max_gap=max(1, round(float(fps) * 0.20)))
        usable = np.isfinite(filled).all(axis=1)
        if smoothing:
            filtered = smooth_landmarks_centered(filled, radius=max(1, round(float(fps) * 0.08)))
            filled[usable] = filtered[usable]
        # Playback fallback is recorded, never upgraded into source evidence.
        first = int(np.flatnonzero(usable)[0])
        filled[:first] = filled[first]
        for frame in range(first + 1, len(points)):
            if not usable[frame]:
                filled[frame] = filled[frame - 1]
        offsets[:, side * 2:side * 2 + 2] = filled
        reports[("Left", "Right")[side]] = {
            "observed_frames": int(valid.sum()),
            "interpolated_frames": int((usable & ~valid).sum()),
            "held_frames": int((~usable).sum()),
            "held_frame_indices": (np.flatnonzero(~usable) + 1).tolist(),
            "median_wrist_depth": float(np.median(filled[:, 0])),
        }
    held = any(report["held_frames"] for report in reports.values())
    return offsets, observed, {
        "status": "REVIEW" if held else "PASS",
        "reasons": ["Unobserved arm-depth intervals use held estimates and require source comparison."] if held else [],
        "method": "observed_canonical_upperarm_forearm_depth_scaled_by_avatar_segment_lengths",
        "monocular_estimate": True, "image_plane_trajectory_unchanged": True,
        "sides": reports,
    }
