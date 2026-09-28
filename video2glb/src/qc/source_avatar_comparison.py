from __future__ import annotations

from pathlib import Path
import math

import cv2


def create_source_avatar_comparison(
    source_video: str | Path,
    avatar_preview: str | Path,
    output_video: str | Path,
) -> dict:
    source_path = Path(source_video)
    avatar_path = Path(avatar_preview)
    output_path = Path(output_video)
    source = cv2.VideoCapture(str(source_path))
    avatar = cv2.VideoCapture(str(avatar_path))
    try:
        if not source.isOpened() or not avatar.isOpened():
            raise RuntimeError("Could not open source or avatar preview for comparison.")
        source_fps = float(source.get(cv2.CAP_PROP_FPS) or 0.0)
        avatar_fps = float(avatar.get(cv2.CAP_PROP_FPS) or 0.0)
        source_reported = float(source.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        avatar_reported = float(avatar.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if not all(math.isfinite(value) for value in (source_fps, avatar_fps, source_reported, avatar_reported)) or source_fps <= 0 or avatar_fps <= 0:
            raise RuntimeError("Comparison videos do not provide a valid FPS.")
        source_expected = int(source_reported)
        avatar_expected = int(avatar_reported)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        source_width = int(source.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(source.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        target_height = min(540, max(source_height, 1))
        source_width_scaled = max(1, round(source_width * target_height / max(source_height, 1)))
        avatar_width_scaled = target_height
        writer = cv2.VideoWriter(
            str(output_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            source_fps,
            (source_width_scaled + avatar_width_scaled, target_height),
        )
        if not writer.isOpened():
            raise RuntimeError(f"Could not create comparison video: {output_path}")
        written = 0
        source_pts: list[float] = []
        avatar_pts: list[float] = []
        try:
            while True:
                source_ok, source_frame = source.read()
                avatar_ok, avatar_frame = avatar.read()
                if source_ok:
                    source_pts.append(float(source.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0)
                if avatar_ok:
                    avatar_pts.append(float(avatar.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0)
                if not source_ok and not avatar_ok:
                    break
                if not source_ok or not avatar_ok:
                    continue
                left = cv2.resize(source_frame, (source_width_scaled, target_height), interpolation=cv2.INTER_AREA)
                right = cv2.resize(avatar_frame, (avatar_width_scaled, target_height), interpolation=cv2.INTER_AREA)
                writer.write(cv2.hconcat([left, right]))
                written += 1
        finally:
            writer.release()

        source_count, avatar_count = len(source_pts), len(avatar_pts)
        expected_comparison_count = source_expected if source_expected > 0 else source_count
        reopened_count, reopened_fps, reopened_pts = _decoded_video_timing(output_path)
        source_duration = (source_count - 1) / source_fps if source_count else 0.0
        avatar_duration = (avatar_count - 1) / avatar_fps if avatar_count else 0.0
        reasons: list[str] = []
        if abs(source_fps - avatar_fps) > 0.001:
            reasons.append("SOURCE_AVATAR_FPS_MISMATCH")
        if source_count != avatar_count:
            reasons.append("SOURCE_AVATAR_FRAME_COUNT_MISMATCH")
        if source_expected > 0 and source_count != source_expected:
            reasons.append("SOURCE_DECODE_FRAME_COUNT_MISMATCH")
        if avatar_expected > 0 and avatar_count != avatar_expected:
            reasons.append("AVATAR_DECODE_FRAME_COUNT_MISMATCH")
        if expected_comparison_count <= 0:
            reasons.append("NO_EXPECTED_COMPARISON_FRAMES")
        if written != expected_comparison_count:
            reasons.append("INCOMPLETE_COMPARISON_WRITE")
        if reopened_count != expected_comparison_count:
            reasons.append("INCOMPLETE_COMPARISON_REOPEN")
        source_timing_error = _timeline_error(source_pts, source_fps)
        avatar_timing_error = _timeline_error(avatar_pts, avatar_fps)
        reopened_timing_error = _timeline_error(reopened_pts, source_fps)
        if source_count > 0 and (source_timing_error is None or source_timing_error > 0.0011):
            reasons.append("SOURCE_WORKING_TIMELINE_MISMATCH")
        if avatar_count > 0 and (avatar_timing_error is None or avatar_timing_error > 0.0011):
            reasons.append("AVATAR_TIMELINE_MISMATCH")
        if reopened_count > 0 and (not math.isfinite(reopened_fps) or abs(reopened_fps - source_fps) > 0.001 or reopened_timing_error is None or reopened_timing_error > 0.0011):
            reasons.append("COMPARISON_REOPEN_TIMELINE_MISMATCH")

        comparison_complete = (
            expected_comparison_count > 0
            and written == expected_comparison_count
            and reopened_count == expected_comparison_count
            and source_count == avatar_count
            and not reasons
        )
        if expected_comparison_count <= 0 or written <= 0 or reopened_count <= 0:
            status = "FAIL"
        elif reasons:
            status = "REVIEW"
        else:
            status = "PASS"
        return {
            "status": status,
            "reasons": reasons,
            "source_frame_count": source_count,
            "avatar_frame_count": avatar_count,
            "source_reported_frame_count": source_expected,
            "avatar_reported_frame_count": avatar_expected,
            "expected_comparison_frame_count": expected_comparison_count,
            "comparison_frame_count": written,
            "comparison_reopened_frame_count": reopened_count,
            "comparison_complete": comparison_complete,
            "source_fps": source_fps,
            "avatar_fps": avatar_fps,
            "comparison_reopened_fps": reopened_fps,
            "source_timeline_max_error_seconds": source_timing_error,
            "avatar_timeline_max_error_seconds": avatar_timing_error,
            "source_duration_seconds": source_duration,
            "avatar_duration_seconds": avatar_duration,
            "duration_difference_seconds": abs(source_duration - avatar_duration),
            "technical_motion_match": "See the matching .glb_validation.json report for endpoint and finger-direction metrics.",
            "isl_verified": False,
            "isl_verification_status": "PENDING_SIGNER_REVIEW",
        }
    finally:
        source.release()
        avatar.release()


def _count_decodable_frames(path: Path) -> int:
    """Reopen the completed artifact and count frames the decoder can read."""
    return _decoded_video_timing(path)[0]


def _decoded_video_timing(path: Path) -> tuple[int, float, list[float]]:

    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            return 0, 0.0, []
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        timestamps = []
        while True:
            ok, _frame = capture.read()
            if not ok:
                break
            timestamps.append(float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0)
        return len(timestamps), fps, timestamps
    finally:
        capture.release()


def _timeline_error(timestamps: list[float], fps: float) -> float | None:
    if not timestamps or not math.isfinite(fps) or fps <= 0 or not all(math.isfinite(value) for value in timestamps):
        return None
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        return None
    return max(abs((value - timestamps[0]) - index / fps) for index, value in enumerate(timestamps))
