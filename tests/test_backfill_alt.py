"""Photo-alt repair uses donor facts and never pays for unrelated fitment lookups."""

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.sink import backfill_alt
from coreyard.sink.backfill_alt import (Photo, _completed, _iter_media, _parts_for,
                                        _shows_donor, _updates_for)
from coreyard.transform import seo


class PartLoading(unittest.TestCase):
    def test_only_requested_parts_are_returned_without_resolving_fitment(self):
        parts = [
            Part(r_number="10", part_type="Door Glass", price=Decimal("40")),
            Part(r_number="20", part_type="Air Bag", price=Decimal("80")),
        ]

        resolver = SimpleNamespace(resolve=lambda part: setattr(part, "uses_donor_photos", True))
        with patch("coreyard.yms.inventory.fetch_parts_by_r_number",
                   return_value={"20": parts[1]}) as fetch, \
             patch("coreyard.run_sync._make_resolver", return_value=resolver):
            found = _parts_for({"20"})

        fetch.assert_called_once_with(["20"])
        self.assertEqual(list(found), ["20"])
        self.assertEqual(found["20"].fitment, [])
        self.assertTrue(found["20"].uses_donor_photos)

    def test_one_shared_donor_file_gets_one_donor_alt(self):
        parts = {
            r: Part(r_number=r, part_type=part_type, price=Decimal("40"),
                    year=2014, make="Subaru", model="Legacy", stock_number="261671",
                    uses_donor_photos=True)
            for r, part_type in (("10", "Alternator"), ("20", "Wiper Motor"))
        }
        todo = {"10": [Photo("gid://File/shared", 1, "", "88_01.jpg")],
                "20": [Photo("gid://File/shared", 1, "", "88_01.jpg")]}

        updates, owners, conflicts, unchanged = _updates_for(todo, parts, StoreProfile())

        self.assertEqual(len(updates), 1)
        self.assertIn("Donor vehicle 2014 Subaru Legacy", updates[0]["alt"])
        self.assertEqual(owners, [{"10", "20"}])
        self.assertEqual(conflicts, {})
        self.assertEqual(unchanged, 0)

    def test_a_shared_donor_file_named_after_one_part_is_corrected(self):
        """The live defect: one donor shoot, and the first part published named the file."""
        parts = {
            r: Part(r_number=r, part_type=part_type, price=Decimal("40"),
                    year=2019, make="Chevrolet", model="Malibu", stock_number="261667",
                    uses_donor_photos=True)
            for r, part_type in (("50931", "Blower Motor"), ("50930", "AC Compressor"))
        }
        stale = "Used OEM AC Compressor from 2019 Chevrolet Malibu"
        todo = {"50931": [Photo("gid://File/shared", 1, stale, "2872_01.jpg")],
                "50930": [Photo("gid://File/shared", 1, stale, "2872_01.jpg")]}

        updates, owners, conflicts, unchanged = _updates_for(todo, parts, StoreProfile())

        self.assertEqual(len(updates), 1)
        self.assertIn("Donor vehicle 2019 Chevrolet Malibu", updates[0]["alt"])
        self.assertNotIn("AC Compressor", updates[0]["alt"])
        self.assertEqual(unchanged, 0)

    def test_a_photo_already_reading_as_generated_is_not_rewritten(self):
        part = Part(r_number="10", part_type="Alternator", price=Decimal("40"),
                    year=2014, make="Subaru", model="Legacy")
        correct = seo.image_alt(part, 1, StoreProfile())
        todo = {"10": [Photo("gid://File/a", 1, correct, "10_01.jpg"),
                       Photo("gid://File/b", 2, "something else", "10_02.jpg")]}

        updates, owners, conflicts, unchanged = _updates_for(todo, {"10": part}, StoreProfile())

        self.assertEqual([u["id"] for u in updates], ["gid://File/b"])
        self.assertEqual(unchanged, 1)
        self.assertEqual(conflicts, {})

    def test_a_disagreement_is_still_a_conflict_when_one_owner_already_matches(self):
        """Skipping a matching owner early would let the other owner win unchallenged."""
        parts = {
            "10": Part(r_number="10", part_type="Alternator", price=Decimal("40"),
                       year=2014, make="Subaru", model="Legacy"),
            "20": Part(r_number="20", part_type="Wiper Motor", price=Decimal("40"),
                       year=2014, make="Subaru", model="Legacy"),
        }
        settled = seo.image_alt(parts["10"], 1, StoreProfile())
        todo = {"10": [Photo("gid://File/shared", 1, settled, "10_01.jpg")],
                "20": [Photo("gid://File/shared", 1, settled, "20_01.jpg")]}

        updates, owners, conflicts, unchanged = _updates_for(todo, parts, StoreProfile())

        self.assertEqual(updates, [])
        self.assertEqual(unchanged, 0)
        self.assertEqual(conflicts, {"gid://File/shared": {"10", "20"}})


class MediaPagination(unittest.TestCase):
    class Client:
        def __init__(self):
            self.calls = []

        def graphql(self, document, variables):
            self.calls.append(variables)
            return {
                "product": {
                    "media": {
                        "nodes": [{"id": "m11", "alt": "eleven"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": "m11"},
                    }
                }
            }

    def test_images_after_the_first_ten_are_included(self):
        client = self.Client()
        product = {
            "id": "gid://shopify/Product/1",
            "media": {
                "nodes": [{"id": f"m{i}", "alt": ""} for i in range(1, 11)],
                "pageInfo": {"hasNextPage": True, "endCursor": "m10"},
            },
        }

        found = list(_iter_media(client, product))

        self.assertEqual([item["id"] for item in found], [f"m{i}" for i in range(1, 12)])
        self.assertEqual(
            client.calls,
            [{"id": "gid://shopify/Product/1", "cursor": "m10"}],
        )


class ResumeLog(unittest.TestCase):
    class Client:
        def __init__(self):
            self.calls = 0

        def graphql(self, document, variables):
            self.calls += 1
            errors = [] if self.calls == 1 else [{"message": "second batch failed"}]
            return {"fileUpdate": {"userErrors": errors}}

    def test_a_product_is_not_complete_when_any_of_its_photo_batches_fails(self):
        todo = {"20": [Photo(f"m{i}", i, "", "20_%02d.jpg" % i) for i in range(1, 31)]}
        stats = {
            "products": 1,
            "ours": 1,
            "skipped_done": 0,
            "photos": 30,
            "already_set": 0,
        }
        part = Part(
            r_number="20",
            part_type="Air Bag",
            price=Decimal("80"),
            year=2020,
            make="Honda",
            model="Accord",
        )
        with TemporaryDirectory() as directory:
            log = Path(directory) / "alt.jsonl"
            args = SimpleNamespace(
                no_resume=True,
                dry_run=False,
                overwrite=True,
                max_pages=None,
                limit=None,
                log=log,
            )
            with (
                patch.object(backfill_alt, "load_store", return_value=StoreProfile()),
                patch.object(backfill_alt, "ShopifyClient", return_value=self.Client()),
                patch.object(backfill_alt, "_plan", return_value=(todo, stats)),
                patch.object(backfill_alt, "_parts_for", return_value={"20": part}),
            ):
                result = backfill_alt.run(args)

            self.assertEqual(result, 1)
            self.assertNotIn("20", _completed(log))


class WhosePhotographIsIt(unittest.TestCase):
    """The filename on the store decides, because that is the picture being served."""

    def test_a_donor_stem_wins_over_the_source_saying_own_photos(self):
        self.assertTrue(_shows_donor("2872_01_4bd8da44-ba6a.jpg", "50943", fallback=False))

    def test_the_parts_own_stem_is_its_own_photo(self):
        self.assertFalse(_shows_donor("50943_01.jpg", "50943", fallback=True))

    def test_no_filename_leaves_the_source_to_decide(self):
        self.assertTrue(_shows_donor("", "50943", fallback=True))
        self.assertFalse(_shows_donor("", "50943", fallback=False))

    def test_a_drifted_part_no_longer_conflicts_with_its_donors_other_parts(self):
        """R#50943 says it has its own photos; the store is still serving the donor shoot."""
        common = dict(price=Decimal("40"), year=2019, make="Chevrolet", model="Malibu",
                      stock_number="261667")
        parts = {
            "50931": Part(r_number="50931", part_type="Blower Motor",
                          uses_donor_photos=True, **common),
            "50943": Part(r_number="50943", part_type="AC Compressor",
                          uses_donor_photos=False, **common),
        }
        stale = "Used OEM AC Air Conditioning Compressor from 2019 Chevrolet Malibu"
        todo = {r: [Photo("gid://File/donor2872", 1, stale, "2872_01_4bd8da44.jpg")]
                for r in parts}

        updates, owners, conflicts, unchanged = _updates_for(todo, parts, StoreProfile())

        self.assertEqual(conflicts, {})
        self.assertEqual(len(updates), 1)
        self.assertIn("Donor vehicle 2019 Chevrolet Malibu", updates[0]["alt"])
        self.assertEqual(owners, [{"50931", "50943"}])


if __name__ == "__main__":
    unittest.main()
