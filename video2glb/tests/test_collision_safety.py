import json
import unittest

import numpy as np

from src.qc.collision_safety import (
    CollisionSafetyConfig,
    derive_anatomical_torso_frame,
    evaluate_torso_hand_clearance,
)


def _pose_frame() -> np.ndarray:
    pose = np.full((33, 4), np.nan, dtype=np.float64)
    pose[11] = [1.0, 1.0, 0.0, 1.0]   # labelled left shoulder
    pose[12] = [-1.0, 1.0, 0.0, 1.0]  # labelled right shoulder
    pose[23] = [0.6, 0.0, 0.0, 1.0]   # labelled left hip
    pose[24] = [-0.6, 0.0, 0.0, 1.0]  # labelled right hip
    pose[15] = [0.0, 0.5, 0.3, 1.0]
    pose[16] = [0.0, 0.5, 0.3, 1.0]
    return pose


def _sequence(count: int) -> np.ndarray:
    return np.stack([_pose_frame() for _ in range(count)])


def _hands(clearances: list[float], *, x: float = 0.0, y: float = 0.5) -> np.ndarray:
    # Shoulder width is 2, so z == 2 * normalized clearance.
    return np.asarray([[[x, y, 2.0 * clearance]] for clearance in clearances], dtype=np.float64)


class CollisionSafetyTests(unittest.TestCase):
    def test_derives_orthonormal_anatomical_frame_and_projects_points(self):
        torso = derive_anatomical_torso_frame(_pose_frame())
        self.assertIsNotNone(torso)
        assert torso is not None
        np.testing.assert_allclose(torso.origin, [0.0, 0.0, 0.0])
        np.testing.assert_allclose(torso.lateral, [1.0, 0.0, 0.0])
        np.testing.assert_allclose(torso.up, [0.0, 1.0, 0.0])
        np.testing.assert_allclose(torso.front, [0.0, 0.0, 1.0])
        np.testing.assert_allclose(torso.project(np.array([[0.5, 0.5, 0.2]])), [[0.25, 0.5, 0.1]])

    def test_full_clip_safe_clearance_passes_and_is_json_safe(self):
        report = evaluate_torso_hand_clearance(_sequence(3), _hands([0.10, 0.20, 0.05]))
        self.assertEqual(report["status"], "PASS")
        self.assertFalse(report["mesh_aware"])
        self.assertTrue(report["evaluated_every_input_frame"])
        self.assertEqual(report["frames"]["evaluated"], 3)
        self.assertEqual(len(report["frame_results"]), 3)
        self.assertAlmostEqual(report["minimum_normalized_clearance"], 0.05)
        json.dumps(report, allow_nan=False)

    def test_shallow_clearance_routes_frame_to_review(self):
        report = evaluate_torso_hand_clearance(
            _sequence(2),
            _hands([0.20, 0.02]),
            frame_indices=[100, 101],
        )
        self.assertEqual(report["status"], "REVIEW")
        self.assertEqual(report["risky_frame_indices"], [101])
        self.assertEqual(report["review_frame_indices"], [101])
        self.assertEqual(report["fail_frame_indices"], [])

    def test_deep_penetration_in_any_frame_fails_full_clip(self):
        report = evaluate_torso_hand_clearance(
            _sequence(3),
            left_hand_points=_hands([0.2, -0.12, 0.2]),
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["fail_frame_indices"], [1])
        self.assertEqual(report["frames"]["fail"], 1)
        self.assertAlmostEqual(report["minimum_normalized_clearance"], -0.12)
        self.assertIn(1, report["sides"]["left"]["fail_frame_indices"])

    def test_behind_torso_point_outside_central_region_does_not_false_fail(self):
        report = evaluate_torso_hand_clearance(
            _sequence(2),
            left_hand_points=_hands([-0.5, -0.5], x=1.2),
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["frames"]["central_region"], 0)
        self.assertIsNone(report["minimum_normalized_clearance"])

    def test_missing_torso_or_hand_coverage_requires_review(self):
        poses = _sequence(2)
        poses[1, 11, :3] = np.nan
        hands = _hands([0.2, 0.2])
        report = evaluate_torso_hand_clearance(
            poses,
            hands,
            config=CollisionSafetyConfig(minimum_evaluated_coverage=0.75),
        )
        self.assertEqual(report["status"], "REVIEW")
        self.assertEqual(report["frames"]["evaluated"], 1)
        self.assertEqual(report["frames"]["evaluated_coverage"], 0.5)
        self.assertEqual(report["frame_results"][1]["status"], "NOT_EVALUATED")
        self.assertTrue(report["warnings"])

    def test_optional_wrist_fallback_is_disclosed(self):
        report = evaluate_torso_hand_clearance(
            _sequence(1),
            config=CollisionSafetyConfig(use_pose_wrist_fallback=True),
        )
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["sides"]["left"]["pose_wrist_fallback_frames"], 1)
        self.assertIn("Pose-wrist fallback", report["warnings"][0])

    def test_rejects_mismatched_hand_frame_count(self):
        with self.assertRaisesRegex(ValueError, "left_hand_points"):
            evaluate_torso_hand_clearance(_sequence(2), _hands([0.2]))


if __name__ == "__main__":
    unittest.main()
