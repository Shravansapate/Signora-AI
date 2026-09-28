import numpy as np
import pytest

from src.motion.depth_retargeting import retarget_arm_depth


def source(count=12):
    points = np.zeros((count, 33, 3))
    for shoulder, elbow, wrist in ((11, 13, 15), (12, 14, 16)):
        points[:, elbow] = [0.0, -0.3, 0.4]
        points[:, wrist] = [0.0, -0.6, 0.8]
    return points, np.ones((count, 33)), np.array([[10.0, 20.0], [20.0, 30.0]])


def test_observed_depth_uses_separate_avatar_segment_lengths():
    points, visibility, lengths = source()
    offsets, observed, report = retarget_arm_depth(points, visibility, lengths, 25)
    np.testing.assert_allclose(offsets, np.tile([24, 8, 40, 16], (12, 1)))
    assert observed.all() and report["status"] == "PASS"


def test_depth_is_invariant_to_source_translation_and_unit_scale():
    points, visibility, lengths = source()
    base, _, _ = retarget_arm_depth(points, visibility, lengths, 25)
    scaled, _, _ = retarget_arm_depth(points * 100 + [45, -20, 77], visibility, lengths, 25)
    np.testing.assert_allclose(base, scaled)


def test_observed_depth_changes_and_does_not_flatten_motion():
    points, visibility, lengths = source()
    points[6:, [13, 14], 2] *= -1
    points[6:, [15, 16], 2] *= -1
    offsets, _, _ = retarget_arm_depth(points, visibility, lengths, 25, smoothing=False)
    np.testing.assert_allclose(offsets[:6], -offsets[6:])


def test_short_depth_gap_interpolated_but_not_marked_observed():
    points, visibility, lengths = source()
    visibility[4:6, 15] = 0.1
    offsets, observed, report = retarget_arm_depth(points, visibility, lengths, 25)
    assert np.isfinite(offsets).all() and not observed[4:6, 0].any()
    assert report["sides"]["Left"]["interpolated_frames"] == 2
    assert report["status"] == "PASS"


def test_long_depth_gap_is_held_and_requires_review():
    points, visibility, lengths = source()
    visibility[2:10, 15] = 0.1
    offsets, _, report = retarget_arm_depth(points, visibility, lengths, 25)
    assert np.isfinite(offsets).all() and report["status"] == "REVIEW"
    assert report["sides"]["Left"]["held_frame_indices"] == list(range(3, 11))


def test_missing_arm_depth_fails_instead_of_inventing_a_wrist_plane():
    points, visibility, lengths = source()
    visibility[:, 15] = 0.0
    with pytest.raises(ValueError, match="No reliable Left source arm depth"):
        retarget_arm_depth(points, visibility, lengths, 25)
