"""The canonical renderer and the fingerprint that depends on it.

These are the regression tests for the bug that motivated the renderer: the sync used to
fingerprint one rendering of a product and publish another, so a change to the published
title, tags or SEO metadata left the stored hash untouched and every stale product was
classified "unchanged" forever. Each test below moves one shopper-visible field and insists
the fingerprint moves with it.
"""

import unittest
from decimal import Decimal
from unittest.mock import patch

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.profile import CatalogProfile
from coreyard.sink.shopify_api import part_to_product_set_input
from coreyard.transform.render import handle_prefix, r_number_from_handle, render
from coreyard.transform.shopify_product import part_to_rows
from coreyard.transform.weights import WeightRules
from coreyard.yms.interchange import Fitment

STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="90-day warranty")


def part(**kw) -> Part:
    base = dict(r_number="51", stock_number="251026", part_type="Alternator",
                price=Decimal("120.00"), year=2010, make="Honda", model="Civic",
                interchange_number="545-01883", interchange_code="01883", quantity=1)
    base.update(kw)
    return Part(**base)


def fingerprint(p: Part, images=(), store: StoreProfile = STORE) -> str:
    return render(p, list(images), store).fingerprint()


class Fingerprint(unittest.TestCase):
    def test_identical_input_is_identical_output(self):
        self.assertEqual(fingerprint(part()), fingerprint(part()))

    def test_unchanged_rendered_output_stays_unchanged(self):
        """A field nobody sees must not churn the catalogue: vin is internal."""
        self.assertEqual(fingerprint(part(vin="1HGCM82633A004352")), fingerprint(part()))

    def test_a_changed_generated_title_changes_the_fingerprint(self):
        with patch("coreyard.transform.seo.build_title",
                   side_effect=lambda p, store=None, **kw: "A DIFFERENT TITLE"):
            moved = fingerprint(part())
        self.assertNotEqual(moved, fingerprint(part()))

    def test_a_changed_generated_tag_set_changes_the_fingerprint(self):
        with patch("coreyard.transform.seo.build_tags",
                   side_effect=lambda p, store=None, **kw: ["one", "two"]):
            moved = fingerprint(part())
        self.assertNotEqual(moved, fingerprint(part()))

    def test_changed_seo_metadata_changes_the_fingerprint(self):
        with patch("coreyard.transform.seo.meta_description",
                   side_effect=lambda p, store=None: "different meta"):
            moved = fingerprint(part())
        self.assertNotEqual(moved, fingerprint(part()))
        with patch("coreyard.transform.seo.meta_title",
                   side_effect=lambda p, store=None: "different meta title"):
            moved = fingerprint(part())
        self.assertNotEqual(moved, fingerprint(part()))

    def test_a_changed_description_changes_the_fingerprint(self):
        with patch("coreyard.transform.seo.build_body_html",
                   side_effect=lambda p, store=None: "<p>different</p>"):
            moved = fingerprint(part())
        self.assertNotEqual(moved, fingerprint(part()))

    def test_price_moves_the_fingerprint(self):
        self.assertNotEqual(fingerprint(part(price=Decimal("121.00"))), fingerprint(part()))

    def test_inventory_moves_the_fingerprint(self):
        self.assertNotEqual(fingerprint(part(quantity=2)), fingerprint(part()))

    def test_weight_moves_the_fingerprint(self):
        heavy = StoreProfile(vendor="Test Yard",
                             weights=WeightRules(rules=(("alternator", 18.0),)))
        self.assertNotEqual(fingerprint(part(), store=heavy), fingerprint(part()))

    def test_photos_move_the_fingerprint(self):
        self.assertNotEqual(fingerprint(part(), ["51_01.jpg"]), fingerprint(part()))
        self.assertNotEqual(fingerprint(part(), ["51_01.jpg", "51_02.jpg"]),
                            fingerprint(part(), ["51_01.jpg"]))

    def test_alt_text_is_part_of_the_fingerprint(self):
        """Alt text is shopper-visible and feeds image search, so it is sync-controlled."""
        with patch("coreyard.transform.seo.image_alt",
                   side_effect=lambda p, i=1, store=None: f"different {i}"):
            moved = fingerprint(part(), ["51_01.jpg"])
        self.assertNotEqual(moved, fingerprint(part(), ["51_01.jpg"]))

    def test_the_fitment_key_is_covered_even_though_it_is_never_printed(self):
        self.assertNotEqual(fingerprint(part(interchange_code="99999")), fingerprint(part()))

    def test_site_policy_moves_the_fingerprint(self):
        """Changing what a listing claims is a change to the listing."""
        claims = StoreProfile(vendor="Test Yard", city="Testville, TX",
                              catalog=CatalogProfile(condition="Used, tested"))
        self.assertNotEqual(fingerprint(part(), store=claims), fingerprint(part()))

    def test_store_identity_moves_the_fingerprint(self):
        other = StoreProfile(vendor="Other Yard", city="Testville, TX",
                             warranty="90-day warranty")
        self.assertNotEqual(fingerprint(part(), store=other), fingerprint(part()))


class OneRepresentation(unittest.TestCase):
    """Both sinks serialize the same object, so neither can drift from the fingerprint."""

    def test_api_input_matches_the_rendered_product(self):
        product = render(part(), ["51_01.jpg"], STORE)
        api = part_to_product_set_input(part(), STORE)
        self.assertEqual(api["title"], product.title)
        self.assertEqual(api["descriptionHtml"], product.description_html)
        self.assertEqual(api["tags"], list(product.tags))
        self.assertEqual(api["productType"], product.product_type)
        self.assertEqual(api["vendor"], product.vendor)
        self.assertEqual(api["seo"], {"title": product.seo_title,
                                      "description": product.seo_description})
        self.assertEqual(api["variants"][0]["sku"], product.sku)
        self.assertEqual(api["variants"][0]["price"], product.price)

    def test_csv_row_matches_the_rendered_product(self):
        product = render(part(), ["a.jpg", "b.jpg"], STORE)
        rows = part_to_rows(part(), ["a.jpg", "b.jpg"], STORE)
        self.assertEqual(rows[0]["Title"], product.title)
        self.assertEqual(rows[0]["Body (HTML)"], product.description_html)
        self.assertEqual(rows[0]["Tags"], ", ".join(product.tags))
        self.assertEqual(rows[0]["SEO Title"], product.seo_title)
        self.assertEqual(rows[0]["SEO Description"], product.seo_description)
        self.assertEqual(rows[0]["Image Alt Text"], product.image_alts[0])
        self.assertEqual(rows[1]["Image Alt Text"], product.image_alts[1])

    def test_multi_model_fitment_reaches_both_sinks(self):
        """The API path always had fitment titles; the CSV path used to print the donor car."""
        p = part(fitment=[Fitment(make="HONDA", model="CIVIC", year_start=2006, year_end=2011),
                          Fitment(make="ACURA", model="CSX", year_start=2006, year_end=2011)])
        row = part_to_rows(p, [], STORE)[0]
        api = part_to_product_set_input(p, STORE)
        self.assertIn("Acura CSX", row["Title"])
        self.assertEqual(row["Title"], api["title"])


class Handles(unittest.TestCase):
    def test_round_trip(self):
        p = part(r_number="51")
        handle = render(p, [], STORE).handle
        self.assertEqual(handle, "coreyard-51")
        self.assertEqual(r_number_from_handle(handle, STORE), "51")

    def test_a_foreign_handle_is_not_ours(self):
        self.assertIsNone(r_number_from_handle("some-other-product", STORE))

    def test_prefix_matches_what_publishing_uses(self):
        self.assertEqual(handle_prefix(STORE), "coreyard-")


class Weights(unittest.TestCase):
    def test_the_rules_table_reaches_both_sinks(self):
        store = StoreProfile(vendor="Test Yard",
                             weights=WeightRules(rules=(("alternator", 18.0),),
                                                 unit="POUNDS"))
        product = render(part(), [], store)
        self.assertEqual(product.weight_value, 18.0)
        self.assertEqual(product.weight_unit, "POUNDS")
        self.assertEqual(product.weight_grams, 8165)

        api = part_to_product_set_input(part(), store)
        measurement = api["variants"][0]["inventoryItem"]["measurement"]["weight"]
        self.assertEqual(measurement, {"value": 18.0, "unit": "POUNDS"})

        row = part_to_rows(part(), [], store)[0]
        self.assertEqual(row["Variant Grams"], "8165")
        self.assertEqual(row["Variant Weight Unit"], "g")

    def test_a_source_weight_beats_the_estimate(self):
        """A measured weight is a fact; the table is a guess by part type."""
        store = StoreProfile(vendor="Test Yard",
                             weights=WeightRules(rules=(("alternator", 18.0),)))
        product = render(part(weight_grams=7000), [], store)
        self.assertEqual(product.weight_grams, 7000)
        self.assertEqual(product.weight_unit, "GRAMS")

    def test_no_table_means_no_weight_is_published(self):
        product = render(part(), [], STORE)
        self.assertIsNone(product.weight_value)
        api = part_to_product_set_input(part(), STORE)
        self.assertNotIn("inventoryItem", api["variants"][0])
        self.assertEqual(part_to_rows(part(), [], STORE)[0]["Variant Grams"], "")


if __name__ == "__main__":
    unittest.main()
