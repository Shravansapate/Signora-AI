from __future__ import annotations

from typing import Any, Sequence

import numpy as np


def evaluate_direction_continuity(
    directions: np.ndarray,
    channel_names: Sequence[str],
    fps: float,
    *,
    source_directions: np.ndarray | None = None,
    source_valid: np.ndarray | None = None,
) -> dict[str, Any]:
    """Check every transition, separating supported fast motion from export jumps.

    Angular velocity is expressed as equivalent degrees per frame at 25 FPS so
    the same motion receives the same limits at different sampling rates.
    """
    actual = np.asarray(directions, dtype=np.float64)
    if actual.ndim != 3 or actual.shape[1:] != (len(channel_names), 3) or len(actual) < 2 or not np.isfinite(actual).all() or not np.isfinite(fps) or fps <= 0:
        return {"status": "FAIL", "reasons": ["Invalid direction samples or FPS for all-frame continuity."]}
    lengths = np.linalg.norm(actual, axis=2, keepdims=True)
    if np.any(lengths < 1e-8):
        return {"status": "FAIL", "reasons": ["Zero-length direction in all-frame continuity samples."]}
    actual = actual / lengths
    steps = np.degrees(np.arccos(np.clip(np.sum(actual[1:] * actual[:-1], axis=2), -1.0, 1.0)))
    rate_steps = steps * float(fps) / 25.0
    supported = np.zeros_like(steps, dtype=bool)
    source_steps = np.zeros_like(steps)
    if source_directions is not None:
        source = np.asarray(source_directions, dtype=np.float64)
        valid = np.asarray(source_valid, dtype=bool) if source_valid is not None else np.isfinite(source).all(axis=2)
        if source.shape != actual.shape or valid.shape != actual.shape[:2]:
            return {"status": "FAIL", "reasons": ["Source continuity samples do not match the exported timeline."]}
        sizes = np.linalg.norm(source, axis=2, keepdims=True)
        valid = valid & np.isfinite(source).all(axis=2) & (sizes[..., 0] > 1e-8)
        source = source / np.maximum(sizes, 1e-8)
        source_steps = np.degrees(np.arccos(np.clip(np.sum(source[1:] * source[:-1], axis=2), -1.0, 1.0))) * float(fps) / 25.0
        supported = valid[1:] & valid[:-1] & np.isfinite(source_steps)
    unexplained = np.where(supported, np.maximum(0.0, rate_steps - source_steps), rate_steps)
    # A supported fast turn still needs human inspection; it must not be
    # silently smoothed out or classified as a retargeting failure.
    failures = unexplained > 45.0
    reviews = (rate_steps > 30.0) | (unexplained > 26.0)
    reasons = []
    if failures.any():
        reasons.append("Exported finger direction has a large turn unsupported by reliable source motion.")
    elif reviews.any():
        reasons.append("Fast or uncertain finger transitions require source comparison.")
    worst = np.unravel_index(int(np.argmax(unexplained)), unexplained.shape)
    return {
        "status": "FAIL" if failures.any() else ("REVIEW" if reviews.any() else "PASS"),
        "reasons": reasons,
        "evaluated_every_frame_transition": True,
        "frame_count": len(actual),
        "channel_count": len(channel_names),
        "sample_count": int(steps.size),
        "source_supported_transition_count": int(supported.sum()),
        "maximum_step_degrees": float(np.max(steps)),
        "maximum_normalized_step_degrees": float(np.max(rate_steps)),
        "p99_normalized_step_degrees": float(np.percentile(rate_steps, 99)),
        "maximum_unexplained_step_degrees": float(np.max(unexplained)),
        "worst_frame": int(worst[0] + 2),
        "worst_channel": str(channel_names[worst[1]]),
        "fail_frame_indices": (np.flatnonzero(np.any(failures, axis=1)) + 2).tolist(),
        "review_frame_indices": (np.flatnonzero(np.any(reviews & ~failures, axis=1)) + 2).tolist(),
        "thresholds": {"reference_fps": 25.0, "review_step_degrees": 30.0, "fail_unexplained_step_degrees": 45.0},
    }


def evaluate_quaternion_jitter(
    rotations_wxyz: np.ndarray,
    channel_names: Sequence[str],
    fps: float,
    *,
    maximum_residual_degrees: float = 12.0,
    p95_residual_degrees: float = 3.0,
    sample_states: np.ndarray | None = None,
) -> dict[str, Any]:
    """Measure isolated one-frame rotation spikes against neighbour midpoints."""

    rotations = np.asarray(rotations_wxyz, dtype=np.float64)
    reasons: list[str] = []
    if rotations.ndim != 3 or rotations.shape[2] != 4:
        return {"status": "FAIL", "reasons": ["Rotation samples must have shape FxCx4."]}
    frame_count, channel_count, _ = rotations.shape
    if frame_count < 3 or channel_count <= 0:
        return {"status": "FAIL", "reasons": ["At least three frames and one channel are required."]}
    if len(channel_names) != channel_count:
        return {"status": "FAIL", "reasons": ["Rotation channel names do not match samples."]}
    if not np.isfinite(rotations).all() or not np.isfinite(float(fps)) or float(fps) <= 0:
        return {"status": "FAIL", "reasons": ["Rotation samples and FPS must be finite and valid."]}
    if not np.isfinite([maximum_residual_degrees, p95_residual_degrees]).all() or min(maximum_residual_degrees, p95_residual_degrees) < 0:
        return {"status": "FAIL", "reasons": ["Jitter limits must be finite and nonnegative."]}
    states = None
    if sample_states is not None:
        states = np.asarray(sample_states, dtype=str)
        if states.shape != (frame_count, channel_count) or not np.isin(states, ["SOURCE_ACTIVE", "NEUTRAL", "TRANSITION", "MISSING"]).all():
            return {"status": "FAIL", "reasons": ["Jitter sample states must label each frame/channel as SOURCE_ACTIVE, NEUTRAL, TRANSITION, or MISSING."]}

    norms = np.linalg.norm(rotations, axis=2, keepdims=True)
    if np.any(norms <= 1e-8):
        return {"status": "FAIL", "reasons": ["Rotation samples contain a zero quaternion."]}
    rotations = rotations / norms
    previous = rotations[:-2]
    following = rotations[2:].copy()
    following *= np.where(np.sum(previous * following, axis=2, keepdims=True) < 0.0, -1.0, 1.0)
    midpoint = previous + following
    midpoint_norm = np.linalg.norm(midpoint, axis=2, keepdims=True)
    if np.any(midpoint_norm <= 1e-8):
        return {"status": "FAIL", "reasons": ["Neighbouring rotations have an ambiguous midpoint."]}
    midpoint /= midpoint_norm
    center = rotations[1:-1]
    dots = np.abs(np.sum(center * midpoint, axis=2))
    residuals = np.degrees(2.0 * np.arccos(np.clip(dots, -1.0, 1.0)))
    maximum = float(np.max(residuals))
    p95 = float(np.percentile(residuals, 95))
    mean = float(np.mean(residuals))
    worst = np.unravel_index(int(np.argmax(residuals)), residuals.shape)
    # Labels explain where a spike occurred; they never exempt transitions or
    # unobserved frames from the same exported-motion checks. A midpoint uses
    # three frames, so a boundary triplet cannot be called fully source-active.
    triplet_states = np.full(residuals.shape, "UNCLASSIFIED", dtype="<U16")
    state_metrics = {}
    if states is not None:
        stable_state = (states[:-2] == states[1:-1]) & (states[1:-1] == states[2:])
        triplet_states = np.where(stable_state, states[1:-1], "TRANSITION")
        for state in ("SOURCE_ACTIVE", "NEUTRAL", "TRANSITION", "MISSING"):
            selected = residuals[triplet_states == state]
            state_metrics[state] = {
                "sample_count": int(selected.size),
                "maximum_degrees": float(selected.max()) if selected.size else None,
                "p95_degrees": float(np.percentile(selected, 95)) if selected.size else None,
                "maximum_limit_exceeded_count": int((selected > maximum_residual_degrees).sum()),
            }
    worst_samples = []
    for flat_index in np.argsort(residuals, axis=None)[-8:][::-1]:
        frame_index, channel_index = np.unravel_index(flat_index, residuals.shape)
        worst_samples.append({"frame": int(frame_index + 2), "channel": str(channel_names[channel_index]),
                              "residual_degrees": float(residuals[frame_index, channel_index]),
                              "state": str(triplet_states[frame_index, channel_index])})
    if maximum > maximum_residual_degrees:
        reasons.append(
            f"Maximum one-frame rotation residual {maximum:.4f} degrees exceeds "
            f"{maximum_residual_degrees:.4f}."
        )
    if p95 > p95_residual_degrees:
        reasons.append(
            f"P95 one-frame rotation residual {p95:.4f} degrees exceeds "
            f"{p95_residual_degrees:.4f}."
        )
    return {
        "status": "FAIL" if reasons else "PASS",
        "reasons": reasons,
        "frame_count": frame_count,
        "channel_count": channel_count,
        "fps": float(fps),
        "metric": "quaternion_neighbour_midpoint_residual",
        "mean_degrees": mean,
        "p95_degrees": p95,
        "maximum_degrees": maximum,
        "worst_frame": int(worst[0] + 2),
        "worst_channel": str(channel_names[worst[1]]),
        "worst_samples": worst_samples,
        "maximum_limit_exceeded_frame_indices": (np.flatnonzero(np.any(residuals > maximum_residual_degrees, axis=1)) + 2).tolist(),
        "state_metrics": state_metrics,
        "thresholds": {
            "p95_degrees": p95_residual_degrees,
            "maximum_degrees": maximum_residual_degrees,
        },
    }
