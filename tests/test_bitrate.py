import unittest

from audiobind.encode import choose_bitrate


class TestChooseBitrate(unittest.TestCase):
    def test_rich_source_is_kept(self):
        self.assertEqual(choose_bitrate(256, "128k"), 256)

    def test_poor_source_is_lifted_to_the_floor(self):
        self.assertEqual(choose_bitrate(64, "128k"), 128)

    def test_unknown_source_uses_the_floor(self):
        self.assertEqual(choose_bitrate(0, "128k"), 128)

    def test_configured_floor_wins_over_a_lower_source(self):
        self.assertEqual(choose_bitrate(96, "192k"), 192)

    def test_floor_of_64_still_respects_a_better_source(self):
        self.assertEqual(choose_bitrate(256, "64k"), 256)

    def test_absurd_source_is_capped(self):
        self.assertEqual(choose_bitrate(500, "128k"), 320)

    def test_garbage_config_falls_back_to_128(self):
        self.assertEqual(choose_bitrate(0, "nonsense"), 128)


if __name__ == "__main__":
    unittest.main()
