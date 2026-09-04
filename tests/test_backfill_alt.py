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
from coreyard.sink.backfill_alt import _completed, _iter_media, _parts_for, _updates_for


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
        todo = {"10": [("gid://File/shared", 1)],
                "20": [("gid://File/shared", 1)]}

        updates, owners, conflicts = _updates_for(todo, parts, StoreProfile())

        self.assertEqual(len(updates), 1)
        self.assertIn("Donor vehicle 2014 Subaru Legacy", updates[0]["alt"])
        self.assertEqual(owners, [{"10", "20"}])
        self.assertEqual(conflicts, {})


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
        todo = {"20": [(f"m{i}", i) for i in range(1, 31)]}
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


if __name__ == "__main__":
    unittest.main()
