import json
import unittest
from decimal import Decimal
from tempfile import TemporaryDirectory
from pathlib import Path

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.overrides import CatalogOverrides, OverrideError, PartOverride, load
from coreyard.transform.render import render


class Loading(unittest.TestCase):
    def write(self, value):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "overrides.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_title_loads_by_stable_r_number(self):
        rules = load(self.write({"version": 1, "parts": {
            "42": {"title": "A Reviewed Title"}
        }}))
        self.assertEqual(rules.for_r_number("42").title, "A Reviewed Title")
        self.assertEqual(rules.for_r_number(42).title, "A Reviewed Title")

    def test_price_override_is_rejected(self):
        with self.assertRaises(OverrideError):
            load(self.write({"version": 1, "parts": {"42": {"price": "349.99"}}}))

    def test_unknown_keys_are_rejected(self):
        with self.assertRaises(OverrideError):
            load(self.write({"version": 1, "parts": {"42": {"titel": "typo"}}}))


class CanonicalRenderer(unittest.TestCase):
    def test_override_changes_the_one_rendered_product_and_fingerprint(self):
        part = Part(r_number="42", part_type="Engine Assembly", price=Decimal("100"))
        before = render(part)
        store = StoreProfile(overrides=CatalogOverrides({
            "42": PartOverride("Reviewed SEO Engine Title")
        }))
        after = render(part, store=store)
        self.assertEqual(after.title, "Reviewed SEO Engine Title")
        self.assertEqual(after.seo_title, "Reviewed SEO Engine Title")
        self.assertEqual(after.price, "100.00")
        self.assertNotEqual(after.fingerprint(), before.fingerprint())

    def test_long_reviewed_title_is_word_capped_for_shopify_seo(self):
        part = Part(r_number="42", part_type="Engine Assembly")
        title = ("2015 2016 2017 Example Motors Large Engine Assembly "
                 "Complete Tested OEM Replacement")
        store = StoreProfile(overrides=CatalogOverrides({
            "42": PartOverride(title=title)
        }))
        product = render(part, store=store)
        self.assertEqual(product.title, title)
        self.assertLessEqual(len(product.seo_title), 60)
        self.assertTrue(title.startswith(product.seo_title))

if __name__ == "__main__":
    unittest.main()
