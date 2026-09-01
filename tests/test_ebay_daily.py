"""The unattended pass over the listing portal.

This is the only part of the channel that runs with nobody watching, so the properties
worth pinning are the refusals: a listing it cannot identify is left alone, a price with
too little evidence is held rather than written, and no path through it reaches eBay.
"""

import unittest
from decimal import Decimal
from pathlib import Path

from coreyard.config import StoreProfile
from coreyard.ebay import daily
from coreyard.models import Part

STORE = StoreProfile()


def part(r_number="51", stock="1001", code=166, part_type="TAIL LAMP", price="80.00",
         **kwargs):
    return Part(r_number=r_number, part_type=part_type, part_type_code=code,
                stock_number=stock, price=Decimal(price), quantity=1,
                year=2014, make="Ford", model="Fusion", **kwargs)


def row(listing_id="L1", stock="1001", part_type="166", title="Tail Lamp 51",
        price="$80.00", interchange="166-01234"):
    return {"listing_id": listing_id, "stock_number": stock, "part_type": part_type,
            "title": title, "price": price, "interchange_number": interchange}


def research(r_number="51", price="64.99", comp_count=6, confidence="high"):
    return {r_number: {"r_number": r_number, "suggested_price": price,
                       "comp_count": comp_count, "confidence": confidence,
                       "comp_median": 70.0, "floored": False}}


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
        self.assertEqual(result.held[0]["stage"], "link")
        self.assertIn("no yard part", result.held[0]["reason"])

    def test_an_ambiguous_listing_is_held_rather_than_titled_as_a_guess(self):
        """Two tail lamps off one donor: retitling one as the other is the bad outcome."""
        result = daily.plan([row()], [part("51"), part("52")], STORE)
        self.assertEqual(result.titles, [])
        self.assertTrue(any("share this donor" in item["reason"]
                            for item in result.held))

    def test_without_research_it_plans_titles_and_no_prices_at_all(self):
        result = daily.plan([row()], [part()], STORE)
        self.assertEqual(result.prices, [])
        self.assertTrue(result.titles)

    def test_a_well_evidenced_price_is_planned(self):
        result = daily.plan([row()], [part()], STORE, research())
        self.assertEqual(len(result.prices), 1)
        self.assertEqual(result.prices[0]["new_price"], "64.99")
        self.assertEqual(result.prices[0]["listing_id"], "L1")

    def test_a_thinly_evidenced_price_is_held_not_written(self):
        result = daily.plan([row()], [part()], STORE, research(comp_count=1),
                            min_comps=3)
        self.assertEqual(result.prices, [])
        held = [item for item in result.held if item["stage"] == "price"]
        self.assertIn("comparables", held[0]["reason"])

    def test_a_price_with_no_confidence_is_held(self):
        result = daily.plan([row()], [part()], STORE,
                            research(confidence="none", price="0"))
        self.assertEqual(result.prices, [])

    def test_a_violent_price_move_is_held_by_the_swing_ceiling(self):
        result = daily.plan([row(price="$20.00")], [part()], STORE,
                            research(price="500.00"))
        self.assertEqual(result.prices, [])
        held = [item for item in result.held if item["stage"] == "price"]
        self.assertIn("safety ceiling", held[0]["reason"])

    def test_research_for_a_part_reaches_every_listing_that_part_is(self):
        rows = [row("L1"), row("L2", title="Tail Lamp 51 again")]
        resolved = {"L1": part(), "L2": part()}
        records = daily.research_by_listing(resolved, research()["51"] and research())
        self.assertEqual(sorted(item["listing_id"] for item in records), ["L1", "L2"])
        self.assertEqual({item["r_number"] for item in records}, {"51"})

    def test_research_for_a_part_nobody_listed_is_simply_absent(self):
        records = daily.research_by_listing({"L1": part("51")}, research("999"))
        self.assertEqual(records, [])

    def test_the_summary_counts_what_a_run_would_write(self):
        result = daily.plan([row()], [part()], STORE, research())
        self.assertIn("1 listings", result.summary())
        self.assertIn("1 title(s) and 1 price(s) ready", result.summary())


class ShopifyOverrides(unittest.TestCase):
    def test_reviewed_values_are_keyed_by_r_number_for_the_renderer(self):
        result = daily.plan([row()], [part()], STORE, research())
        overrides = daily.overrides_for(result, {"L1": part()})
        self.assertEqual(set(overrides["parts"]), {"51"})
        self.assertEqual(overrides["parts"]["51"]["price"], "64.99")
        self.assertIn("Fusion", overrides["parts"]["51"]["title"])


class OverrideMerge(unittest.TestCase):
    """A nightly pass adds to the renderer's override file; it must never shorten it."""

    def test_a_decision_reviewed_earlier_survives_tonights_pass(self):
        result = daily.plan([row()], [part()], STORE, research())
        prior = {"version": 1, "parts": {"900": {"title": "reviewed last week",
                                                 "price": "125.00"}}}
        merged = daily.overrides_for(result, {"L1": part()}, prior)
        self.assertEqual(merged["parts"]["900"]["price"], "125.00")
        self.assertIn("51", merged["parts"])

    def test_tonights_decision_replaces_an_older_one_for_the_same_part(self):
        result = daily.plan([row()], [part()], STORE, research())
        prior = {"version": 1, "parts": {"51": {"price": "999.00"}}}
        merged = daily.overrides_for(result, {"L1": part()}, prior)
        self.assertEqual(merged["parts"]["51"]["price"], "64.99")

    def test_a_field_the_pass_did_not_decide_is_kept_from_the_older_entry(self):
        """A titles-only night must not blank a price reviewed on a previous one."""
        result = daily.plan([row()], [part()], STORE)
        prior = {"version": 1, "parts": {"51": {"price": "125.00"}}}
        merged = daily.overrides_for(result, {"L1": part()}, prior)
        self.assertEqual(merged["parts"]["51"]["price"], "125.00")
        self.assertIn("Fusion", merged["parts"]["51"]["title"])

    def test_with_no_previous_file_it_is_simply_tonights_decisions(self):
        result = daily.plan([row()], [part()], STORE, research())
        merged = daily.overrides_for(result, {"L1": part()})
        self.assertEqual(set(merged["parts"]), {"51"})


class Comparables(unittest.TestCase):
    def setUp(self):
        self.fetched = []

    def _fetcher(self, ok=True):
        def fetch(query, destination):
            self.fetched.append(query)
            return ok
        return fetch

    def test_a_part_whose_page_cannot_be_fetched_is_recorded_with_no_price(self):
        out = daily.research_parts([part()], STORE, Path("/nonexistent"),
                                   self._fetcher(ok=False), retry_thin=False)
        self.assertEqual(out["51"]["comp_count"], 0)
        self.assertEqual(out["51"]["confidence"], "none")

    def test_a_part_already_researched_is_not_fetched_again(self):
        done = {"51": {"r_number": "51", "comp_count": 4}}
        out = daily.research_parts([part()], STORE, Path("/nonexistent"),
                                   self._fetcher(), done=done)
        self.assertEqual(self.fetched, [])
        self.assertEqual(out["51"]["comp_count"], 4)

    def test_a_thin_result_asks_the_broader_query_before_giving_up(self):
        engine = part(part_type="ENGINE ASSEMBLY", code=300, price="900.00",
                      description="2.5L VIN 7 8th digit")
        daily.research_parts([engine], STORE, Path("/nonexistent"),
                             self._fetcher(ok=False), retry_thin=True)
        self.assertEqual(self.fetched,
                         ["2014 Ford Fusion Engine Motor Assembly 2.5L VIN 7",
                          "2014 Ford Fusion Engine Motor Assembly"])

    def test_a_part_with_no_distinguishing_spec_is_not_asked_the_same_query_twice(self):
        """The broad query drops the spec; with no spec to drop it is the same request."""
        daily.research_parts([part()], STORE, Path("/nonexistent"),
                             self._fetcher(ok=False), retry_thin=True)
        self.assertEqual(len(self.fetched), 1)

    def test_checkpoints_are_written_so_a_killed_run_resumes(self):
        banked = []
        daily.research_parts([part("51"), part("52", stock="1002")], STORE,
                             Path("/nonexistent"), self._fetcher(ok=False),
                             retry_thin=False, checkpoint=banked.append, every=1)
        self.assertEqual(len(banked), 2)
        self.assertEqual(set(banked[-1]), {"51", "52"})


if __name__ == "__main__":
    unittest.main()
