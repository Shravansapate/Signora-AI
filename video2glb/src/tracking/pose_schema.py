from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import numpy as np


POSE_LANDMARK_COUNT = 33
HAND_LANDMARK_COUNT = 21


@dataclass
class PoseFrame:
    frame_index: int
    timestamp_ms: int
    pose_image: np.ndarray
    pose_world: np.ndarray
    left_hand_image: np.ndarray | None
    left_hand_world: np.ndarray | None
    right_hand_image: np.ndarray | None
    right_hand_world: np.ndarray | None
    tracking_quality: dict[str, Any]


@dataclass
class PoseSequence:
    fps: float
    width: int
    height: int
    frames: list[PoseFrame]

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    def save_npz(self, path: str | Path) -> None:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        frame_count = len(self.frames)
        pose_image = np.full((frame_count, POSE_LANDMARK_COUNT, 4), np.nan, dtype=np.float32)
        pose_world = np.full((frame_count, POSE_LANDMARK_COUNT, 4), np.nan, dtype=np.float32)
        left_hand_image = np.full((frame_count, HAND_LANDMARK_COUNT, 3), np.nan, dtype=np.float32)
        left_hand_world = np.full((frame_count, HAND_LANDMARK_COUNT, 3), np.nan, dtype=np.float32)
        right_hand_image = np.full((frame_count, HAND_LANDMARK_COUNT, 3), np.nan, dtype=np.float32)
        right_hand_world = np.full((frame_count, HAND_LANDMARK_COUNT, 3), np.nan, dtype=np.float32)
        frame_indices = np.zeros(frame_count, dtype=np.int32)
        timestamps_ms = np.zeros(frame_count, dtype=np.int64)
        tracking_quality_json: list[str] = []

        for index, frame in enumerate(self.frames):
            frame_indices[index] = frame.frame_index
            timestamps_ms[index] = frame.timestamp_ms
            pose_image[index] = _fit_array(frame.pose_image, (POSE_LANDMARK_COUNT, 4))
            pose_world[index] = _fit_array(frame.pose_world, (POSE_LANDMARK_COUNT, 4))
            if frame.left_hand_image is not None:
                left_hand_image[index] = _fit_array(frame.left_hand_image, (HAND_LANDMARK_COUNT, 3))
            if frame.left_hand_world is not None:
                left_hand_world[index] = _fit_array(frame.left_hand_world, (HAND_LANDMARK_COUNT, 3))
            if frame.right_hand_image is not None:
                right_hand_image[index] = _fit_array(frame.right_hand_image, (HAND_LANDMARK_COUNT, 3))
            if frame.right_hand_world is not None:
                right_hand_world[index] = _fit_array(frame.right_hand_world, (HAND_LANDMARK_COUNT, 3))
            tracking_quality_json.append(json.dumps(frame.tracking_quality, sort_keys=True))

        np.savez_compressed(
            output_path,
            fps=np.array(self.fps, dtype=np.float32),
            width=np.array(self.width, dtype=np.int32),
            height=np.array(self.height, dtype=np.int32),
            frame_count=np.array(frame_count, dtype=np.int32),
            frame_indices=frame_indices,
            timestamps_ms=timestamps_ms,
            pose_image=pose_image,
            pose_world=pose_world,
            left_hand_image=left_hand_image,
            left_hand_world=left_hand_world,
            right_hand_image=right_hand_image,
            right_hand_world=right_hand_world,
            tracking_quality_json=np.array(tracking_quality_json, dtype=np.str_),
            coordinate_notes=np.array(
                [
                    "pose_image and hand_image are normalized MediaPipe image coordinates.",
                    "pose_world and hand_world are raw MediaPipe world coordinates.",
                    "Hand world coordinates are stored raw and are not fused into body coordinates.",
                ],
                dtype=np.str_,
            ),
        )

    @classmethod
    def load_npz(cls, path: str | Path) -> "PoseSequence":
        """Reload a saved raw pose sequence for validation-only retries."""
        data = np.load(path)
        frame_count = int(data["frame_count"])
        pose_image = data["pose_image"]
        pose_world = data["pose_world"]
        left_hand_image = data["left_hand_image"]
        left_hand_world = data["left_hand_world"]
        right_hand_image = data["right_hand_image"]
        right_hand_world = data["right_hand_world"]
        qualities = data["tracking_quality_json"] if "tracking_quality_json" in data.files else None
        frames: list[PoseFrame] = []
        for index in range(frame_count):
            quality = _load_quality(qualities[index]) if qualities is not None else {}
            frames.append(
                PoseFrame(
                    frame_index=int(data["frame_indices"][index]),
                    timestamp_ms=int(data["timestamps_ms"][index]),
                    pose_image=np.asarray(pose_image[index], dtype=np.float32),
                    pose_world=np.asarray(pose_world[index], dtype=np.float32),
                    left_hand_image=_optional_array(left_hand_image[index]),
                    left_hand_world=_optional_array(left_hand_world[index]),
                    right_hand_image=_optional_array(right_hand_image[index]),
                    right_hand_world=_optional_array(right_hand_world[index]),
                    tracking_quality=quality,
                )
            )
        return cls(
            fps=float(data["fps"]),
            width=int(data["width"]),
            height=int(data["height"]),
            frames=frames,
        )


def _fit_array(value: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    fitted = np.full(shape, np.nan, dtype=np.float32)
    rows = min(shape[0], value.shape[0])
    columns = min(shape[1], value.shape[1])
    fitted[:rows, :columns] = value[:rows, :columns]
    return fitted


def _optional_array(value: np.ndarray) -> np.ndarray | None:
    result = np.asarray(value, dtype=np.float32)
    return result if np.isfinite(result[:, :3]).any() else None


def _load_quality(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
