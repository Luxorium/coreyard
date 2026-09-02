"""The guards over a proposed price, and the portal session jar.

Wheel-specific comparable selection and pricing lived here until one generic engine
replaced the per-part-type modules; those behaviours are now
:mod:`tests.test_ebay_comps_generic` and :mod:`tests.test_ebay_index`.
"""

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.ebay import auth, research


LISTING = {"listing_id": "1", "conditions": ""}


class PriceGuards(unittest.TestCase):
    """These run outside the calculation that proposes a price, so a bad answer cannot
    reach a listing unchallenged whatever produced it."""

    def test_a_price_far_above_the_comparables_is_capped(self):
        out = research.apply_guards({"suggested_price": 5000},
                                    LISTING, {"comp_median": 1000, "comp_count": 5})
        self.assertTrue(out["capped"])
        # Capped to the ceiling, then landed on the price grid — so at the ceiling or
        # within one step under it, never above.
        ceiling = Decimal("1000") * research.MAX_OVER_MEDIAN
        price = Decimal(str(out["suggested_price"]))
        self.assertLessEqual(price, ceiling)
        self.assertGreater(price, ceiling - research.PRICE_STEP)

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


class EveryPriceIsAShopperFacingNumber(unittest.TestCase):
    """Multiples of five, a penny under. $64.99, never $67.49."""

    def _grid(self, value) -> bool:
        cents = (Decimal(str(value)) + Decimal("0.01")) % research.PRICE_STEP
        return cents == 0 and str(value).endswith(".99")

    def test_a_comparable_derived_price_lands_on_the_grid(self):
        out = research.apply_guards({"suggested_price": 267.43}, LISTING,
                                    {"comp_median": 300, "comp_count": 8})
        self.assertTrue(self._grid(out["suggested_price"]), out["suggested_price"])

    def test_a_floored_price_lands_on_the_grid_too(self):
        # The floor is a business minimum, not a shopper-facing number.
        out = research.apply_guards({"suggested_price": 4}, LISTING,
                                    {"comp_median": 5, "comp_count": 3, "floor": "12.50"})
        self.assertTrue(out["floored"])
        self.assertTrue(self._grid(out["suggested_price"]), out["suggested_price"])

    def test_landing_on_the_grid_never_drops_below_the_floor(self):
        out = research.apply_guards({"suggested_price": 4}, LISTING,
                                    {"comp_median": 5, "comp_count": 3, "floor": "11.00"})
        self.assertGreaterEqual(Decimal(str(out["suggested_price"])), Decimal("11.00"))
