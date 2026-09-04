"""The unattended portal pass uses the source as its only price authority."""

import unittest
from decimal import Decimal

from coreyard.config import StoreProfile
from coreyard.ebay import daily
from coreyard.models import Part

STORE = StoreProfile()


def part(
    r_number="51",
    stock="1001",
    code=166,
    part_type="TAIL LAMP",
    price="80.00",
    **kwargs,
):
    amount = Decimal(price) if price is not None else None
    return Part(
        r_number=r_number,
        part_type=part_type,
        part_type_code=code,
        stock_number=stock,
        price=amount,
        quantity=1,
        year=2014,
        make="Ford",
        model="Fusion",
        **kwargs,
    )


def row(
    listing_id="L1",
    stock="1001",
    part_type="166",
    title="Tail Lamp 51",
    price="$75.00",
    interchange="166-01234",
):
    return {
        "listing_id": listing_id,
        "stock_number": stock,
        "part_type": part_type,
        "title": title,
        "price": price,
        "interchange_number": interchange,
    }


class Planning(unittest.TestCase):
    def test_a_resolved_listing_is_titled_by_the_renderer(self):
        result = daily.plan([row()], [part()], STORE)
        self.assertEqual(result.resolved, 1)
        self.assertEqual(result.title_writes, 1)
        self.assertIn("Fusion", result.titles[0]["new_title"])

    def test_a_listing_that_resolves_to_no_part_is_held_with_a_reason(self):
        result = daily.plan([row(stock="9999")], [part()], STORE)
        self.assertEqual(result.resolved, 0)
        self.assertEqual(result.titles, [])
        self.assertEqual(result.prices, [])
        self.assertEqual(result.held[0]["stage"], "link")
        self.assertIn("no yard part", result.held[0]["reason"])

    def test_an_ambiguous_listing_is_held_rather_than_changed_as_a_guess(self):
        result = daily.plan([row()], [part("51"), part("52")], STORE)
        self.assertEqual(result.titles, [])
        self.assertEqual(result.prices, [])
        self.assertTrue(any("share this donor" in item["reason"] for item in result.held))

    def test_portal_price_is_replaced_with_exact_source_price(self):
        result = daily.plan([row(price="$64.99")], [part(price="80.00")], STORE)
        self.assertEqual(result.prices[0]["new_price"], "80.00")
        self.assertEqual(result.prices[0]["listing_id"], "L1")

    def test_matching_price_needs_no_write(self):
        result = daily.plan([row(price="$80.00")], [part(price="80.00")], STORE)
        self.assertEqual(result.prices, [])

    def test_source_price_is_not_adjusted(self):
        result = daily.plan([row(price="$60.00")], [part(price="67.50")], STORE)
        self.assertEqual(result.prices[0]["new_price"], "67.50")

    def test_missing_source_price_is_held(self):
        ready, held = daily.source_price_decisions(
            [row()], {"L1": part(price=None)}
        )
        self.assertEqual(ready, [])
        self.assertIn("source system", held[0]["reason"])

    def test_the_summary_names_source_price_changes(self):
        result = daily.plan([row()], [part()], STORE)
        self.assertIn("1 listings", result.summary())
        self.assertIn("1 source-price change(s) ready", result.summary())


class ShopifyOverrides(unittest.TestCase):
    def test_only_reviewed_titles_reach_the_renderer(self):
        result = daily.plan([row()], [part()], STORE)
        overrides = daily.overrides_for(result, {"L1": part()})
        self.assertEqual(set(overrides["parts"]), {"51"})
        self.assertEqual(set(overrides["parts"]["51"]), {"title"})
        self.assertIn("Fusion", overrides["parts"]["51"]["title"])

    def test_merge_keeps_titles_and_removes_historical_prices(self):
        result = daily.plan([row()], [part()], STORE)
        prior = {
            "version": 1,
            "parts": {
                "900": {"title": "reviewed last week", "price": "125.00"},
                "901": {"price": "99.99"},
            },
        }
        merged = daily.overrides_for(result, {"L1": part()}, prior)
        self.assertEqual(merged["parts"]["900"], {"title": "reviewed last week"})
        self.assertNotIn("901", merged["parts"])
        self.assertIn("51", merged["parts"])


if __name__ == "__main__":
    unittest.main()
