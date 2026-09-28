from __future__ import annotations

import numpy as np


def mediapipe_world_to_canonical(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float64)
    converted = points[..., :3].copy()
    x = converted[..., 0].copy()
    y = converted[..., 1].copy()
    z = converted[..., 2].copy()
    converted[..., 0] = x
    converted[..., 1] = -y
    converted[..., 2] = -z
    return converted


def mediapipe_image_to_canonical(points: np.ndarray, aspect_ratio: float = 1.0) -> np.ndarray:
    """Convert normalized MediaPipe image coordinates to canonical axes.

    MediaPipe normalizes x by image width and y by image height.  Scaling x
    (and z, whose landmark scale follows x) by width/height prevents a 16:9
    frame from shearing body and palm directions before retargeting.
    """
    points = np.asarray(points, dtype=np.float64)
    aspect_ratio = float(aspect_ratio)
    if not np.isfinite(aspect_ratio) or aspect_ratio <= 0.0:
        raise ValueError("aspect_ratio must be finite and positive")
    converted = np.zeros(points.shape[:-1] + (3,), dtype=np.float64)
    converted[..., 0] = (points[..., 0] - 0.5) * aspect_ratio
    converted[..., 1] = 0.5 - points[..., 1]
    converted[..., 2] = -points[..., 2] * aspect_ratio
    return converted


def canonical_to_blender_vector(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float64)
    return vector[..., :3].copy()


def gltf_quaternion_order() -> str:
    return "XYZW"


def internal_quaternion_order() -> str:
    return "WXYZ"
