import numpy as np


def test_arm_spike_correction_is_bounded_and_preserves_clip_endpoints():
    from src.motion.smoothing import smooth_arm_rotation_spikes
    from src.motion.quaternion_utils import quaternion_from_axis_angle
    values = np.tile([1., 0., 0., 0.], (5, 1, 1))
    values[2, 0] = quaternion_from_axis_angle([1, 0, 0], np.radians(13))
    output, report = smooth_arm_rotation_spikes(values)
    assert report["status"] == "REVIEW"
    assert report["adjusted_sample_count"] == 1
    np.testing.assert_allclose(output[[0, -1]], values[[0, -1]])
    angle = np.degrees(2 * np.arccos(output[2, 0, 0]))
    assert np.isclose(angle, 10)


def test_arm_spike_filter_preserves_constant_angular_velocity_and_quaternion_signs():
    from src.motion.smoothing import smooth_arm_rotation_spikes
    from src.motion.quaternion_utils import quaternion_from_axis_angle
    values = np.array([[quaternion_from_axis_angle([0, 1, 0], np.radians(angle))] for angle in range(0, 60, 10)])
    values[::2] *= -1
    output, report = smooth_arm_rotation_spikes(values)
    np.testing.assert_allclose(output, values)
    assert report["status"] == "PASS"

from src.motion.smoothing import smooth_landmarks_centered


def test_centered_smoothing_does_not_shift_motion_peak():
    values = np.zeros((9, 1), dtype=float)
    values[4, 0] = 1.0
    result = smooth_landmarks_centered(values, radius=2)
    assert int(np.argmax(result[:, 0])) == 4
    assert result[3, 0] == result[5, 0]


def test_centered_smoothing_ignores_nan_samples():
    values = np.array([[0.0], [np.nan], [2.0]])
    result = smooth_landmarks_centered(values, radius=1)
    assert np.isfinite(result).all()
    assert result[1, 0] == 1.0
