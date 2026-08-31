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

    def test_title_and_price_load_by_stable_r_number(self):
        rules = load(self.write({"version": 1, "parts": {
            "42": {"title": "A Researched Title", "price": "349.99"}
        }}))
        self.assertEqual(rules.for_r_number("42").title, "A Researched Title")
        self.assertEqual(rules.for_r_number(42).price, Decimal("349.99"))

    def test_nonpositive_price_is_rejected(self):
        with self.assertRaises(OverrideError):
            load(self.write({"version": 1, "parts": {"42": {"price": "0"}}}))

    def test_unknown_keys_are_rejected(self):
        with self.assertRaises(OverrideError):
            load(self.write({"version": 1, "parts": {"42": {"titel": "typo"}}}))


class CanonicalRenderer(unittest.TestCase):
    def test_override_changes_the_one_rendered_product_and_fingerprint(self):
        part = Part(r_number="42", part_type="Engine Assembly", price=Decimal("100"))
        before = render(part)
        store = StoreProfile(overrides=CatalogOverrides({
            "42": PartOverride("Researched SEO Engine Title", Decimal("349.99"))
        }))
        after = render(part, store=store)
        self.assertEqual(after.title, "Researched SEO Engine Title")
        self.assertEqual(after.seo_title, "Researched SEO Engine Title")
        self.assertEqual(after.price, "349.99")
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

    def test_researched_price_does_not_receive_storefront_charm_rounding(self):
        part = Part(r_number="42", part_type="Engine", price=Decimal("100"))
        store = StoreProfile(overrides=CatalogOverrides({
            "42": PartOverride(price=Decimal("350.00"))
        }))
        self.assertEqual(render(part, store=store).price, "350.00")


if __name__ == "__main__":
    unittest.main()
