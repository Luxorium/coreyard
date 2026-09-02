"""Publishing a part with photographs of the car it came off, when it has none of its own.

The invariant that matters most here is the one the delta path already had: the whole-share
resolver and the per-part resolver must spell an unchanged photo identically, or every delta
run re-fingerprints what it touches. Adding a second folder doubles the ways that can break.
"""

import unittest
from decimal import Decimal

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.run_sync import _photo_view
from coreyard.transform import seo

STORE = StoreProfile(vendor="Test Yard", city="Testville, TX")

# R# 1001 and donor vehicle 1001 both file a "1001_01.jpg". That collision is the point.
OWN = {"1001": [("1001_01.jpg", "1001_01.jpg|900|Mon Jan  1 00:00:00 2026")]}
DONOR_PHOTOS = {"1001": [("1001_01.jpg", "1001_01.jpg|111|Tue Feb  2 00:00:00 2026"),
                         ("1001_02.jpg", "1001_02.jpg|222|Tue Feb  2 00:00:00 2026")]}
DONORS = {"77": "1001"}          # stock number -> donor photo folder key


def part(**kw) -> Part:
    base = dict(r_number="1001", part_type="Grille", stock_number="77",
                year=2004, make="Dodge", model="Durango", price=Decimal("50"))
    base.update(kw)
    return Part(**base)


def view(base=None):
    return _photo_view(lambda k: OWN.get(k, []), lambda k: DONOR_PHOTOS.get(k, []),
                       DONORS, base)


class OwnPhotosWin(unittest.TestCase):
    def test_a_photographed_part_never_consults_the_donor_folder(self):
        p = part()
        self.assertEqual(["1001_01.jpg"], view().resolve(p))
        self.assertFalse(p.uses_donor_photos)

    def test_its_references_are_unchanged_by_the_fallback_existing(self):
        # The 9,461 products already published must not move.
        self.assertEqual(["1001_01.jpg"], view().resolve(part()))


class FallsBackToTheDonor(unittest.TestCase):
    def setUp(self):
        self.part = part(r_number="9999")      # no photos of its own

    def test_the_donor_photos_are_used_in_order(self):
        names = view().resolve(self.part)
        self.assertEqual(["donor:1001_01.jpg", "donor:1001_02.jpg"], names)
        self.assertTrue(self.part.uses_donor_photos)
        self.assertEqual("1001", self.part.donor_image_key)

    def test_a_donor_photo_cannot_collide_with_a_part_photo(self):
        donor_refs = view().resolve(part(r_number="9999"))
        own_refs = view().resolve(part(r_number="1001"))
        self.assertNotEqual(donor_refs[0], own_refs[0])

    def test_stamps_are_namespaced_too(self):
        for stamp in view().stamps(self.part):
            self.assertTrue(stamp.startswith("donor:"), stamp)

    def test_a_donor_with_no_photos_yields_nothing(self):
        orphan = part(r_number="9999", stock_number="no-such-donor")
        self.assertEqual([], view().resolve(orphan))
        self.assertFalse(orphan.uses_donor_photos)

    def test_a_public_base_url_points_at_the_donor_folder(self):
        self.assertEqual(
            ["https://cdn.test/1001/1001_01.jpg", "https://cdn.test/1001/1001_02.jpg"],
            view(base="https://cdn.test").resolve(self.part),
        )


class DonorShootIsTrimmed(unittest.TestCase):
    def test_only_the_first_frames_are_inherited(self):
        from coreyard.yms import images
        big = {"1001": [(f"1001_{n:02d}.jpg", f"1001_{n:02d}.jpg|1|t") for n in range(1, 40)]}
        photos = _photo_view(lambda k: {}.get(k, []), lambda k: big.get(k, []), DONORS, None)
        refs = photos.resolve(part(r_number="9999"))
        self.assertEqual(images.DONOR_PHOTO_LIMIT, len(refs))
        self.assertEqual("donor:1001_01.jpg", refs[0])

    def test_the_trim_is_the_same_for_stamps(self):
        from coreyard.yms import images
        big = {"1001": [(f"1001_{n:02d}.jpg", f"1001_{n:02d}.jpg|1|t") for n in range(1, 40)]}
        photos = _photo_view(lambda k: {}.get(k, []), lambda k: big.get(k, []), DONORS, None)
        p = part(r_number="9999")
        self.assertEqual(len(photos.resolve(p)), len(photos.stamps(p)))
        self.assertEqual(images.DONOR_PHOTO_LIMIT, len(photos.stamps(p)))


class BothResolversAgree(unittest.TestCase):
    """The full path lists each folder once; the delta path lists per key."""

    def test_identical_references_and_stamps_for_the_same_part(self):
        whole = _photo_view(lambda k: OWN.get(k, []),
                            lambda k: DONOR_PHOTOS.get(k, []), DONORS, None)
        per_key = _photo_view(lambda k: dict(OWN).get(k, []),
                              lambda k: dict(DONOR_PHOTOS).get(k, []), DONORS, None)
        for p in (part(), part(r_number="9999")):
            self.assertEqual(whole.resolve(p), per_key.resolve(p))
            self.assertEqual(whole.stamps(p), per_key.stamps(p))


class Disclosure(unittest.TestCase):
    def test_a_donor_photo_is_declared_in_the_description(self):
        p = part(r_number="9999")
        view().resolve(p)
        body = seo.build_body_html(p, STORE)
        self.assertIn("Photographs show the donor vehicle", body)
        self.assertIn("2004 Dodge Durango (Stock #77)", body)

    def test_a_photographed_part_says_nothing_new(self):
        # The stock lead already mentions an inventoried donor vehicle; what must not appear
        # is the disclosure that the *photographs* are of it.
        p = part()
        view().resolve(p)
        self.assertNotIn("Photographs show the donor vehicle",
                         seo.build_body_html(p, STORE))

    def test_alt_text_describes_the_car_that_is_in_the_picture(self):
        p = part(r_number="9999")
        view().resolve(p)
        self.assertIn("Donor vehicle 2004 Dodge Durango", seo.image_alt(p, 1, STORE))

    def test_alt_text_is_untouched_for_a_photographed_part(self):
        p = part()
        view().resolve(p)
        self.assertNotIn("Donor vehicle", seo.image_alt(p, 1, STORE))


if __name__ == "__main__":
    unittest.main()


class DonorFrameUploadedOnce(unittest.TestCase):
    """~7 parts share each donor, so its photographs must not be uploaded ~7 times."""

    class Images:
        def __init__(self):
            self.fetched = []

        def list_vehicle_images(self, key):
            return [f"{key}_01.jpg", f"{key}_02.jpg"]

        def fetch_vehicle(self, key, dest):
            self.fetched.append(key)
            made = []
            for name in self.list_vehicle_images(key):
                path = dest / name
                path.write_bytes(b"jpeg")
                made.append(path)
            return made

    class Client:
        def __init__(self):
            self.calls = []
            self.created = 0

        def graphql(self, query, variables=None):
            self.calls.append(query)
            if "stagedUploadsCreate" in query:
                return {"stagedUploadsCreate": {"userErrors": [], "stagedTargets": [
                    {"url": "https://upload.test", "resourceUrl": f"res://{i}",
                     "parameters": []}
                    for i, _ in enumerate(variables["input"])]}}
            if "fileCreate" in query:
                out = []
                for _ in variables["files"]:
                    self.created += 1
                    out.append({"id": f"gid://File/{self.created}", "fileStatus": "READY"})
                return {"fileCreate": {"files": out, "userErrors": []}}
            raise AssertionError(query)

    def _publisher(self, images, client, state):
        from coreyard.sink.shopify_write import ShopifyPublisher
        p = ShopifyPublisher.__new__(ShopifyPublisher)
        p.client, p.images, p.donor_files = client, images, state
        p.store, p.require_images = STORE, False
        return p

    def _state(self):
        import tempfile, pathlib
        from coreyard.state import SyncState
        self._tmp = tempfile.TemporaryDirectory()
        return SyncState(pathlib.Path(self._tmp.name) / "s.sqlite3")

    def setUp(self):
        import unittest.mock as mock
        self.images, self.client, self.state = self.Images(), self.Client(), self._state()
        self.pub = self._publisher(self.images, self.client, self.state)
        # The real uploader POSTs to the staged target; nothing here should reach a network.
        self.patch = mock.patch("requests.Session.post",
                                return_value=mock.Mock(raise_for_status=lambda: None))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.state.close)

    def _part(self):
        p = part(r_number="9999")
        p.donor_image_key, p.uses_donor_photos = "15", True
        return p

    def test_the_first_part_uploads_and_remembers(self):
        files = self.pub._staged_files(self._part(), lambda i: f"alt {i}")
        self.assertEqual([{"id": "gid://File/1"}, {"id": "gid://File/2"}], files)
        self.assertEqual("gid://File/1", self.state.donor_file_id("15", "15_01.jpg"))

    def test_the_next_part_off_that_donor_uploads_nothing(self):
        self.pub._staged_files(self._part(), lambda i: f"alt {i}")
        before = self.client.created
        files = self.pub._staged_files(self._part(), lambda i: f"alt {i}")
        self.assertEqual(before, self.client.created, "re-uploaded a known donor frame")
        self.assertEqual([{"id": "gid://File/1"}, {"id": "gid://File/2"}], files)
        self.assertEqual(1, len(self.images.fetched), "re-downloaded a known donor frame")

    def test_without_a_file_map_it_still_publishes(self):
        # An installation that passes no map is slower, not broken.
        self.pub.donor_files = None
        self.pub._fetch_photos = lambda part, dest: []
        self.assertEqual([], self.pub._staged_files(self._part(), lambda i: "alt"))
