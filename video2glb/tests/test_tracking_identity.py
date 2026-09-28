from __future__ import annotations

import numpy as np

from src.tracking.pose_schema import PoseFrame, PoseSequence
from src.tracking.tracking_qc import assess_hand_assignment, evaluate_tracking


def frame(left_pose=(0.7, 0.5), right_pose=(0.3, 0.5), left_hand=(0.7, 0.5), right_hand=(0.3, 0.5)):
    pose = np.zeros((33, 4), dtype=np.float32)
    pose[:, 3] = 1.0
    pose[15, :2], pose[16, :2] = left_pose, right_pose
    def hand(point):
        if point is None:
            return None
        array = np.zeros((21, 3), dtype=np.float32)
        array[0, :2] = point
        return array
    return PoseFrame(0, 0, pose, pose.copy(), hand(left_hand), None, hand(right_hand), None,
                     {"pose_present": True, "left_hand_present": left_hand is not None,
                      "right_hand_present": right_hand is not None})


def test_single_wrong_side_hand_is_detected_without_second_hand():
    assessment = assess_hand_assignment(frame(left_hand=(0.3, 0.5), right_hand=None))
    assert assessment == {"left": "POSSIBLE_WRONG_SIDE", "right": "ABSENT"}


def test_crossed_hands_keep_anatomical_sides():
    crossed = frame(left_pose=(0.2, 0.5), right_pose=(0.8, 0.5),
                    left_hand=(0.2, 0.5), right_hand=(0.8, 0.5))
    assert assess_hand_assignment(crossed) == {"left": "CONSISTENT", "right": "CONSISTENT"}
    np.testing.assert_allclose(crossed.left_hand_image[0, :2], [0.2, 0.5])


def test_overlapping_wrists_are_flagged_ambiguous_without_relabeling():
    overlap = frame(left_pose=(0.5, 0.5), right_pose=(0.51, 0.5),
                    left_hand=(0.5, 0.5), right_hand=(0.51, 0.5))
    assert assess_hand_assignment(overlap) == {"left": "AMBIGUOUS", "right": "AMBIGUOUS"}
    result = evaluate_tracking(PoseSequence(25, 640, 480, [overlap]), {})
    assert result.technical_qc == "REVIEW"
    assert result.metrics["hand_assignment_ambiguous_frames"] == [0]


def test_low_confidence_body_wrists_do_not_support_side_reassignment():
    uncertain = frame(left_hand=(0.3, 0.5), right_hand=None)
    uncertain.pose_image[15:17, 3] = 0.1
    assert assess_hand_assignment(uncertain)["left"] == "UNVERIFIABLE"


def test_missing_intervals_are_recorded_per_hand():
    frames = [frame(), frame(left_hand=None), frame(left_hand=None), frame()]
    qc = evaluate_tracking(PoseSequence(25, 640, 480, frames), {})
    assert qc.metrics["left_missing_intervals"] == [{"start_frame": 1, "end_frame": 2, "frame_count": 2}]
    assert qc.metrics["right_missing_intervals"] == []
