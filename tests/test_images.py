"""Photo-share listing, offline: the SMB call is stubbed, the parsing is real."""

import unittest

from coreyard.config import SmbConfig
from coreyard.yms.images import SmbImageStore

CFG = SmbConfig(host="h", server_name="s", user="u", password="p",
                images_share="share", inventory_subdir="Inventory", vehicle_subdir="Vehicle")

LISTING = """Domain=[WORKGROUP] OS=[Windows] Server=[Windows]
  .                                   D        0  Mon Jan  1 00:00:00 2024
  ..                                  D        0  Mon Jan  1 00:00:00 2024
  51_01.jpg                           A   123456  Mon Jan  1 00:00:00 2024
  51_10.jpg                           A   123456  Mon Jan  1 00:00:00 2024
  51_02.JPG                           A   123456  Mon Jan  1 00:00:00 2024
  510_01.jpg                          A   123456  Mon Jan  1 00:00:00 2024
  9_03.png                            A   123456  Mon Jan  1 00:00:00 2024
  thumbs.db                           A      100  Mon Jan  1 00:00:00 2024
  scan of invoice.pdf                 A      100  Mon Jan  1 00:00:00 2024
  51_notes.txt                        A      100  Mon Jan  1 00:00:00 2024
                62914556 blocks of size 4096. 30707712 blocks available
"""


class ListAll(unittest.TestCase):
    def setUp(self):
        self.store = SmbImageStore(CFG)
        self.store._run = lambda *a, **kw: LISTING

    def test_groups_by_r_number(self):
        index = self.store.list_all_inventory_images()
        self.assertEqual(index["51"], ["51_01.jpg", "51_02.JPG", "51_10.jpg"])
        self.assertEqual(index["9"], ["9_03.png"])

    def test_sequence_orders_numerically_not_lexically(self):
        """_10 must come after _02, or the fingerprint churns on ordering alone."""
        self.assertEqual(self.store.list_all_inventory_images()["51"][-1], "51_10.jpg")

    def test_similar_r_numbers_do_not_bleed(self):
        """R#51 must not absorb 510_01.jpg — the anchor is the whole numeric stem."""
        index = self.store.list_all_inventory_images()
        self.assertEqual(index["510"], ["510_01.jpg"])
        self.assertNotIn("510_01.jpg", index["51"])

    def test_non_photos_are_ignored(self):
        """A stray file in the folder must not invent an R# or join a real one."""
        index = self.store.list_all_inventory_images()
        self.assertNotIn("51_notes.txt", index["51"])
        self.assertEqual(set(index), {"51", "510", "9"})


if __name__ == "__main__":
    unittest.main()
