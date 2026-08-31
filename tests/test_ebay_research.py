"""Comparable selection, price guards, and the portal session jar.

The guards here are deliberately *outside* the model: a price that reaches a live listing
must be defensible from the comparables alone, whatever the inference returned.
"""

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.ebay import auth, market_index, market_research, research


class Material(unittest.TestCase):
    """Steel and alloy wheels are different parts at different prices."""

    def test_material_is_read_from_the_source_title(self):
        self.assertEqual(market_research.material_from_title("17x7 Alloy Wheel"), "alloy")
        self.assertEqual(market_research.material_from_title("17x7 Steel Wheel"), "steel")
        self.assertEqual(market_research.material_from_title("Road Wheel"), "steel")

    def test_an_undeclared_material_stays_unknown_rather_than_defaulting(self):
        self.assertEqual(market_research.material_from_title("17x7 Wheel"), "unknown")

    def test_a_finish_word_implies_alloy_only_as_a_fallback(self):
        self.assertEqual(market_index.expected_material("17x7 Machined Wheel"), "alloy")
        self.assertEqual(market_index.expected_material("17x7 Steel Wheel"), "steel")


COMPS = [
    {"item": "1", "title": "17x7 OEM Alloy Wheel Rim 2011 Ford", "price": 120.0},
    {"item": "2", "title": "17x7 OEM Alloy Wheel Rim 2011 Ford", "price": 140.0},
    {"item": "3", "title": "17x7 Factory Alloy Wheel Rim 2011 Ford", "price": 160.0},
]


class CompSelection(unittest.TestCase):
    def setUp(self):
        self.source = "17x7 Alloy Wheel Rim 2011 Ford"

    def test_a_matching_comparable_is_kept(self):
        self.assertTrue(market_index.select_comps(self.source, COMPS))

    def test_a_different_wheel_size_is_not_a_comparable(self):
        wrong = [{"item": "9", "title": "20x9 OEM Alloy Wheel Rim 2011 Ford",
                  "price": 300.0}]
        self.assertEqual(market_index.select_comps(self.source, wrong), [])

    def test_a_steel_wheel_is_not_a_comparable_for_an_alloy_one(self):
        steel = [{"item": "9", "title": "17x7 OEM Steel Wheel Rim 2011 Ford",
                  "price": 60.0}]
        self.assertEqual(
            market_index.select_comps(self.source, steel, material="alloy"), [])

    def test_something_that_is_not_a_wheel_is_never_a_comparable(self):
        junk = [{"item": "9", "title": "17x7 OEM Alloy Center Cap 2011 Ford",
                 "price": 40.0}]
        self.assertEqual(market_index.select_comps(self.source, junk), [])

    def test_absurd_prices_are_excluded_at_both_ends(self):
        extremes = [{"item": "9", "title": "17x7 OEM Alloy Wheel Rim 2011 Ford",
                     "price": 5.0},
                    {"item": "8", "title": "17x7 OEM Alloy Wheel Rim 2011 Ford",
                     "price": 5000.0}]
        self.assertEqual(market_index.select_comps(self.source, extremes), [])

    def test_the_same_listing_twice_counts_once(self):
        """Duplicated index entries would otherwise inflate the comparable count."""
        selected = market_index.select_comps(self.source, COMPS[:1] * 4)
        self.assertEqual(len(selected), 1)


class Pricing(unittest.TestCase):
    def test_the_price_sits_below_the_middle_of_the_market(self):
        """A salvage part competes on price; the target is the lower third, not the median."""
        prices = [100.0, 120.0, 140.0, 160.0, 180.0]
        price = market_index.selling_price(prices, floor=10.0)
        self.assertLess(price, 140.0)
        self.assertGreater(price, 10.0)

    def test_a_market_below_the_floor_returns_the_floor(self):
        self.assertEqual(market_index.selling_price([20.0, 25.0], floor=200.0), 200.0)

    def test_prices_land_on_a_charm_ending(self):
        price = market_index.selling_price([100.0, 120.0, 140.0, 160.0, 180.0], 10.0)
        self.assertTrue(str(price).endswith(".99"), price)

    def test_confidence_follows_the_evidence_and_bottoms_out_at_none(self):
        self.assertEqual(market_index.confidence(0), "none")
        self.assertEqual(market_index.confidence(1), "low")
        self.assertEqual(market_index.confidence(3), "medium")
        self.assertEqual(market_index.confidence(9), "high")

    def test_percentile_interpolates_and_survives_a_single_value(self):
        self.assertEqual(market_index.percentile([10.0], 0.3), 10.0)
        self.assertEqual(market_index.percentile([10.0, 20.0], 0.5), 15.0)


LISTING = {"listing_id": "1", "conditions": ""}


class PriceGuards(unittest.TestCase):
    """These run after inference, so a bad answer cannot reach a listing unchallenged."""

    def test_a_price_far_above_the_comparables_is_capped(self):
        out = research.apply_guards({"suggested_price": 5000},
                                    LISTING, {"comp_median": 1000, "comp_count": 5})
        self.assertTrue(out["capped"])
        self.assertEqual(Decimal(str(out["suggested_price"])),
                         Decimal("1000") * research.MAX_OVER_MEDIAN)

    def test_a_price_below_the_floor_is_raised_and_flagged(self):
        out = research.apply_guards({"suggested_price": 50},
                                    LISTING, {"comp_median": 60, "comp_count": 5})
        self.assertTrue(out["floored"])
        self.assertEqual(Decimal(str(out["suggested_price"])), research.FLOOR)

    def test_no_price_is_left_at_zero_rather_than_invented(self):
        out = research.apply_guards({"suggested_price": 0}, LISTING, {"comp_count": 0})
        self.assertEqual(out["suggested_price"], 0.0)
        self.assertIn("no price returned", out["review_flags"])

    def test_a_condition_defect_forces_disclosure_whatever_the_model_said(self):
        for note in ("could not test", "needs a head gasket", "block is cracked"):
            out = research.apply_guards(
                {"suggested_price": 900, "needs_disclosure": False},
                {"listing_id": "1", "conditions": note},
                {"comp_median": 900, "comp_count": 5},
            )
            self.assertTrue(out["needs_disclosure"], note)

    def test_the_models_own_disclosure_call_is_respected_on_a_clean_note(self):
        out = research.apply_guards(
            {"suggested_price": 900, "needs_disclosure": True},
            {"listing_id": "1", "conditions": "runs well"},
            {"comp_median": 900, "comp_count": 5},
        )
        self.assertTrue(out["needs_disclosure"])

    def test_a_clean_listing_is_not_held_back(self):
        out = research.apply_guards({"suggested_price": 900}, LISTING,
                                    {"comp_median": 900, "comp_count": 5})
        self.assertFalse(out.get("needs_disclosure"))
        self.assertEqual(out["review_flags"], [])

    def test_missing_comparables_are_flagged_not_hidden(self):
        out = research.apply_guards({"suggested_price": 900}, LISTING, {"comp_count": 0})
        self.assertIn("no comparables found", out["review_flags"])

    def test_the_unguarded_answer_is_kept_for_review(self):
        out = research.apply_guards({"suggested_price": 5000},
                                    LISTING, {"comp_median": 1000, "comp_count": 5})
        self.assertEqual(out["raw_suggested"], "5000")


class Resume(unittest.TestCase):
    def test_prepare_skips_groups_that_are_already_researched(self):
        rows = [{"listing_id": "1", "interchange_number": "560-1", "title": "17x7 Wheel"},
                {"listing_id": "2", "interchange_number": "560-2", "title": "18x8 Wheel"}]
        with TemporaryDirectory() as tmp:
            pending = market_index.prepare(
                rows, [], [{"id": "560-1"}], Path(tmp) / "pages",
                Path(tmp) / "fetch.conf",
            )
        self.assertEqual([key for key, _ in pending], ["560-2"])

    def test_prepare_writes_one_fetch_target_per_pending_group(self):
        rows = [{"listing_id": "1", "interchange_number": "560-1", "title": "17x7 Wheel"}]
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "fetch.conf"
            market_index.prepare(rows, [], [], Path(tmp) / "pages", config)
            text = config.read_text(encoding="utf-8")
        self.assertEqual(text.count("url = "), 1)
        self.assertEqual(text.count("output = "), 1)

    def test_a_truncated_cached_page_is_treated_as_absent(self):
        """A short page is an error page; parsing it would price against nothing."""
        with TemporaryDirectory() as tmp:
            page = Path(tmp) / "560-1.html"
            page.write_text("<html>rate limited</html>", encoding="utf-8")
            self.assertEqual(market_index.parse_page(page), [])
            self.assertEqual(market_index.parse_page(Path(tmp) / "absent.html"), [])


class SessionJar(unittest.TestCase):
    def test_a_cookie_header_parses_into_a_jar(self):
        jar = auth.parse_cookie_header("session=abc; account=def")
        self.assertEqual(jar, {"session": "abc", "account": "def"})

    def test_surrounding_whitespace_and_empty_parts_are_tolerated(self):
        self.assertEqual(auth.parse_cookie_header(" a=1 ;; b=2 ; "),
                         {"a": "1", "b": "2"})

    def test_a_value_containing_an_equals_sign_survives_intact(self):
        """Base64 session values end in padding; splitting on every = would corrupt them."""
        self.assertEqual(auth.parse_cookie_header("t=abc==")["t"], "abc==")

    def test_nothing_parseable_yields_an_empty_jar_not_a_junk_entry(self):
        self.assertEqual(auth.parse_cookie_header(""), {})
        self.assertEqual(auth.parse_cookie_header("garbage"), {})

    def test_a_missing_cache_reads_as_no_session_rather_than_raising(self):
        self.assertIsNone(auth.cached_cookies(Path("/nonexistent/portal.cookies")))


if __name__ == "__main__":
    unittest.main()
