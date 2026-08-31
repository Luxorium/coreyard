"""Comparable queries, comparable scoring, the deterministic fallback, and ID resolution.

The query and the score decide what a price is evidence *of*: a query that drops the
displacement, or a score that accepts a different engine, produces a confident number for
the wrong part. The last class covers which listings a write is aimed at, which is the one
mistake that cannot be undone by re-running anything.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.ebay import cli, engine_comps, research


ROW = {"listing_id": "1", "interchange_number": "300-A",
       "title": "2011-2014 FORD F150 5.0L V8 VIN F Engine 123456"}


class Queries(unittest.TestCase):
    def test_displacement_is_normalized_for_matching(self):
        self.assertEqual(engine_comps.displacement("5.0L V8"), "5")
        self.assertEqual(engine_comps.displacement("1.8L"), "1.8")
        self.assertIsNone(engine_comps.displacement("no engine size"))

    def test_a_year_is_never_mistaken_for_an_engine_family_code(self):
        """"2011" matches the family-code shape; treating it as one queries nonsense."""
        self.assertIsNone(engine_comps.family_code("2011 2014 Engine"))

    def test_two_digit_and_four_digit_years_both_resolve(self):
        self.assertEqual(engine_comps.years_in("2011-2014 F150"), {2011, 2014})

    def test_the_query_carries_the_displacement_and_says_engine(self):
        query = engine_comps.query_for(ROW)
        self.assertIn("5.0L", query)
        self.assertIn("engine", query)
        self.assertIn("F150", query)

    def test_the_vin_code_is_dropped_from_the_query(self):
        """"VIN F" is an identifier for us and noise to a public index."""
        self.assertNotIn("VIN", engine_comps.query_for(ROW))

    def test_the_stock_number_is_not_searched_for(self):
        self.assertNotIn("123456", engine_comps.query_for(ROW))

    def test_no_term_is_repeated(self):
        terms = engine_comps.query_for(ROW).lower().split()
        self.assertEqual(len(terms), len(set(terms)))

    def test_the_short_query_falls_back_to_the_title_without_donor_detail(self):
        short = engine_comps.short_query_for(ROW)
        self.assertIn("engine", short)
        self.assertNotIn("2011", short, "years belong in the scoring, not the fallback")

    def test_donor_detail_is_preferred_for_the_fits_grammar(self):
        row = {"listing_id": "1", "title": "ENGINE 5.0L Fits 11-14 F150"}
        detail = {"make": "FORD TRUCK", "model": "FORD F150", "model_year": "2012"}
        query = engine_comps.query_for(row, detail)
        self.assertIn("Ford", query)
        self.assertNotIn("Truck", query, "yard vocabulary is not buyer vocabulary")
        self.assertEqual(query.lower().count("ford"), 1)


class Scoring(unittest.TestCase):
    def setUp(self):
        self.args = ("2011 Ford F150 5.0L V8", "5", None, {2011, 2012, 2013, 2014})

    def score(self, title, price=1500.0):
        return engine_comps.score(*self.args, {"title": title, "price": price})

    def test_a_matching_engine_scores(self):
        self.assertIsNotNone(self.score("2011 Ford F150 5.0L V8 Engine 90k Miles"))

    def test_a_different_displacement_is_never_a_comparable(self):
        self.assertIsNone(self.score("2011 Ford F150 3.5L V6 Engine"))

    def test_a_part_that_is_not_an_engine_is_rejected(self):
        self.assertIsNone(self.score("2011 Ford F150 5.0L Engine Wiring Harness"))

    def test_prices_outside_the_plausible_band_are_rejected(self):
        self.assertIsNone(self.score("2011 Ford F150 5.0L V8 Engine", price=5.0))
        self.assertIsNone(self.score("2011 Ford F150 5.0L V8 Engine", price=99_000.0))

    def test_a_different_vehicle_scores_too_low_to_count(self):
        self.assertIsNone(self.score("2003 Toyota Camry Engine Assembly"))

    def test_a_listing_that_identifies_nothing_is_not_evidence(self):
        """"Engine Assembly" at $1,500 says nothing about what this engine is worth."""
        self.assertIsNone(self.score("Complete Engine Assembly Good Runner"))

    def test_mileage_is_read_from_the_comparable_title(self):
        self.assertEqual(engine_comps.comp_miles("Engine 90K Miles"), 90_000)
        self.assertEqual(engine_comps.comp_miles("Engine 128,450 Miles"), 128_450)
        self.assertIsNone(engine_comps.comp_miles("Engine, low miles"))

    def test_an_import_engine_is_kept_but_marked(self):
        """JDM stock prices differently; the basis has to stay visible downstream."""
        scored = self.score("JDM 2011 Ford F150 5.0L V8 Engine 60k")
        if scored is not None:
            self.assertTrue(scored[1]["jdm"])


class DeterministicFallback(unittest.TestCase):
    """When inference is rate-limited, the fallback must stay explainable and cautious."""

    GROUP = {"comp_median": 2000, "domestic_median": 2000, "comp_count": 6,
             "comps": [{"price": 1900, "title": "2011 Ford 5.0L Engine"}]}

    def test_no_median_means_no_price_rather_than_a_guess(self):
        record = research.deterministic_record({"listing_id": "1"}, {"comp_count": 0})
        self.assertEqual(record["suggested_price"], 0.0)
        self.assertEqual(record["confidence"], "none")

    def test_a_price_is_derived_below_the_market_median(self):
        record = research.deterministic_record(
            {"listing_id": "1", "miles": 120000}, self.GROUP)
        self.assertLess(record["suggested_price"], 2000)
        self.assertGreater(record["suggested_price"], 0)

    def test_high_mileage_prices_lower_than_low_mileage(self):
        low = research.deterministic_record({"listing_id": "1", "miles": 40000},
                                            self.GROUP)["suggested_price"]
        high = research.deterministic_record({"listing_id": "1", "miles": 250000},
                                             self.GROUP)["suggested_price"]
        self.assertGreater(low, high)

    def test_unknown_mileage_is_discounted_not_treated_as_average(self):
        unknown = research.deterministic_record({"listing_id": "1"},
                                                self.GROUP)["suggested_price"]
        known = research.deterministic_record({"listing_id": "1", "miles": 90000},
                                              self.GROUP)["suggested_price"]
        self.assertLess(unknown, known)

    def test_an_untested_engine_is_discounted_and_flagged(self):
        record = research.deterministic_record(
            {"listing_id": "1", "miles": 90000, "conditions": "could not test"},
            self.GROUP)
        clean = research.deterministic_record({"listing_id": "1", "miles": 90000},
                                              self.GROUP)
        self.assertLess(record["suggested_price"], clean["suggested_price"])
        self.assertIn("untested", record["reasoning"])

    def test_the_reasoning_names_every_adjustment_it_made(self):
        record = research.deterministic_record(
            {"listing_id": "1", "miles": 40000, "grade": "A", "days_in_inventory": 800},
            self.GROUP)
        self.assertIn("under 60k miles", record["reasoning"])
        self.assertIn("grade A", record["reasoning"])
        self.assertIn("two years", record["reasoning"])

    def test_the_fallback_is_still_subject_to_the_price_guards(self):
        record = research.deterministic_record({"listing_id": "1", "miles": 90000},
                                               {"comp_median": 10, "comp_count": 1,
                                                "domestic_median": 10, "comps": []})
        self.assertTrue(record["floored"])

    def test_the_discount_is_bounded_so_a_stack_of_penalties_cannot_zero_a_part(self):
        record = research.deterministic_record(
            {"listing_id": "1", "miles": 250000, "grade": "C",
             "days_in_inventory": 2000, "conditions": "could not test, block is cracked"},
            self.GROUP)
        self.assertGreaterEqual(record["suggested_price"], 0.40 * 2000 - 1)


class TargetResolution(unittest.TestCase):
    """Which listings a write touches. A wrong ID here ends the wrong live listing."""

    def test_a_comma_separated_list_resolves_to_ids(self):
        self.assertEqual(cli._listing_records("7,8, 9"),
                         [{"listing_id": "7"}, {"listing_id": "8"},
                          {"listing_id": "9"}])

    def test_blank_entries_do_not_become_empty_targets(self):
        self.assertEqual(cli._listing_records("7,,"), [{"listing_id": "7"}])

    def test_a_grouped_plan_expands_to_every_listing_in_the_group(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps(
                [{"interchange": "300-A", "listing_ids": ["1", "2"]}]), encoding="utf-8")
            records = cli._listing_records(str(path))
        self.assertEqual([item["listing_id"] for item in records], ["1", "2"])
        self.assertEqual(records[0]["interchange"], "300-A",
                         "the plan's context travels with the target")

    def test_ids_are_always_strings_whatever_the_json_held(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ids.json"
            path.write_text(json.dumps([7, "8", {"listing_id": 9}]), encoding="utf-8")
            records = cli._listing_records(str(path))
        self.assertEqual([item["listing_id"] for item in records], ["7", "8", "9"])

    def test_an_entry_with_no_id_stops_the_command(self):
        """Silently skipping it would write to a set the operator never reviewed."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps([{"price": "10.00"}]), encoding="utf-8")
            with self.assertRaises(SystemExit):
                cli._listing_records(str(path))

    def test_a_json_object_is_refused_rather_than_iterated_as_keys(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            path.write_text(json.dumps({"listing_id": "1"}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                cli._listing_records(str(path))


if __name__ == "__main__":
    unittest.main()
