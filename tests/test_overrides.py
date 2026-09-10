import json
import unittest
from decimal import Decimal
from tempfile import TemporaryDirectory
from pathlib import Path

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.overrides import CatalogOverrides, OverrideError, PartOverride, load
from coreyard.transform.render import render, resolve_shipping
from coreyard.transform.shipping import ShippingPolicy, ShippingPolicyError


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

    def test_a_ship_group_loads_on_its_own(self):
        """A part can have its shipping reviewed without its title being touched."""
        rules = load(self.write({"version": 1, "parts": {"29582": {"ship": "GROUND49"}}}))
        self.assertEqual(rules.for_r_number("29582").ship, "GROUND49")
        self.assertEqual(rules.for_r_number("29582").title, "")

    def test_title_and_ship_can_both_be_reviewed(self):
        rules = load(self.write({"version": 1, "parts": {
            "42": {"title": "A Reviewed Title", "ship": "PICKUP"}}}))
        self.assertEqual(rules.for_r_number("42").title, "A Reviewed Title")
        self.assertEqual(rules.for_r_number("42").ship, "PICKUP")

    def test_an_override_that_overrides_nothing_is_rejected(self):
        """It reads as a decision somebody made, and would silently do nothing."""
        with self.assertRaisesRegex(OverrideError, "title or ship"):
            load(self.write({"version": 1, "parts": {"42": {}}}))

    def test_an_empty_ship_is_rejected(self):
        with self.assertRaisesRegex(OverrideError, "ship must not be empty"):
            load(self.write({"version": 1, "parts": {"42": {"ship": "   "}}}))

    def test_a_part_with_no_override_ships_by_its_part_type(self):
        rules = load(self.write({"version": 1, "parts": {"42": {"ship": "PICKUP"}}}))
        self.assertEqual(rules.for_r_number("99").ship, "")


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


class ShippingOverride(unittest.TestCase):
    """One part shipping differently from its part type."""

    POLICY = ShippingPolicy.from_dict({
        "groups": {
            "A": {"tag": "ship:freight-299", "price": "299.99",
                  "match": ["radiator core supp"], "fulfillment": "yard"},
            "GROUND49": {"tag": "ship:ground-49", "price": "49.99",
                         "rate_name": "UPS Ground", "fulfillment": "external"},
            "GROUND": {"tag": "ship:free", "price": "0.00", "default": True,
                       "fulfillment": "external"},
        },
        "match_order": ["A"],
    })

    def part(self):
        return Part(r_number="29582", part_type="Radiator Core Support",
                    price=Decimal("150"))

    def test_without_an_override_the_part_type_decides(self):
        store = StoreProfile(shipping=self.POLICY)
        self.assertEqual(render(self.part(), store=store).shipping_group, "A")
        self.assertIn("ship:freight-299", render(self.part(), store=store).tags)

    def test_an_override_moves_only_that_part(self):
        store = StoreProfile(shipping=self.POLICY, overrides=CatalogOverrides({
            "29582": PartOverride(ship="GROUND49")}))
        product = render(self.part(), store=store)
        self.assertEqual(product.shipping_group, "GROUND49")
        self.assertIn("ship:ground-49", product.tags)
        self.assertNotIn("ship:freight-299", product.tags)
        # Its neighbour on the same part type is untouched.
        sibling = Part(r_number="29583", part_type="Radiator Core Support")
        self.assertIn("ship:freight-299", render(sibling, store=store).tags)

    def test_a_group_outside_match_order_is_unreachable_by_pattern(self):
        """GROUND49 exists for the override alone; no part type may fall into it."""
        for part_type in ("Radiator Core Support", "Alternator", "Engine Assembly"):
            with self.subTest(part_type=part_type):
                group = self.POLICY.classify(part_type, part_type)
                self.assertNotEqual(group.id, "GROUND49")

    def test_an_unknown_group_is_refused_rather_than_quietly_falling_back(self):
        """Falling back would publish the part-type rate the override exists to prevent."""
        store = StoreProfile(shipping=self.POLICY)
        with self.assertRaisesRegex(ShippingPolicyError, "GROUND99"):
            resolve_shipping(self.part(), store, "Radiator Core Support", "GROUND99")

    def test_the_override_changes_the_fingerprint_so_the_part_republishes(self):
        plain = StoreProfile(shipping=self.POLICY)
        moved = StoreProfile(shipping=self.POLICY, overrides=CatalogOverrides({
            "29582": PartOverride(ship="GROUND49")}))
        self.assertNotEqual(render(self.part(), store=moved).fingerprint(),
                            render(self.part(), store=plain).fingerprint())
