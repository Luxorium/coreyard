"""Photo-share listing, offline: the SMB call is stubbed, the parsing is real."""

import unittest

from coreyard.config import SmbConfig
from coreyard.yms.images import SmbError, SmbImageStore

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


def _ls_output(names):
    head = "Domain=[W] OS=[W] Server=[W]\n"
    rows = "".join(
        f"  {n:<34}A   123456  Mon Jan  1 00:00:00 2024\n" for n in names
    )
    return head + rows + "                62914556 blocks of size 4096. 1 blocks available\n"


class DeleteInventoryImages(unittest.TestCase):
    """Offline: the SMB call is a stateful fake, the target resolution is real."""

    def setUp(self):
        self.store = SmbImageStore(CFG)
        self.share = {f"54898_{n:02d}.jpg" for n in range(1, 6)} | {
            "54898_10.jpg", "54898_11.jpg", "54898_12.jpg"}
        self.commands = []

        def fake_run(smb_command, cwd=None):
            self.commands.append(smb_command)
            if smb_command.startswith("cd ") and "; ls " in smb_command:
                stem = smb_command.split('ls "')[1].split('_*"')[0]
                return _ls_output(sorted(n for n in self.share
                                         if n.startswith(stem + "_")))
            if "; del " in smb_command:
                for token in smb_command.split("; ")[1:]:
                    self.share.discard(token[len('del "'):-1])
                return ""
            return ""

        self.store._run = fake_run
        # keep the backup path offline
        self.store._fetch = lambda subdir, names, dest_dir: [dest_dir / n for n in names]

    def test_plan_resolves_seqs_and_removes_nothing(self):
        res = self.store.delete_inventory_images("54898", ["04", "10", "11"], apply=False)
        self.assertEqual(res["targets"],
                         ["54898_04.jpg", "54898_10.jpg", "54898_11.jpg"])
        self.assertEqual(res["deleted"], [])
        self.assertNotIn("54898_10.jpg", " ".join(self.commands))  # no del issued
        self.assertIn("54898_10.jpg", self.share)

    def test_apply_deletes_only_the_named_frames(self):
        res = self.store.delete_inventory_images(
            "54898", ["10", "11", "12"], apply=True)
        self.assertEqual(set(res["deleted"]),
                         {"54898_10.jpg", "54898_11.jpg", "54898_12.jpg"})
        self.assertEqual(res["still_present"], [])
        self.assertNotIn("54898_10.jpg", self.share)
        self.assertIn("54898_01.jpg", self.share)  # a real part frame is untouched
        self.assertIn('del "54898_10.jpg"', " ".join(self.commands))

    def test_full_filename_and_bare_number_both_accepted(self):
        res = self.store.delete_inventory_images(
            "54898", ["54898_10.jpg", "4"], apply=False)
        self.assertEqual(res["targets"], ["54898_04.jpg", "54898_10.jpg"])

    def test_unknown_sequence_refuses(self):
        with self.assertRaises(SmbError):
            self.store.delete_inventory_images("54898", ["10", "99"], apply=True)
        self.assertIn("54898_10.jpg", self.share)  # nothing removed on refusal

    def test_cannot_name_another_parts_photo(self):
        with self.assertRaises(SmbError):
            self.store.delete_inventory_images("54898", ["510_01.jpg"], apply=True)

    def test_access_denied_surfaces(self):
        """If the share still lists a target after the delete, that is an error, not a pass."""
        self.store._run = lambda smb_command, cwd=None: _ls_output(
            ["54898_01.jpg", "54898_10.jpg"]) if "; ls " in smb_command else ""
        with self.assertRaises(SmbError):
            self.store.delete_inventory_images("54898", ["10"], apply=True)


if __name__ == "__main__":
    unittest.main()
