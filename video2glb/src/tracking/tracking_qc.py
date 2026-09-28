from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from src.tracking.pose_schema import PoseFrame, PoseSequence


LEFT_WRIST = 15
RIGHT_WRIST = 16
HAND_WRIST = 0


@dataclass(frozen=True)
class TrackingQcResult:
    technical_qc: str
    isl_verified: bool
    metrics: dict[str, Any]
    review_reasons: list[str]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "technical_qc": self.technical_qc,
            "isl_verified": self.isl_verified,
            "metrics": self.metrics,
            "review_reasons": self.review_reasons,
        }


def evaluate_tracking(sequence: PoseSequence, thresholds: dict[str, Any]) -> TrackingQcResult:
    frames = sequence.frames
    frame_count = len(frames)
    if frame_count == 0:
        return TrackingQcResult(
            technical_qc="FAIL",
            isl_verified=False,
            metrics={"frame_count": 0},
            review_reasons=["No frames were processed."],
        )

    pose_present = np.array([frame.tracking_quality.get("pose_present", False) for frame in frames], dtype=bool)
    left_present = np.array([frame.tracking_quality.get("left_hand_present", False) for frame in frames], dtype=bool)
    right_present = np.array([frame.tracking_quality.get("right_hand_present", False) for frame in frames], dtype=bool)
    any_hand_present = left_present | right_present
    assignments = [assess_hand_assignment(frame, frames[index - 1] if index else None,
                                         aspect_ratio=sequence.width / max(sequence.height, 1))
                   for index, frame in enumerate(frames)]
    swap_events = sum(any(item[side] == "POSSIBLE_WRONG_SIDE" for side in ("left", "right")) for item in assignments)
    ambiguous_frames = [index for index, item in enumerate(assignments)
                        if any(item[side] == "AMBIGUOUS" for side in ("left", "right"))]

    longest_missing_hand_frames = _longest_false_run(any_hand_present)
    longest_missing_hand_seconds = longest_missing_hand_frames / sequence.fps if sequence.fps else 0.0

    metrics = {
        "frame_count": frame_count,
        "fps": sequence.fps,
        "body_tracking_availability": float(pose_present.mean()),
        "left_hand_availability": float(left_present.mean()),
        "right_hand_availability": float(right_present.mean()),
        "longest_missing_hand_sequence_frames": int(longest_missing_hand_frames),
        "longest_missing_hand_sequence_seconds": float(longest_missing_hand_seconds),
        "left_right_swap_events": int(swap_events),
        "hand_assignment_ambiguous_frames": ambiguous_frames,
        "left_wrong_side_frames": [index for index, item in enumerate(assignments) if item["left"] == "POSSIBLE_WRONG_SIDE"],
        "right_wrong_side_frames": [index for index, item in enumerate(assignments) if item["right"] == "POSSIBLE_WRONG_SIDE"],
        "left_missing_intervals": _missing_intervals(left_present),
        "right_missing_intervals": _missing_intervals(right_present),
    }

    tracking_thresholds = thresholds.get("tracking", thresholds)
    review_reasons: list[str] = []
    status = "PASS"

    if metrics["body_tracking_availability"] < float(tracking_thresholds.get("body_availability_fail_below", 0.5)):
        status = "FAIL"
        review_reasons.append("Body tracking availability is below the fail threshold.")
    elif metrics["body_tracking_availability"] < float(tracking_thresholds.get("body_availability_review_below", 0.8)):
        status = "REVIEW"
        review_reasons.append("Body tracking availability is below the review threshold.")

    hand_review_threshold = float(tracking_thresholds.get("hand_availability_review_below", 0.2))
    if metrics["left_hand_availability"] < hand_review_threshold:
        status = _max_status(status, "REVIEW")
        review_reasons.append("Left hand tracking availability is low.")
    if metrics["right_hand_availability"] < hand_review_threshold:
        status = _max_status(status, "REVIEW")
        review_reasons.append("Right hand tracking availability is low.")

    if longest_missing_hand_seconds > float(tracking_thresholds.get("longest_missing_hand_review_seconds", 1.5)):
        status = _max_status(status, "REVIEW")
        review_reasons.append("A long missing-hand sequence was detected.")

    if swap_events >= int(tracking_thresholds.get("swap_events_review_at_or_above", 1)):
        status = _max_status(status, "REVIEW")
        review_reasons.append("Possible left/right hand swap events were detected.")
    if ambiguous_frames:
        status = _max_status(status, "REVIEW")
        review_reasons.append("Hand identity is ambiguous near overlapping body wrists; inspect the flagged frames.")

    return TrackingQcResult(
        technical_qc=status,
        isl_verified=False,
        metrics=metrics,
        review_reasons=review_reasons,
    )


def _count_possible_swap_events(sequence: PoseSequence) -> int:
    return sum(any(value == "POSSIBLE_WRONG_SIDE" for value in assess_hand_assignment(frame).values())
               for frame in sequence.frames)


def assess_hand_assignment(frame: PoseFrame, previous: PoseFrame | None = None, *, aspect_ratio: float = 1.0) -> dict[str, str]:
    """Assess each anatomical side independently; never swap on image x order."""
    result = {"left": "ABSENT", "right": "ABSENT"}
    pose = frame.pose_image
    scale = np.array([aspect_ratio, 1.0])
    reliable = pose.shape[0] > RIGHT_WRIST and np.isfinite(pose[[LEFT_WRIST, RIGHT_WRIST], :2]).all()
    if reliable and pose.shape[1] > 3:
        reliable = bool(np.isfinite(pose[[LEFT_WRIST, RIGHT_WRIST], 3]).all() and (pose[[LEFT_WRIST, RIGHT_WRIST], 3] >= 0.5).all())
    for side, own, other in (("left", LEFT_WRIST, RIGHT_WRIST), ("right", RIGHT_WRIST, LEFT_WRIST)):
        hand = getattr(frame, f"{side}_hand_image")
        if hand is None:
            continue
        if not reliable or not np.isfinite(hand[HAND_WRIST, :2]).all():
            result[side] = "UNVERIFIABLE"
            continue
        point = hand[HAND_WRIST, :2] * scale
        own_point, other_point = pose[own, :2] * scale, pose[other, :2] * scale
        separation = float(np.linalg.norm(own_point - other_point))
        own_distance = float(np.linalg.norm(point - own_point))
        other_distance = float(np.linalg.norm(point - other_point))
        if separation < 0.04 or abs(own_distance - other_distance) < 0.015:
            result[side] = "AMBIGUOUS"
        elif other_distance + 0.02 < own_distance:
            result[side] = "POSSIBLE_WRONG_SIDE"
        else:
            result[side] = "CONSISTENT"
        # Temporal position is only supporting evidence when body wrists are
        # stationary; fast intentional crossings cannot trigger automatic swaps.
        if previous is not None and result[side] == "CONSISTENT":
            prior_hand = getattr(previous, f"{side}_hand_image")
            prior_pose = previous.pose_image
            if prior_hand is not None and prior_pose.shape[0] > own and np.isfinite(prior_hand[0, :2]).all() and np.isfinite(prior_pose[own, :2]).all():
                body_step = float(np.linalg.norm((pose[own, :2] - prior_pose[own, :2]) * scale))
                hand_step = float(np.linalg.norm((hand[0, :2] - prior_hand[0, :2]) * scale))
                if body_step < 0.02 and hand_step > 0.2 and own_distance > 0.08:
                    result[side] = "AMBIGUOUS"
    return result


def _missing_intervals(values: np.ndarray) -> list[dict[str, int]]:
    intervals = []
    start = None
    for index, present in enumerate([*values, True]):
        if not present and start is None:
            start = index
        elif present and start is not None:
            intervals.append({"start_frame": start, "end_frame": index - 1, "frame_count": index - start})
            start = None
    return intervals


def _longest_false_run(values: np.ndarray) -> int:
    longest = 0
    current = 0
    for value in values:
        if value:
            longest = max(longest, current)
            current = 0
        else:
            current += 1
    return max(longest, current)


def _max_status(current: str, candidate: str) -> str:
    order = {"PASS": 0, "REVIEW": 1, "FAIL": 2}
    return candidate if order[candidate] > order[current] else current
