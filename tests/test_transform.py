import csv
import io
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.sink.shopify_api import part_to_product_set_input
from coreyard.transform import seo, shopify_csv
from coreyard.transform.shopify_product import (
    build_tags,
    build_title,
    handle_for,
    part_to_rows,
)


STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="90-day warranty")


def sample_part(**kw) -> Part:
    base = dict(
        r_number="51",
        stock_number="251026",
        interchange_number="545-01883",
        interchange_code="01883",
        part_type="Engine Control Module ECM",
        year=2014,
        make="Ford",
        model="Fusion",
        price=Decimal("79.95"),
        quantity=1,
        mileage=88000,
        grade="A",
    )
    base.update(kw)
    return Part(**base)


class TitleAndHandle(unittest.TestCase):
    def test_handle_stable_and_safe(self):
        self.assertEqual(handle_for(sample_part(), STORE), "coreyard-51")
        self.assertEqual(handle_for(sample_part(r_number="R 12/3"), STORE), "coreyard-r-12-3")

    def test_handle_prefix_is_configurable_and_slugged(self):
        # An existing store keeps publishing under the prefix it launched with.
        legacy = StoreProfile(vendor="Test Yard", handle_prefix="oldyard")
        self.assertEqual(handle_for(sample_part(), legacy), "oldyard-51")
        messy = StoreProfile(vendor="Test Yard", handle_prefix="My Yard!")
        self.assertEqual(handle_for(sample_part(), messy), "my-yard-51")

    def test_stock_number_does_not_control_identity(self):
        a = sample_part(r_number="51", stock_number="251026")
        b = sample_part(r_number="52", stock_number="251026")
        self.assertNotEqual(handle_for(a, STORE), handle_for(b, STORE))

    def test_title_fitment(self):
        self.assertEqual(build_title(sample_part()), "2014 Ford Fusion Engine Control Module ECM")

    def test_title_falls_back_to_part_type(self):
        p = sample_part(year=None, make=None, model=None)
        self.assertEqual(build_title(p), "Engine Control Module ECM")

    def test_title_trimmed_at_word_boundary(self):
        p = sample_part(part_type="X " * 200)
        t = build_title(p)
        self.assertLessEqual(len(t), 255)
        self.assertFalse(t.endswith(" "))


class Tags(unittest.TestCase):
    def test_tags_dedupe_and_order(self):
        tags = build_tags(sample_part())
        self.assertEqual(tags[0], "2014")
        self.assertIn("Ford Fusion", tags)
        self.assertIn("Used OEM", tags)
        self.assertIn("Interchange 545-01883", tags)
        self.assertEqual(len(tags), len(set(t.lower() for t in tags)))


class Rows(unittest.TestCase):
    def test_primary_plus_image_rows(self):
        rows = part_to_rows(sample_part(), ["u1", "u2", "u3"], STORE)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["Variant SKU"], "51")
        self.assertEqual(rows[0]["Variant Price"], "79.95")
        self.assertEqual(rows[0]["Image Position"], "1")
        # continuation rows: handle + image only, no Title
        self.assertEqual(rows[1]["Handle"], "coreyard-51")
        self.assertEqual(rows[1]["Image Position"], "2")
        self.assertNotIn("Title", rows[1])

    def test_no_images(self):
        rows = part_to_rows(sample_part(), [], STORE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Image Src"], "")

    def test_unlistable_skipped_in_csv(self):
        parts = [sample_part(), sample_part(r_number="52", price=None)]
        out = list(shopify_csv.rows_for_parts(parts))
        skus = [r.get("Variant SKU") for r in out if r.get("Title")]
        self.assertEqual(skus, ["51"])

    def test_identifiers_are_not_mixed_up(self):
        row = part_to_rows(sample_part(), [], STORE)[0]
        body = row["Body (HTML)"]
        self.assertEqual(row["Variant SKU"], "51")
        self.assertIn("<strong>R#:</strong> 51", body)
        self.assertIn("<strong>Stock #:</strong> 251026", body)
        self.assertIn("<strong>Interchange #:</strong> 545-01883", body)

    def test_direct_api_uses_r_number_as_sku(self):
        product = part_to_product_set_input(sample_part(), STORE)
        self.assertEqual(product["variants"][0]["sku"], "51")

    def test_photos_are_keyed_by_r_number(self):
        self.assertEqual(sample_part().image_key(), "51")

    def test_missing_grade_and_mileage_are_described_safely(self):
        body = seo.build_body_html(sample_part(grade=None, mileage=None))
        self.assertIn("<strong>Condition:</strong> Used, tested", body)
        self.assertNotIn("<strong>Mileage:</strong>", body)

    def test_customer_content_does_not_name_interchange_vendor(self):
        part = sample_part()
        content = " ".join([
            seo.build_title(part),
            seo.meta_title(part),
            seo.meta_description(part),
            seo.build_body_html(part),
            " ".join(seo.build_tags(part)),
        ])
        prohibited_name = "".join(("holl", "ander"))
        self.assertNotIn(prohibited_name, content.lower())


class CsvWriter(unittest.TestCase):
    def test_write_csv_roundtrip(self):
        parts = [sample_part(), sample_part(r_number="52", part_type="Headlight")]
        resolver = lambda p: ["https://img/%s_01.jpg" % p.image_key()]
        with TemporaryDirectory() as d:
            path = Path(d) / "products.csv"
            products, rows = shopify_csv.write_csv(parts, path, resolver, STORE)
            self.assertEqual(products, 2)
            text = path.read_text(encoding="utf-8")
        reader = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(reader[0]["Handle"], "coreyard-51")
        self.assertEqual(reader[0]["Image Src"], "https://img/51_01.jpg")
        self.assertEqual(reader[0]["Variant Inventory Policy"], "deny")
        self.assertTrue(reader[0]["Body (HTML)"].startswith("<p>"))
        # header contains the Shopify-recognized columns
        self.assertIn("Variant Price", reader[0].keys())


if __name__ == "__main__":
    unittest.main()
