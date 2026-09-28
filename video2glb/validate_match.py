from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from src.tracking.holistic_tracker import MediaPipeHolisticBackend
from src.video.inspector import inspect_video


PROJECT_ROOT = Path(__file__).resolve().parent
SOURCE_LANDMARKS = {
    "left_shoulder": 11,
    "right_shoulder": 12,
    "left_elbow": 13,
    "right_elbow": 14,
    "left_wrist": 15,
    "right_wrist": 16,
}


def main() -> int:
    args = parse_args()
    config = load_yaml(resolve_path(args.config))
    source_pose_path = resolve_path(args.source_pose) if args.source_pose else None
    avatar_pose_path = resolve_path(args.avatar_pose) if args.avatar_pose else None
    source_video = resolve_path(args.source)
    avatar_video = resolve_path(args.avatar_recording)
    output = resolve_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    if source_pose_path and source_pose_path.exists():
        source_sequence = load_pose_npz(source_pose_path)
    else:
        source_sequence = track_video(source_video, config, output.parent / "source_validation_overlay.mp4")

    if avatar_pose_path and avatar_pose_path.exists():
        avatar_sequence = load_pose_npz(avatar_pose_path)
    else:
        avatar_sequence = track_video(avatar_video, config, output.parent / "avatar_validation_overlay.mp4")
    report = compare_sequences(source_sequence, avatar_sequence)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.qc_json:
        merge_qc(resolve_path(args.qc_json), report)
    print(json.dumps(report, indent=2))
    return 0 if report["motion_match_qc"] != "FAIL" else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare source sign video against an avatar playback recording.")
    parser.add_argument("--source", required=True, help="Original signer video.")
    parser.add_argument("--avatar-recording", required=True, help="Screen/video recording of avatar playback.")
    parser.add_argument("--source-pose", help="Optional existing source .pose.npz.")
    parser.add_argument("--avatar-pose", help="Optional existing avatar recording .pose.npz.")
    parser.add_argument("--config", default="./config/settings.yaml")
    parser.add_argument("--output", default="./output/PASSENGER/Passenger.match_validation.json")
    parser.add_argument("--qc-json", help="Optional converter QC JSON to merge this match result into.")
    return parser.parse_args()


def track_video(path: Path, config: dict[str, Any], overlay_path: Path) -> dict[str, Any]:
    info = inspect_video(path)
    backend = MediaPipeHolisticBackend(
        model_path=resolve_path(config["pose"]["model_path"]),
        options=config.get("pose", {}),
    )
    sequence = backend.process_video(path, info, overlay_path=overlay_path)
    temp_pose = overlay_path.with_suffix(".pose.npz")
    sequence.save_npz(temp_pose)
    return load_pose_npz(temp_pose)


def load_pose_npz(path: Path) -> dict[str, Any]:
    data = np.load(path)
    return {
        "fps": float(data["fps"]),
        "frame_count": int(data["frame_count"]),
        "pose_image": data["pose_image"].astype(np.float64),
        "left_hand_image": data["left_hand_image"].astype(np.float64),
        "right_hand_image": data["right_hand_image"].astype(np.float64),
    }


def compare_sequences(source: dict[str, Any], avatar: dict[str, Any]) -> dict[str, Any]:
    source_features = normalized_features(source["pose_image"])
    avatar_features = normalized_features(avatar["pose_image"])
    sample_count = min(len(source_features), len(avatar_features), 240)
    source_resampled = resample_features(source_features, sample_count)
    avatar_resampled = resample_features(avatar_features, sample_count)
    valid = np.isfinite(source_resampled).all(axis=1) & np.isfinite(avatar_resampled).all(axis=1)

    if valid.sum() < max(10, sample_count * 0.25):
        return {
            "motion_match_qc": "FAIL",
            "isl_verified": False,
            "isl_communication_status": "NOT_VERIFIED",
            "reason": "Could not track enough comparable body landmarks in source and avatar recording.",
            "valid_comparison_frames": int(valid.sum()),
            "sample_count": int(sample_count),
        }

    diff = np.abs(source_resampled[valid] - avatar_resampled[valid])
    left_wrist_mae = float(np.mean(diff[:, 0:2]))
    right_wrist_mae = float(np.mean(diff[:, 2:4]))
    wrist_mae = float(np.mean(diff[:, 0:4]))
    elbow_mae = float(np.mean(diff[:, 4:8]))
    source_centering = hand_centering_score(source_resampled[valid])
    avatar_centering = hand_centering_score(avatar_resampled[valid])
    centering_error = abs(source_centering - avatar_centering)
    duration_ratio = avatar["frame_count"] / max(avatar["fps"], 1e-6) / (source["frame_count"] / max(source["fps"], 1e-6))

    reasons: list[str] = []
    status = "PASS"
    if wrist_mae > 0.55 or centering_error > 0.65:
        status = "FAIL"
        reasons.append("Avatar wrist path does not match the source signer closely enough.")
    elif wrist_mae > 0.32 or centering_error > 0.38:
        status = "REVIEW"
        reasons.append("Avatar wrist path differs noticeably from the source signer.")
    if not (0.75 <= duration_ratio <= 1.35):
        status = max_status(status, "REVIEW")
        reasons.append("Avatar playback duration differs from source duration.")

    return {
        "motion_match_qc": status,
        "isl_verified": False,
        "isl_communication_status": "REQUIRES_HUMAN_ISL_REVIEW",
        "summary": "Automated pose comparison checks motion similarity only; it cannot certify linguistic ISL correctness.",
        "metrics": {
            "valid_comparison_frames": int(valid.sum()),
            "sample_count": int(sample_count),
            "source_duration_seconds": source["frame_count"] / source["fps"],
            "avatar_duration_seconds": avatar["frame_count"] / avatar["fps"],
            "duration_ratio": float(duration_ratio),
            "wrist_position_mae_normalized": wrist_mae,
            "left_wrist_mae_normalized": left_wrist_mae,
            "right_wrist_mae_normalized": right_wrist_mae,
            "elbow_position_mae_normalized": elbow_mae,
            "source_hand_centering_score": source_centering,
            "avatar_hand_centering_score": avatar_centering,
            "hand_centering_error": centering_error,
        },
        "reasons": reasons,
    }


def normalized_features(pose_image: np.ndarray) -> np.ndarray:
    result = np.full((pose_image.shape[0], 8), np.nan, dtype=np.float64)
    for frame_index, frame in enumerate(pose_image):
        left_shoulder = frame[SOURCE_LANDMARKS["left_shoulder"], :2]
        right_shoulder = frame[SOURCE_LANDMARKS["right_shoulder"], :2]
        if not np.isfinite(left_shoulder).all() or not np.isfinite(right_shoulder).all():
            continue
        center = (left_shoulder + right_shoulder) * 0.5
        scale = max(float(np.linalg.norm(left_shoulder - right_shoulder)), 0.05)
        points = [
            frame[SOURCE_LANDMARKS["left_wrist"], :2],
            frame[SOURCE_LANDMARKS["right_wrist"], :2],
            frame[SOURCE_LANDMARKS["left_elbow"], :2],
            frame[SOURCE_LANDMARKS["right_elbow"], :2],
        ]
        values = []
        for point in points:
            if np.isfinite(point).all():
                normalized = (point - center) / scale
                values.extend([normalized[0], normalized[1]])
            else:
                values.extend([np.nan, np.nan])
        result[frame_index] = values
    return result


def resample_features(features: np.ndarray, sample_count: int) -> np.ndarray:
    if len(features) == sample_count:
        return features
    source_x = np.linspace(0.0, 1.0, len(features))
    target_x = np.linspace(0.0, 1.0, sample_count)
    output = np.full((sample_count, features.shape[1]), np.nan, dtype=np.float64)
    for column in range(features.shape[1]):
        series = features[:, column]
        valid = np.isfinite(series)
        if valid.sum() >= 2:
            output[:, column] = np.interp(target_x, source_x[valid], series[valid])
    return output


def hand_centering_score(features: np.ndarray) -> float:
    left_x = np.abs(features[:, 0])
    right_x = np.abs(features[:, 2])
    return float(np.mean((left_x + right_x) * 0.5))


def max_status(a: str, b: str) -> str:
    order = {"PASS": 0, "REVIEW": 1, "FAIL": 2}
    return a if order[a] >= order[b] else b


def merge_qc(path: Path, report: dict[str, Any]) -> None:
    payload: dict[str, Any] = {}
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
    payload["motion_match_qc"] = report["motion_match_qc"]
    payload["isl_verified"] = False
    payload["isl_communication_status"] = report["isl_communication_status"]
    payload["match_validation"] = report
    payload["overall_qc"] = max_status(payload.get("technical_qc", "PASS"), report["motion_match_qc"])
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


if __name__ == "__main__":
    raise SystemExit(main())
