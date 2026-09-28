import numpy as np


def test_signed_distance_to_extended_triangle_plane_is_not_penetration_evidence():
    from src.qc.mesh_clearance import supported_face_projection
    assert supported_face_projection([0, 0, -0.1], [0, 0, 0], [0, 0, 1], 1.0)
    assert supported_face_projection([0, 0, 0.1], [0, 0, 0], [0, 0, 1], 1.0)
    # Beyond the finite patch, the negative plane distance is ambiguous.
    assert not supported_face_projection([0.3, 0, -0.1], [0, 0, 0], [0, 0, 1], 1.0)
    assert not supported_face_projection([0, 0, -0.1], [0, 0, 0], [0, 0, 0], 1.0)
import pytest

from src.qc.mesh_clearance import minimum_front_surface_correction, clearance_envelope


def test_clearance_envelope_preserves_required_depth_and_has_compact_support():
    needed = np.zeros((21, 2))
    needed[10, 0] = 0.2
    actual = clearance_envelope(needed, radius=5)
    assert np.all(actual >= needed)
    assert actual[10, 0] == 0.2
    assert actual.max() == 0.2
    assert np.all(actual[:, 1] == 0.0)
    np.testing.assert_allclose(actual[:10, 0], actual[11:, 0][::-1])
    assert np.all(actual[:6] == 0.0) and np.all(actual[15:] == 0.0)


def test_clearance_envelope_rejects_invalid_input():
    with pytest.raises(ValueError):
        clearance_envelope(np.array([[-1.0]]), radius=2)
    with pytest.raises(ValueError):
        clearance_envelope(np.zeros((3, 2)), radius=0)


def surface(depth=0.2):
    return np.array([[-0.5, -0.5, depth], [0.5, -0.5, depth], [0.5, 0.5, depth], [-0.5, 0.5, depth]]), np.array([[0, 1, 2], [0, 2, 3]])


def correction(points, *, mask=None, **options):
    vertices, triangles = surface()
    points = np.asarray(points, dtype=float)
    return minimum_front_surface_correction(points, vertices, triangles, np.array([0.0, 0.0, 1.0]), 1.0, penetrating_mask=np.ones(len(points), dtype=bool) if mask is None else mask, **options)


def test_small_penetration_gets_minimum_rigid_depth_offset():
    result = correction([[0.1, 0.1, 0.19], [-0.1, 0.2, 0.185]])
    assert result["status"] == "PASS"
    assert result["automatic_correction_allowed"]
    np.testing.assert_allclose(result["translation"], [0, 0, 0.015], atol=1e-10)
    assert result["supported_point_count"] == 2


def test_contact_does_not_get_an_artificial_gap():
    result = correction([[0, 0, 0.2]])
    assert not result["correction_required"]
    assert result["translation"] == [0, 0, 0]


def test_unconfirmed_or_behind_body_points_are_never_pushed():
    result = correction([[0, 0, -10.0]], mask=np.array([False]))
    assert result["translation"] == [0, 0, 0]
    assert result["confirmed_penetrating_point_count"] == 0


def test_large_offset_requires_review_and_is_not_applied():
    result = correction([[0, 0, 0.0]])
    assert result["status"] == "REVIEW"
    assert not result["automatic_correction_allowed"]
    assert result["translation"] == [0, 0, 0]
    np.testing.assert_allclose(result["proposed_translation"], [0, 0, 0.2])


def test_uncovered_penetration_requires_review_without_guessing():
    result = correction([[2, 0, 0.19]])
    assert result["status"] == "REVIEW"
    assert result["supported_point_count"] == 0
    assert result["required_shift"] is None
    assert result["translation"] == [0, 0, 0]


def test_multiple_clothing_layers_clear_the_outermost_surface():
    vertices, triangles = surface(0.19)
    outer, outer_triangles = surface(0.2)
    result = minimum_front_surface_correction(np.array([[0, 0, 0.185]]), np.concatenate([vertices, outer]), np.concatenate([triangles, outer_triangles + len(vertices)]), np.array([0, 0, 1]), 1.0, penetrating_mask=np.array([True]))
    assert result["status"] == "PASS"
    np.testing.assert_allclose(result["translation"], [0, 0, 0.015], atol=1e-10)


def test_correction_is_invariant_to_rotation_translation_and_scale():
    vertices, triangles = surface()
    rotation = np.array([[0, -1, 0], [0, 0, 1], [-1, 0, 0]], dtype=float)
    # Proper rotation; depth now points along world Y.
    assert np.isclose(np.linalg.det(rotation), 1.0)
    translation = np.array([23.0, -41.0, 72.0])
    scale = 100.0
    vertices = vertices @ rotation.T * scale + translation
    points = np.array([[0.1, 0.1, 0.19]]) @ rotation.T * scale + translation
    result = minimum_front_surface_correction(points, vertices, triangles, rotation[:, 2], scale, penetrating_mask=np.array([True]))
    np.testing.assert_allclose(result["translation"], rotation[:, 2], atol=1e-10)
    assert result["required_shift_shoulder_widths"] == pytest.approx(0.01)


def test_reversed_winding_is_not_a_supported_surface():
    vertices, triangles = surface()
    result = minimum_front_surface_correction(np.array([[0, 0, 0.19]]), vertices, triangles[:, ::-1], np.array([0, 0, 1]), 1.0, penetrating_mask=np.array([True]))
    assert result["status"] == "REVIEW"
    assert not result["automatic_correction_allowed"]


def test_invalid_triangle_indices_and_implicit_masks_are_rejected():
    vertices, triangles = surface()
    with pytest.raises(ValueError, match="triangle indices"):
        minimum_front_surface_correction(np.array([[0, 0, 0.19]]), vertices, triangles + 10, np.array([0, 0, 1]), 1.0, penetrating_mask=np.array([True]))
    with pytest.raises(ValueError, match="Boolean"):
        correction([[0, 0, 0.19]], mask=np.array([1]))


def test_normalizing_front_does_not_mutate_caller_geometry():
    vertices, triangles = surface()
    front = np.array([0.0, 0.0, 10.0])
    minimum_front_surface_correction(np.array([[0, 0, 0.19]]), vertices, triangles, front, 1.0, penetrating_mask=np.array([True]))
    np.testing.assert_array_equal(front, [0, 0, 10])
