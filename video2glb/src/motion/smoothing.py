from __future__ import annotations

import math
import numpy as np


def smooth_arm_rotation_spikes(rotations: np.ndarray) -> tuple[np.ndarray, dict]:
    """Bound post-IK corrections to 3 degrees; preserve endpoints and smooth motion."""
    from src.motion.quaternion_utils import quaternion_slerp, validate_quaternions
    values = np.asarray(rotations, dtype=np.float64)
    if values.ndim != 3 or values.shape[-1] != 4:
        raise ValueError("Arm rotations must have shape frames x channels x 4.")
    validate_quaternions(values)
    output = values.copy()
    changes = []
    for frame in range(1, len(values) - 1):
        for channel in range(values.shape[1]):
            midpoint = quaternion_slerp(values[frame - 1, channel], values[frame + 1, channel], 0.5)
            angle = 2 * np.arccos(np.clip(abs(np.dot(midpoint, values[frame, channel])), 0, 1))
            if angle <= np.radians(8):
                continue
            amount = min(0.4, np.radians(3) / angle)
            output[frame, channel] = quaternion_slerp(values[frame, channel], midpoint, amount)
            changes.append({"frame": frame + 1, "channel": channel, "adjustment_degrees": float(np.degrees(angle * amount))})
    return output, {
        "status": "REVIEW" if changes else "PASS",
        "reasons": ["Bounded post-IK arm smoothing requires source comparison."] if changes else [],
        "adjusted_sample_count": len(changes), "maximum_adjustment_degrees": 3.0,
        "sample_corrections": changes[:32], "clip_endpoints_preserved": True,
    }


class OneEuroFilter:
    def __init__(self, fps: float, min_cutoff: float = 1.0, beta: float = 0.01, d_cutoff: float = 1.0) -> None:
        self.frequency = fps
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.previous: np.ndarray | None = None
        self.previous_derivative: np.ndarray | None = None

    def apply(self, value: np.ndarray) -> np.ndarray:
        value = np.asarray(value, dtype=np.float64)
        if self.previous is None:
            self.previous = value
            self.previous_derivative = np.zeros_like(value)
            return value
        derivative = (value - self.previous) * self.frequency
        derivative_hat = exponential_smooth(self.alpha(self.d_cutoff), derivative, self.previous_derivative)
        cutoff = self.min_cutoff + self.beta * np.abs(derivative_hat)
        value_hat = exponential_smooth(self.alpha(cutoff), value, self.previous)
        self.previous = value_hat
        self.previous_derivative = derivative_hat
        return value_hat

    def alpha(self, cutoff) -> np.ndarray:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        te = 1.0 / self.frequency
        return 1.0 / (1.0 + tau / te)


def smooth_landmarks(values: np.ndarray, fps: float, min_cutoff: float, beta: float) -> np.ndarray:
    output = np.asarray(values, dtype=np.float64).copy()
    frame_count = output.shape[0]
    flat = output.reshape(frame_count, -1)
    filters = [OneEuroFilter(fps=fps, min_cutoff=min_cutoff, beta=beta) for _ in range(flat.shape[1])]
    for frame_index in range(frame_count):
        for column, one_euro in enumerate(filters):
            if np.isfinite(flat[frame_index, column]):
                flat[frame_index, column] = one_euro.apply(np.array(flat[frame_index, column]))
    return output


def smooth_landmarks_centered(values: np.ndarray, radius: int = 2) -> np.ndarray:
    """Offline zero-phase triangular smoothing with NaN-aware weights.

    Video-to-GLB conversion has the complete clip available, so a centered
    filter avoids the visible temporal lag of a causal real-time filter.
    """
    source = np.asarray(values, dtype=np.float64)
    if source.shape[0] < 2 or radius <= 0:
        return source.copy()
    radius = min(int(radius), max(1, source.shape[0] - 1))
    weighted_sum = np.zeros_like(source, dtype=np.float64)
    weight_sum = np.zeros_like(source, dtype=np.float64)
    for offset in range(-radius, radius + 1):
        weight = float(radius + 1 - abs(offset))
        if offset < 0:
            destination = slice(-offset, None)
            origin = slice(None, offset)
        elif offset > 0:
            destination = slice(None, -offset)
            origin = slice(offset, None)
        else:
            destination = slice(None)
            origin = slice(None)
        sample = source[origin]
        finite = np.isfinite(sample)
        weighted_sum[destination] += np.where(finite, sample, 0.0) * weight
        weight_sum[destination] += finite * weight
    return np.divide(
        weighted_sum,
        weight_sum,
        out=np.full_like(weighted_sum, np.nan),
        where=weight_sum > 0.0,
    )


def exponential_smooth(alpha, value, previous):
    return alpha * value + (1.0 - alpha) * previous
