"""The condition-aware price fallback, and which listings a write is aimed at.

Scoring and query construction moved to :mod:`tests.test_ebay_comps_generic` when one
generic engine replaced the per-part-type modules. What is left here is the pricing of a
single physical unit, and ID resolution — the one mistake that cannot be undone by
re-running anything.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.ebay import cli, research


class DeterministicFallback(unittest.TestCase):
    """When inference is rate-limited, the fallback must stay explainable and cautious."""

    GROUP = {"comp_median": 2000, "domestic_median": 2000, "comp_count": 6,
             "comps": [{"price": 1900, "title": "2011 Ford 5.0L Engine"}]}

    def test_no_median_means_no_price_rather_than_a_guess(self):
        record = research.price_listing({"listing_id": "1"}, {"comp_count": 0})
        self.assertEqual(record["suggested_price"], 0.0)
        self.assertEqual(record["confidence"], "none")

    def test_a_price_is_derived_below_the_market_median(self):
        record = research.price_listing(
            {"listing_id": "1", "miles": 120000}, self.GROUP)
        self.assertLess(record["suggested_price"], 2000)
        self.assertGreater(record["suggested_price"], 0)

    def test_high_mileage_prices_lower_than_low_mileage(self):
        low = research.price_listing({"listing_id": "1", "miles": 40000},
                                            self.GROUP)["suggested_price"]
        high = research.price_listing({"listing_id": "1", "miles": 250000},
                                             self.GROUP)["suggested_price"]
        self.assertGreater(low, high)

    def test_unknown_mileage_is_discounted_not_treated_as_average(self):
        unknown = research.price_listing({"listing_id": "1"},
                                                self.GROUP)["suggested_price"]
        known = research.price_listing({"listing_id": "1", "miles": 90000},
                                              self.GROUP)["suggested_price"]
        self.assertLess(unknown, known)

    def test_an_untested_engine_is_discounted_and_flagged(self):
        record = research.price_listing(
            {"listing_id": "1", "miles": 90000, "conditions": "could not test"},
            self.GROUP)
        clean = research.price_listing({"listing_id": "1", "miles": 90000},
                                              self.GROUP)
        self.assertLess(record["suggested_price"], clean["suggested_price"])
        self.assertIn("untested", record["reasoning"])

    def test_the_reasoning_names_every_adjustment_it_made(self):
        record = research.price_listing(
            {"listing_id": "1", "miles": 40000, "grade": "A", "days_in_inventory": 800},
            self.GROUP)
        self.assertIn("under 60k miles", record["reasoning"])
        self.assertIn("grade A", record["reasoning"])
        self.assertIn("two years", record["reasoning"])

    def test_the_fallback_is_still_subject_to_the_price_guards(self):
        record = research.price_listing({"listing_id": "1", "miles": 90000},
                                               {"comp_median": 10, "comp_count": 1,
                                                "domestic_median": 10, "comps": []})
        self.assertTrue(record["floored"])

    def test_the_discount_is_bounded_so_a_stack_of_penalties_cannot_zero_a_part(self):
        record = research.price_listing(
            {"listing_id": "1", "miles": 250000, "grade": "C",
             "days_in_inventory": 2000, "conditions": "could not test, block is cracked"},
            self.GROUP)
        self.assertGreaterEqual(record["suggested_price"], 0.40 * 2000 - 1)


PAGE = """
<html><body>%s</body></html>
""" + "<!-- pad -->" * 1200


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
