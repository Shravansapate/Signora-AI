from __future__ import annotations

import numpy as np


EPSILON = 1e-8


def normalize_quaternion(q: np.ndarray) -> np.ndarray:
    q = np.asarray(q, dtype=np.float64)
    norm = np.linalg.norm(q)
    if not np.isfinite(norm) or norm < EPSILON:
        return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64)
    return q / norm


def quaternion_from_axis_angle(axis: np.ndarray, angle: float) -> np.ndarray:
    axis = normalize_vector(axis)
    half = float(angle) * 0.5
    return normalize_quaternion(np.array([np.cos(half), *(np.sin(half) * axis)], dtype=np.float64))


def quaternion_from_matrix(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float64)
    trace = np.trace(matrix)
    if trace > 0:
        scale = np.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (matrix[2, 1] - matrix[1, 2]) / scale
        y = (matrix[0, 2] - matrix[2, 0]) / scale
        z = (matrix[1, 0] - matrix[0, 1]) / scale
    else:
        index = int(np.argmax(np.diag(matrix)))
        if index == 0:
            scale = np.sqrt(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2]) * 2.0
            w = (matrix[2, 1] - matrix[1, 2]) / scale
            x = 0.25 * scale
            y = (matrix[0, 1] + matrix[1, 0]) / scale
            z = (matrix[0, 2] + matrix[2, 0]) / scale
        elif index == 1:
            scale = np.sqrt(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2]) * 2.0
            w = (matrix[0, 2] - matrix[2, 0]) / scale
            x = (matrix[0, 1] + matrix[1, 0]) / scale
            y = 0.25 * scale
            z = (matrix[1, 2] + matrix[2, 1]) / scale
        else:
            scale = np.sqrt(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1]) * 2.0
            w = (matrix[1, 0] - matrix[0, 1]) / scale
            x = (matrix[0, 2] + matrix[2, 0]) / scale
            y = (matrix[1, 2] + matrix[2, 1]) / scale
            z = 0.25 * scale
    return normalize_quaternion(np.array([w, x, y, z], dtype=np.float64))


def quaternion_to_matrix(q: np.ndarray) -> np.ndarray:
    w, x, y, z = normalize_quaternion(q)
    return np.array(
        [
            [1 - 2 * y * y - 2 * z * z, 2 * x * y - 2 * z * w, 2 * x * z + 2 * y * w],
            [2 * x * y + 2 * z * w, 1 - 2 * x * x - 2 * z * z, 2 * y * z - 2 * x * w],
            [2 * x * z - 2 * y * w, 2 * y * z + 2 * x * w, 1 - 2 * x * x - 2 * y * y],
        ],
        dtype=np.float64,
    )


def quaternion_multiply(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return normalize_quaternion(
        np.array(
            [
                aw * bw - ax * bx - ay * by - az * bz,
                aw * bx + ax * bw + ay * bz - az * by,
                aw * by - ax * bz + ay * bw + az * bx,
                aw * bz + ax * by - ay * bx + az * bw,
            ],
            dtype=np.float64,
        )
    )


def quaternion_slerp(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    a = normalize_quaternion(a)
    b = normalize_quaternion(b)
    t = float(np.clip(t, 0.0, 1.0))
    dot = float(np.dot(a, b))
    if dot < 0.0:
        b = -b
        dot = -dot
    if dot > 0.9995:
        return normalize_quaternion(a + t * (b - a))
    theta_0 = np.arccos(np.clip(dot, -1.0, 1.0))
    theta = theta_0 * t
    sin_theta = np.sin(theta)
    sin_theta_0 = np.sin(theta_0)
    s0 = np.cos(theta) - dot * sin_theta / sin_theta_0
    s1 = sin_theta / sin_theta_0
    return normalize_quaternion((s0 * a) + (s1 * b))


def limit_quaternion_angle(q: np.ndarray, max_angle: float) -> np.ndarray:
    q = normalize_quaternion(q)
    angle = 2.0 * np.arccos(float(np.clip(abs(q[0]), -1.0, 1.0)))
    if angle <= max_angle:
        return q
    return quaternion_slerp(np.array([1.0, 0.0, 0.0, 0.0]), q, max_angle / max(angle, EPSILON))


def quaternion_from_vectors(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    source = normalize_vector(source)
    target = normalize_vector(target)
    dot = float(np.dot(source, target))
    if dot > 1.0 - EPSILON:
        return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64)
    if dot < -1.0 + EPSILON:
        axis = np.cross(source, np.array([1.0, 0.0, 0.0], dtype=np.float64))
        if np.linalg.norm(axis) < EPSILON:
            axis = np.cross(source, np.array([0.0, 1.0, 0.0], dtype=np.float64))
        return quaternion_from_axis_angle(axis, np.pi)
    cross = np.cross(source, target)
    return normalize_quaternion(np.array([1.0 + dot, cross[0], cross[1], cross[2]], dtype=np.float64))


def enforce_quaternion_continuity(rotations: np.ndarray) -> np.ndarray:
    output = np.asarray(rotations, dtype=np.float64).copy()
    for bone_index in range(output.shape[1]):
        previous = output[0, bone_index]
        output[0, bone_index] = normalize_quaternion(previous)
        for frame_index in range(1, output.shape[0]):
            current = normalize_quaternion(output[frame_index, bone_index])
            if np.dot(previous, current) < 0:
                current = -current
            output[frame_index, bone_index] = current
            previous = current
    return output


def normalize_vector(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    norm = np.linalg.norm(v)
    if not np.isfinite(norm) or norm < EPSILON:
        return np.array([0.0, 1.0, 0.0], dtype=np.float64)
    return v / norm


def validate_quaternions(rotations: np.ndarray, tolerance: float = 1e-3) -> None:
    if not np.isfinite(rotations).all():
        raise ValueError("Motion rotations contain NaN or Infinity.")
    norms = np.linalg.norm(rotations, axis=2)
    if np.max(np.abs(norms - 1.0)) > tolerance:
        raise ValueError("Motion rotations are not unit quaternions within tolerance.")
