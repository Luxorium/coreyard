"""DATA-03: one canonical product, and everything that serializes it.

`render()` produces the object CoreYard considers shopper-visible for a part. The Admin API
sink serializes it, the CSV writer serializes it, and the sync fingerprints it — three
consumers of one object, which is the only arrangement in which "the storefront is up to
date" can mean anything. Fingerprinting one rendering while publishing another is the
failure this design exists to prevent: the stored hash stops moving, every stale product
reads as unchanged, and the storefront keeps output no current version of the code would
produce.

The tests below are mechanical on purpose. For each field of the canonical product they ask
whether changing it moves the fingerprint, reaches the API input, and reaches the CSV row —
and a field that does not do one of those has to be named here with a reason. A field added
to the renderer and forgotten by a sink therefore fails a test rather than quietly failing
to publish.
"""

import dataclasses
import unittest
from decimal import Decimal

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.sink.shopify_api import product_set_input
from coreyard.transform.pricing import money_str
from coreyard.transform.render import RenderedProduct, render
from coreyard.transform.shopify_product import part_to_rows, primary_row

STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="90-day warranty",
                     handle_prefix="test")

PART = Part(r_number="51", part_type="Alternator", price=Decimal("120.00"), quantity=2,
            year=2010, make="Honda", model="Civic")


def rendered(**overrides) -> RenderedProduct:
    # The base carries a weight, because the API sends a measurement only when it has both
    # a value and a unit: without one, changing the other reaches nothing and the test would
    # be asking about the fixture rather than about the sink.
    product = dataclasses.replace(
        render(PART, ["https://example.invalid/a.jpg"], STORE),
        weight_value=12.0, weight_unit="POUNDS", weight_grams=5443)
    return dataclasses.replace(product, **overrides) if overrides else product


# One different value per field, used to ask "does changing this reach the sink?".
OTHER_VALUE = {
    "handle": "test-52",
    "title": "Something Else",
    "description_html": "<p>different</p>",
    "vendor": "Another Yard",
    "product_type": "Starter",
    "tags": ("different",),
    "seo_title": "different seo",
    "seo_description": "different seo description",
    "sku": "52",
    "price": "999.00",
    "inventory": 41,
    "images": ("https://example.invalid/b.jpg",),
    "image_alts": ("different alt",),
    "weight_value": 42.0,
    "weight_unit": "GRAMS",
    "weight_grams": 4242,
    "metafields": (),
    "shipping_group": "freight",
    "fitment_key": "different-key",
}

# Fields the Admin API sink does not carry, each for a reason that is about the API rather
# than about the field being unimportant.
API_EXEMPT = {
    "images": "media is a separate mutation: `files` is built from staged uploads",
    "image_alts": "same — alt text is attached to the staged file, not to productSet",
    "weight_grams": "the API takes the resolved value and unit; grams is the CSV's spelling",
    "inventory": "carried, but as `inventoryQuantities`, which needs the location the "
                 "publisher holds — asserted directly in TheQuantityIsTheRenderedOne",
    "shipping_group": "a record of a decision already expressed in `tags`",
    "fitment_key": "not printed; it drives the description, which is carried",
}

# Fields the CSV importer has no column for.
CSV_EXEMPT = {
    "metafields": "Shopify's product CSV has no metafield columns",
    "weight_value": "the CSV carries grams plus a display unit, from the same resolution",
    "weight_unit": "the importer takes grams, so the column states the unit of that number",
    "shipping_group": "a record of a decision already expressed in `Tags`",
    "fitment_key": "not printed; it drives Body (HTML), which is carried",
}


def api_input(product: RenderedProduct) -> dict:
    return product_set_input(product, "ACTIVE", [], (), True, ())


class TheFieldsAreAccountedFor(unittest.TestCase):
    """Every field of the canonical product either reaches a sink or is named here."""

    FIELDS = [f.name for f in dataclasses.fields(RenderedProduct)]

    def test_the_test_has_a_value_for_every_field(self):
        """A field added to the renderer must be decided about, not defaulted past."""
        self.assertEqual(sorted(OTHER_VALUE), sorted(self.FIELDS))

    def test_the_exemptions_name_fields_that_exist(self):
        for name in set(API_EXEMPT) | set(CSV_EXEMPT):
            with self.subTest(field=name):
                self.assertIn(name, self.FIELDS)

    def test_every_field_moves_the_fingerprint(self):
        """The whole contract in one assertion: nothing shopper-visible is invisible to
        the diff, so no change can be published once and then forgotten."""
        base = rendered()
        for name in self.FIELDS:
            with self.subTest(field=name):
                moved = dataclasses.replace(base, **{name: OTHER_VALUE[name]})
                self.assertNotEqual(base.fingerprint(), moved.fingerprint())

    def test_every_field_reaches_the_api_input_or_is_exempt(self):
        base = api_input(rendered())
        for name in self.FIELDS:
            if name in API_EXEMPT:
                continue
            with self.subTest(field=name):
                moved = api_input(rendered(**{name: OTHER_VALUE[name]}))
                self.assertNotEqual(base, moved)

    def test_every_field_reaches_the_csv_row_or_is_exempt(self):
        base = primary_row(rendered())
        for name in self.FIELDS:
            if name in CSV_EXEMPT:
                continue
            with self.subTest(field=name):
                moved = primary_row(rendered(**{name: OTHER_VALUE[name]}))
                self.assertNotEqual(base, moved)


class BothSinksAgree(unittest.TestCase):
    def setUp(self):
        self.product = rendered()
        self.api = api_input(self.product)
        self.csv = primary_row(self.product)

    def test_they_publish_the_same_identity_and_price(self):
        self.assertEqual(self.api["handle"], self.csv["Handle"])
        self.assertEqual(self.api["variants"][0]["sku"], self.csv["Variant SKU"])
        self.assertEqual(self.api["variants"][0]["price"], self.csv["Variant Price"])

    def test_they_publish_the_same_copy(self):
        self.assertEqual(self.api["title"], self.csv["Title"])
        self.assertEqual(self.api["descriptionHtml"], self.csv["Body (HTML)"])
        self.assertEqual(self.api["vendor"], self.csv["Vendor"])
        self.assertEqual(self.api["productType"], self.csv["Type"])
        self.assertEqual(self.api["seo"]["title"], self.csv["SEO Title"])
        self.assertEqual(self.api["seo"]["description"], self.csv["SEO Description"])

    def test_they_publish_the_same_tags(self):
        self.assertEqual(set(self.api["tags"]), set(self.csv["Tags"].split(", ")))

    def test_the_quantity_is_the_same_number(self):
        self.assertEqual(str(self.product.inventory), self.csv["Variant Inventory Qty"])

    def test_the_image_rows_carry_what_the_render_resolved(self):
        rows = list(part_to_rows(PART, ["https://example.invalid/a.jpg",
                                        "https://example.invalid/b.jpg"], STORE))
        self.assertEqual([row["Image Src"] for row in rows],
                         ["https://example.invalid/a.jpg", "https://example.invalid/b.jpg"])


class TheQuantityIsTheRenderedOne(unittest.TestCase):
    """The number published and the number fingerprinted have to be the same number.

    They were computed twice from the same part — `render` clamping a negative to zero, and
    the upsert clamping it again beside it. Identical rules, and that is the hazard: one of
    them could have been changed.
    """

    def sent(self, product) -> int:
        from unittest.mock import MagicMock

        from coreyard.sink.shopify_write import ShopifyPublisher

        publisher = ShopifyPublisher.__new__(ShopifyPublisher)
        publisher.client = MagicMock()
        publisher.client.mutate.return_value = {"product": {"id": "gid://x"}}
        publisher.location = "gid://shopify/Location/1"
        publisher.status = "ACTIVE"
        publisher.store = STORE
        publisher._upsert(product, None)
        variables = publisher.client.mutate.call_args.args[1]
        return variables["input"]["variants"][0]["inventoryQuantities"][0]["quantity"]

    def test_the_published_quantity_comes_from_the_canonical_product(self):
        self.assertEqual(self.sent(rendered(inventory=7)), 7)

    def test_a_negative_source_quantity_is_zero_in_both(self):
        product = render(dataclasses.replace(PART, quantity=-3), [], STORE)
        self.assertEqual(product.inventory, 0)
        self.assertEqual(self.sent(product), 0)


class WhatCoreYardDoesNotOwn(unittest.TestCase):
    """Status, channel publication and other systems' tags are preserved, not generated.

    They are deliberately outside the canonical product: folding them in would make the
    fingerprint depend on the store's state rather than on the yard's, so a hand-set status
    would read as a change to publish away every run.
    """

    def test_status_is_not_part_of_the_canonical_product(self):
        self.assertNotIn("status", rendered().payload())

    def test_the_status_sent_is_the_one_the_caller_decided(self):
        self.assertEqual(api_input(rendered())["status"], "ACTIVE")
        self.assertEqual(
            product_set_input(rendered(), "DRAFT", [], (), True, ())["status"], "DRAFT")

    def test_the_two_sinks_agree_about_the_intended_status(self):
        """The only publishing decision the operator is asked to make, and the CSV path
        used to make its own: `Status` said `active` unconditionally, so `--status` — which
        the CLI offers on every sync — changed nothing at all on that path."""
        for status in ("DRAFT", "ACTIVE"):
            with self.subTest(status=status):
                api = product_set_input(rendered(), status, [], (), True, ())
                csv_row = primary_row(rendered(), status)
                self.assertEqual(api["status"], status)
                self.assertEqual(csv_row["Status"], status.lower())

    def test_a_draft_is_not_published_to_the_online_store(self):
        """Shopify's importer reads both columns, and resolves the contradiction by
        publishing — which is the outcome DRAFT exists to prevent."""
        self.assertEqual(primary_row(rendered(), "DRAFT")["Published"], "FALSE")
        self.assertEqual(primary_row(rendered(), "ACTIVE")["Published"], "TRUE")

    def test_the_csv_defaults_to_the_reviewable_state(self):
        """A draft that should have been live is one click away. An unreviewed catalogue
        that went live is already in front of customers."""
        self.assertEqual(primary_row(rendered())["Status"], "draft")

    def test_another_systems_tags_survive_a_publish(self):
        sent = product_set_input(rendered(), "ACTIVE", ["ebay-listed"], ("ebay-",), True, ())
        self.assertIn("ebay-listed", sent["tags"])

    def test_and_are_not_in_the_fingerprint(self):
        """Otherwise every part another system tags would read as changed, for ever."""
        self.assertNotIn("ebay-listed", rendered().payload()["tags"])


class ThePriceIsTheSourcePrice(unittest.TestCase):
    """CoreYard performs no merchandising adjustment, so there is nothing to compound."""

    def test_it_publishes_exactly_what_the_yard_says(self):
        for amount in ("0.01", "45.00", "57.50", "1299.99", "12345.67"):
            with self.subTest(amount=amount):
                part = dataclasses.replace(PART, price=Decimal(amount))
                self.assertEqual(render(part, [], STORE).price, amount)

    def test_two_decimal_places_always(self):
        part = dataclasses.replace(PART, price=Decimal("45"))
        self.assertEqual(render(part, [], STORE).price, "45.00")

    def test_a_third_decimal_rounds_half_to_even_as_documented(self):
        """The precision "equals the source price" is true at. Yard prices are money
        columns, so this is about the edge rather than about the traffic."""
        self.assertEqual(money_str(Decimal("12.345")), "12.34")
        self.assertEqual(money_str(Decimal("12.355")), "12.36")
        self.assertEqual(money_str(Decimal("12.344")), "12.34")

    def test_rendering_twice_cannot_move_the_price(self):
        """The property the criterion asks for in the absence of any adjustment step: a
        second pass over the same part is the same price, not a second markup."""
        once = render(PART, [], STORE).price
        twice = render(PART, [], STORE).price
        self.assertEqual(once, twice)
        self.assertEqual(once, money_str(PART.price))


if __name__ == "__main__":
    unittest.main()
