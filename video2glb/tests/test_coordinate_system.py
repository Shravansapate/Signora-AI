import unittest

import numpy as np

from src.motion.coordinate_system import mediapipe_image_to_canonical, mediapipe_world_to_canonical


class CoordinateSystemTests(unittest.TestCase):
    def test_world_conversion_is_centralized_and_preserves_shape(self):
        points = np.array([[[1.0, 2.0, 3.0]]])
        converted = mediapipe_world_to_canonical(points)
        self.assertEqual(converted.shape, (1, 1, 3))
        np.testing.assert_allclose(converted[0, 0], [1.0, -2.0, -3.0])

    def test_image_conversion_centers_normalized_coordinates(self):
        points = np.array([[[0.5, 0.5, 0.1], [1.0, 0.0, -0.2]]])
        converted = mediapipe_image_to_canonical(points)
        np.testing.assert_allclose(converted[0, 0], [0.0, 0.0, -0.1])
        np.testing.assert_allclose(converted[0, 1], [0.5, 0.5, 0.2])

    def test_image_conversion_corrects_normalized_axes_for_aspect_ratio(self):
        points = np.array([[[1.0, 0.0, 0.25]]])
        converted = mediapipe_image_to_canonical(points, aspect_ratio=16.0 / 9.0)
        np.testing.assert_allclose(converted[0, 0], [8.0 / 9.0, 0.5, -4.0 / 9.0])


if __name__ == "__main__":
    unittest.main()
