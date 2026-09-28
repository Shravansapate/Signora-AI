import numpy as np
import pytest

from src.motion.arm_ik import solve_two_bone_endpoint


def check_lengths(result, shoulder, upper, forearm):
    elbow, wrist = np.array(result["elbow"]), np.array(result["wrist"])
    assert np.linalg.norm(elbow - shoulder) == pytest.approx(upper, rel=1e-9, abs=1e-10)
    assert np.linalg.norm(wrist - elbow) == pytest.approx(forearm, rel=1e-9, abs=1e-10)
    assert result["maximum_length_error"] < max(upper, forearm) * 1e-9


def test_unchanged_endpoint_preserves_old_elbow_exactly():
    result = solve_two_bone_endpoint([0, 0, 0], [1, 1, 0], [2, 0, 0], [2, 0, 0], np.sqrt(2), np.sqrt(2))
    assert result["status"] == "PASS"
    np.testing.assert_allclose(result["elbow"], [1, 1, 0], atol=1e-12)
    np.testing.assert_allclose(result["wrist"], [2, 0, 0], atol=1e-12)
    check_lengths(result, np.zeros(3), np.sqrt(2), np.sqrt(2))


def test_forward_endpoint_shift_preserves_lengths_and_bend_side():
    result = solve_two_bone_endpoint([0, 0, 0], [1, 1, 0], [2, 0, 0], [2, 0, 0.3], np.sqrt(2), np.sqrt(2))
    assert result["status"] == "PASS"
    assert result["elbow"][1] > 0
    np.testing.assert_allclose(result["wrist"], [2, 0, 0.3], atol=1e-12)
    check_lengths(result, np.zeros(3), np.sqrt(2), np.sqrt(2))


def test_unreachable_far_target_is_flagged_and_never_stretches():
    result = solve_two_bone_endpoint([0, 0, 0], [1, 1, 0], [2, 0, 0], [10, 0, 0], np.sqrt(2), np.sqrt(2))
    assert result["status"] == "REVIEW"
    assert not result["reachable"]
    assert result["endpoint_error"] == pytest.approx(10 - 2 * np.sqrt(2))
    check_lengths(result, np.zeros(3), np.sqrt(2), np.sqrt(2))


def test_unreachable_inner_target_preserves_unequal_bone_lengths():
    result = solve_two_bone_endpoint([0, 0, 0], [0, 3, 0], [1, 3, 0], [0.1, 0, 0], 3, 1)
    assert result["status"] == "REVIEW"
    assert not result["reachable"]
    np.testing.assert_allclose(result["wrist"], [2, 0, 0], atol=1e-12)
    check_lengths(result, np.zeros(3), 3, 1)


def test_exact_fold_at_shoulder_is_finite_and_keeps_previous_elbow_direction():
    result = solve_two_bone_endpoint([0, 0, 0], [0, 1, 0], [1, 1, 0], [0, 0, 0], 1, 1)
    assert result["reachable"]
    assert result["singularity"] == "wrist_at_shoulder"
    np.testing.assert_allclose(result["elbow"], [0, 1, 0], atol=1e-12)
    check_lengths(result, np.zeros(3), 1, 1)


def test_nearly_straight_arm_uses_hint_without_flipping_on_noise():
    elbows = []
    for noise in (1e-10, -1e-10, 0):
        result = solve_two_bone_endpoint([0, 0, 0], [1, noise, 0], [2, 0, 0], [1.8, 0, 0.01], 1, 1, bend_direction_hint=[0, 1, 0])
        assert result["status"] == "PASS"
        assert result["bend_source"] == "previous_bend_direction_hint"
        elbows.append(result["elbow"])
        check_lengths(result, np.zeros(3), 1, 1)
    np.testing.assert_allclose(elbows[0], elbows[1], atol=1e-12)
    assert elbows[0][1] > 0


def test_unobserved_straight_bend_is_reported_as_ambiguous():
    result = solve_two_bone_endpoint([0, 0, 0], [1, 0, 0], [2, 0, 0], [1.8, 0, 0], 1, 1)
    assert result["status"] == "REVIEW"
    assert result["reachable"]
    assert result["bend_plane_ambiguous"]
    check_lengths(result, np.zeros(3), 1, 1)


def test_antiparallel_wrist_axis_preserves_old_radial_side():
    result = solve_two_bone_endpoint([0, 0, 0], [1, 1, 0], [2, 0, 0], [-2, 0, 0], np.sqrt(2), np.sqrt(2))
    assert result["elbow"][1] > 0
    assert np.isfinite(result["elbow"]).all()
    check_lengths(result, np.zeros(3), np.sqrt(2), np.sqrt(2))


def test_solution_is_rigid_transform_and_scale_equivariant():
    shoulder = np.zeros(3)
    elbow, wrist, target = np.array([1, 1, 0]), np.array([2, 0, 0]), np.array([2, 0, 0.3])
    reference = solve_two_bone_endpoint(shoulder, elbow, wrist, target, np.sqrt(2), np.sqrt(2))
    rotation = np.array([[0, -1, 0], [0, 0, 1], [-1, 0, 0]])
    translation = np.array([34.0, -8.0, 27.0])
    scale = 100
    transform = lambda point: rotation @ point * scale + translation
    result = solve_two_bone_endpoint(transform(shoulder), transform(elbow), transform(wrist), transform(target), np.sqrt(2) * scale, np.sqrt(2) * scale)
    np.testing.assert_allclose(result["elbow"], transform(np.array(reference["elbow"])), atol=1e-10)
    np.testing.assert_allclose(result["wrist"], transform(np.array(reference["wrist"])), atol=1e-10)
    check_lengths(result, transform(shoulder), np.sqrt(2) * scale, np.sqrt(2) * scale)


def test_inputs_are_not_modified():
    shoulder, elbow, wrist, target = np.zeros(3), np.array([1., 1, 0]), np.array([2., 0, 0]), np.array([2., 0, .3])
    originals = [point.copy() for point in (shoulder, elbow, wrist, target)]
    solve_two_bone_endpoint(shoulder, elbow, wrist, target, np.sqrt(2), np.sqrt(2))
    for original, point in zip(originals, (shoulder, elbow, wrist, target)):
        np.testing.assert_array_equal(original, point)


@pytest.mark.parametrize("upper,forearm", [(0, 1), (-1, 1), (1, np.inf), (np.nan, 1)])
def test_invalid_lengths_are_rejected(upper, forearm):
    with pytest.raises(ValueError, match="lengths"):
        solve_two_bone_endpoint([0, 0, 0], [1, 1, 0], [2, 0, 0], [2, 0, 0], upper, forearm)


def test_nonfinite_target_is_rejected():
    with pytest.raises(ValueError, match="desired_wrist"):
        solve_two_bone_endpoint([0, 0, 0], [1, 1, 0], [2, 0, 0], [np.nan, 0, 0], 1, 1)
