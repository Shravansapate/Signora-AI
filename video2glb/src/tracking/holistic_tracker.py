from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from src.tracking.pose_schema import HAND_LANDMARK_COUNT, POSE_LANDMARK_COUNT, PoseFrame, PoseSequence
from src.tracking.tracking_qc import assess_hand_assignment
from src.video.inspector import VideoInfo


ProgressCallback = Callable[[int, int], None]


class PoseBackend(ABC):
    @abstractmethod
    def process_video(
        self,
        video_path: str | Path,
        video_info: VideoInfo,
        overlay_path: str | Path | None = None,
        progress: ProgressCallback | None = None,
    ) -> PoseSequence:
        raise NotImplementedError


class MediaPipeHolisticBackend(PoseBackend):
    def __init__(self, model_path: str | Path, options: dict[str, Any] | None = None) -> None:
        self.model_path = Path(model_path)
        self.options = options or {}

    def process_video(
        self,
        video_path: str | Path,
        video_info: VideoInfo,
        overlay_path: str | Path | None = None,
        progress: ProgressCallback | None = None,
    ) -> PoseSequence:
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"MediaPipe model not found: {self.model_path}. "
                "Download holistic_landmarker.task into the configured models directory."
            )

        import mediapipe as mp

        BaseOptions = mp.tasks.BaseOptions
        HolisticLandmarker = mp.tasks.vision.HolisticLandmarker
        HolisticLandmarkerOptions = mp.tasks.vision.HolisticLandmarkerOptions
        VisionRunningMode = mp.tasks.vision.RunningMode

        task_options = HolisticLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.model_path)),
            running_mode=VisionRunningMode.VIDEO,
            min_face_detection_confidence=float(self.options.get("min_face_detection_confidence", 0.5)),
            min_pose_detection_confidence=float(self.options.get("min_pose_detection_confidence", 0.5)),
            min_pose_landmarks_confidence=float(self.options.get("min_pose_landmarks_confidence", 0.5)),
            min_hand_landmarks_confidence=float(self.options.get("min_hand_landmarks_confidence", 0.5)),
            output_face_blendshapes=False,
            output_segmentation_mask=False,
        )

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"OpenCV could not open video: {video_path}")

        writer = _open_overlay_writer(overlay_path, video_info) if overlay_path else None
        frames: list[PoseFrame] = []

        try:
            with HolisticLandmarker.create_from_options(task_options) as landmarker:
                for frame_index, timestamp_ms, frame_bgr in _validated_video_frames(cap, video_info):

                    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
                    result = landmarker.detect_for_video(mp_image, timestamp_ms)
                    pose_frame = _result_to_pose_frame(result, frame_index, timestamp_ms)
                    pose_frame.tracking_quality["hand_assignment"] = assess_hand_assignment(
                        pose_frame, frames[-1] if frames else None,
                        aspect_ratio=video_info.width / video_info.height,
                    )
                    frames.append(pose_frame)

                    if writer is not None:
                        writer.write(draw_overlay(frame_bgr, pose_frame, video_info.width, video_info.height))

                    completed = frame_index + 1
                    if progress and (completed == 1 or completed % 25 == 0 or completed == video_info.frame_count):
                        progress(completed, video_info.frame_count)
        finally:
            cap.release()
            if writer is not None:
                writer.release()

        return PoseSequence(fps=video_info.fps, width=video_info.width, height=video_info.height, frames=frames)


def _validated_video_frames(cap: Any, video_info: VideoInfo):
    """Consume exactly the inspected working timeline or fail the conversion."""
    timestamps = video_info.timestamps_ms
    if video_info.variable_frame_rate:
        raise RuntimeError("Tracking requires the normalized constant-frame-rate working video.")
    if video_info.frame_count <= 0 or len(timestamps) != video_info.frame_count:
        raise RuntimeError("Inspected video frame count and timestamp count do not match.")
    if not np.isfinite(video_info.fps) or video_info.fps <= 0:
        raise RuntimeError("Tracking requires a finite positive FPS.")
    if not np.isfinite(timestamps).all() or any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise RuntimeError("Tracking timestamps must be finite and strictly increasing.")
    for index, timestamp in enumerate(timestamps):
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"Incomplete tracking decode: expected {video_info.frame_count} frames, decoded {index}.")
        if frame.shape[:2] != (video_info.height, video_info.width):
            raise RuntimeError(f"Tracking frame {index} dimensions differ from inspected video.")
        yield index, int(timestamp), frame
    extra, _frame = cap.read()
    if extra:
        raise RuntimeError(f"Tracking decoded more than the inspected {video_info.frame_count} frames.")


def _result_to_pose_frame(result: Any, frame_index: int, timestamp_ms: int) -> PoseFrame:
    pose_image = _landmarks_to_array(
        _first_landmark_list(getattr(result, "pose_landmarks", None)),
        POSE_LANDMARK_COUNT,
        include_visibility=True,
    )
    pose_world = _landmarks_to_array(
        _first_landmark_list(getattr(result, "pose_world_landmarks", None)),
        POSE_LANDMARK_COUNT,
        include_visibility=True,
    )
    left_hand_image = _optional_landmarks_to_array(
        _first_landmark_list(getattr(result, "left_hand_landmarks", None)),
        HAND_LANDMARK_COUNT,
        include_visibility=False,
    )
    left_hand_world = _optional_landmarks_to_array(
        _first_landmark_list(getattr(result, "left_hand_world_landmarks", None)),
        HAND_LANDMARK_COUNT,
        include_visibility=False,
    )
    right_hand_image = _optional_landmarks_to_array(
        _first_landmark_list(getattr(result, "right_hand_landmarks", None)),
        HAND_LANDMARK_COUNT,
        include_visibility=False,
    )
    right_hand_world = _optional_landmarks_to_array(
        _first_landmark_list(getattr(result, "right_hand_world_landmarks", None)),
        HAND_LANDMARK_COUNT,
        include_visibility=False,
    )

    quality = {
        "pose_present": bool(np.isfinite(pose_image[:, :3]).any()),
        "left_hand_present": left_hand_image is not None,
        "right_hand_present": right_hand_image is not None,
    }

    return PoseFrame(
        frame_index=frame_index,
        timestamp_ms=timestamp_ms,
        pose_image=pose_image,
        pose_world=pose_world,
        left_hand_image=left_hand_image,
        left_hand_world=left_hand_world,
        right_hand_image=right_hand_image,
        right_hand_world=right_hand_world,
        tracking_quality=quality,
    )


def _first_landmark_list(value: Any) -> Any | None:
    if value is None:
        return None
    if hasattr(value, "__len__") and len(value) == 0:
        return None
    if hasattr(value, "__len__") and len(value) > 0 and hasattr(value[0], "x"):
        return value
    if hasattr(value, "__len__") and len(value) > 0:
        return value[0]
    return value


def _optional_landmarks_to_array(value: Any | None, count: int, include_visibility: bool) -> np.ndarray | None:
    if value is None:
        return None
    array = _landmarks_to_array(value, count, include_visibility)
    if not np.isfinite(array[:, :3]).any():
        return None
    return array


def _landmarks_to_array(value: Any | None, count: int, include_visibility: bool) -> np.ndarray:
    columns = 4 if include_visibility else 3
    array = np.full((count, columns), np.nan, dtype=np.float32)
    if value is None:
        return array

    for index, landmark in enumerate(value[:count]):
        array[index, 0] = float(getattr(landmark, "x", np.nan))
        array[index, 1] = float(getattr(landmark, "y", np.nan))
        array[index, 2] = float(getattr(landmark, "z", np.nan))
        if include_visibility:
            array[index, 3] = float(getattr(landmark, "visibility", getattr(landmark, "presence", np.nan)))
    return array


POSE_CONNECTIONS = [
    (11, 12),
    (11, 13),
    (13, 15),
    (12, 14),
    (14, 16),
    (11, 23),
    (12, 24),
    (23, 24),
]

HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (0, 5), (5, 6), (6, 7), (7, 8),
    (0, 9), (9, 10), (10, 11), (11, 12),
    (0, 13), (13, 14), (14, 15), (15, 16),
    (0, 17), (17, 18), (18, 19), (19, 20),
    (5, 9), (9, 13), (13, 17),
]


def draw_overlay(frame_bgr: np.ndarray, pose_frame: PoseFrame, width: int, height: int) -> np.ndarray:
    output = frame_bgr.copy()
    _draw_landmark_set(output, pose_frame.pose_image[:, :3], width, height, POSE_CONNECTIONS, (80, 220, 80))
    if pose_frame.left_hand_image is not None:
        _draw_landmark_set(output, pose_frame.left_hand_image[:, :3], width, height, HAND_CONNECTIONS, (0, 180, 255))
    if pose_frame.right_hand_image is not None:
        _draw_landmark_set(output, pose_frame.right_hand_image[:, :3], width, height, HAND_CONNECTIONS, (255, 120, 80))

    cv2.putText(
        output,
        f"frame {pose_frame.frame_index}  L:{'Y' if pose_frame.left_hand_image is not None else 'N'} "
        f"R:{'Y' if pose_frame.right_hand_image is not None else 'N'}",
        (24, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return output


def _draw_landmark_set(
    frame: np.ndarray,
    landmarks: np.ndarray,
    width: int,
    height: int,
    connections: list[tuple[int, int]],
    color: tuple[int, int, int],
) -> None:
    points: list[tuple[int, int] | None] = []
    for landmark in landmarks:
        if not np.isfinite(landmark[:2]).all():
            points.append(None)
            continue
        x = int(round(float(landmark[0]) * width))
        y = int(round(float(landmark[1]) * height))
        points.append((x, y))

    for start, end in connections:
        if start < len(points) and end < len(points) and points[start] is not None and points[end] is not None:
            cv2.line(frame, points[start], points[end], color, 2, cv2.LINE_AA)

    for point in points:
        if point is not None:
            cv2.circle(frame, point, 4, color, -1, cv2.LINE_AA)


def _open_overlay_writer(overlay_path: str | Path, video_info: VideoInfo) -> cv2.VideoWriter:
    path = Path(overlay_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, video_info.fps, (video_info.width, video_info.height))
    if not writer.isOpened():
        raise RuntimeError(f"Could not create overlay video: {path}")
    return writer


def _timestamp_for_frame(frame_index: int, fps: float, previous_timestamp_ms: int) -> int:
    timestamp_ms = int(round((frame_index / fps) * 1000.0))
    if timestamp_ms <= previous_timestamp_ms:
        timestamp_ms = previous_timestamp_ms + 1
    return timestamp_ms
