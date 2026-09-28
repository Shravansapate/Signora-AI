import numpy as np
import pytest

from src.motion.skeleton_solver import _condition_arm_pole_targets


def _targets(elbow_x):
    targets = np.zeros((len(elbow_x), 4, 3))
    targets[:, 0] = [0, -1, 0]
    targets[:, 1] = [0.5, -0.5, 0]
    targets[:, 2] = [0, -1, 0]
    targets[:, 3, 0] = elbow_x
    targets[:, 3, 1] = -0.5
    return targets


def test_pole_singularity_is_conditioned_without_moving_wrists_or_reliable_elbows():
    targets = _targets([0.5, 0.05, 0.001, -0.001, -0.05, -0.5])
    before = targets.copy()
    result, report = _condition_arm_pole_targets(targets, np.zeros((2, 3)), np.array([2.0, 2.0]))
    np.testing.assert_array_equal(targets, before)
    np.testing.assert_array_equal(result[:, [0, 2]], targets[:, [0, 2]])
    np.testing.assert_array_equal(result[[0, 5], 3], targets[[0, 5], 3])
    np.testing.assert_array_equal(result[:, 1], targets[:, 1])
    projected = result[:, 3, [0, 2]]
    directions = projected / np.linalg.norm(projected, axis=1)[:, None]
    jumps = np.degrees(np.arccos(np.clip(np.sum(directions[:-1] * directions[1:], axis=1), -1, 1)))
    assert jumps.max() <= 36.000001
    assert np.min(np.linalg.norm(projected, axis=1)) >= 0.16 - 1e-12
    assert report["sides"]["Right"]["spans"] == [{"start_frame": 1, "end_frame": 4, "frame_count": 4, "reliable_boundary_count": 2}]


def test_well_observed_elbows_are_bitwise_unchanged():
    targets = _targets([0.5, 0.3, -0.3, -0.5])
    result, report = _condition_arm_pole_targets(targets, np.zeros((2, 3)), np.array([2.0, 2.0]))
    np.testing.assert_array_equal(result, targets)
    assert report["sides"]["Right"]["conditioned_frames"] == 0


def test_pole_conditioning_is_independent_of_avatar_unit_scale():
    targets = _targets([0.5, 0.005, -0.005, -0.5])
    baseline, _ = _condition_arm_pole_targets(targets, np.zeros((2, 3)), np.array([2.0, 2.0]))
    scaled, _ = _condition_arm_pole_targets(targets * 100, np.zeros((2, 3)), np.array([200.0, 200.0]))
    np.testing.assert_allclose(scaled / 100, baseline, atol=1e-12)


def test_unobserved_bend_plane_is_reported_and_finite():
    result, report = _condition_arm_pole_targets(_targets([0, 0, 0]), np.zeros((2, 3)), np.array([2.0, 2.0]))
    assert np.isfinite(result).all()
    assert report["sides"]["Right"]["unanchored_spans"] == 1


def test_missing_end_anchor_reuses_observed_plane():
    targets = _targets([0.5, 0.005, 0])
    result, _ = _condition_arm_pole_targets(targets, np.zeros((2, 3)), np.array([2.0, 2.0]))
    assert np.all(result[:, 3, 0] > 0)
    np.testing.assert_array_equal(result[:, 3, 2], np.zeros(3))


def test_nonfinite_target_cannot_be_silently_repaired():
    targets = _targets([0.5, np.nan])
    with pytest.raises(ValueError, match="finite"):
        _condition_arm_pole_targets(targets, np.zeros((2, 3)), np.array([2.0, 2.0]))
