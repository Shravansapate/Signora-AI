import unittest

import numpy as np

from src.motion.quaternion_utils import (
    enforce_quaternion_continuity,
    quaternion_from_vectors,
    quaternion_to_matrix,
    validate_quaternions,
)


class QuaternionUtilsTests(unittest.TestCase):
    def test_vector_rotation_quaternion_is_unit_and_finite(self):
        q = quaternion_from_vectors(np.array([0, 1, 0]), np.array([1, 0, 0]))
        rotations = np.array([[[*q]]])
        validate_quaternions(rotations)
        matrix = quaternion_to_matrix(q)
        rotated = matrix @ np.array([0, 1, 0])
        np.testing.assert_allclose(rotated, [1, 0, 0], atol=1e-6)

    def test_continuity_flips_equivalent_negative_quaternion(self):
        rotations = np.array([[[1.0, 0.0, 0.0, 0.0]], [[-1.0, 0.0, 0.0, 0.0]]])
        continuous = enforce_quaternion_continuity(rotations)
        np.testing.assert_allclose(continuous[1, 0], [1.0, 0.0, 0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
