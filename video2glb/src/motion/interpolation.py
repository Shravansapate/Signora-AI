from __future__ import annotations

import numpy as np


def interpolate_short_gaps(values: np.ndarray, max_gap: int) -> np.ndarray:
    output = np.asarray(values, dtype=np.float64).copy()
    if output.ndim < 2:
        raise ValueError("Expected values with a frame dimension.")
    frame_count = output.shape[0]
    flat = output.reshape(frame_count, -1)
    for column in range(flat.shape[1]):
        series = flat[:, column]
        valid = np.isfinite(series)
        if valid.all() or not valid.any():
            continue
        indices = np.arange(frame_count)
        invalid_runs = _invalid_runs(valid)
        for start, end in invalid_runs:
            length = end - start
            if length <= max_gap and start > 0 and end < frame_count:
                series[start:end] = np.interp(indices[start:end], indices[valid], series[valid])
    return output


def _invalid_runs(valid: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, is_valid in enumerate(valid):
        if not is_valid and start is None:
            start = index
        elif is_valid and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(valid)))
    return runs
