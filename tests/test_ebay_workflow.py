"""Guards on the writes that reach a live marketplace, and the item specifics they carry.

Every test here is offline: the portal client is a recorder, so what is asserted is the
*decision* — which listings a write covers, what it sends, and when it refuses — never a
round trip.
"""

import json
import unittest

from coreyard.ebay import aspects, workflow


class Recorder:
    """A portal client that records bulk updates instead of performing them."""

    def __init__(self, error: str = ""):
        self.error = error
        self.calls = []

    def bulk_update(self, changes, listing_ids, *, tab="unlisted", **kwargs):
        self.calls.append({"changes": changes, "listing_ids": list(listing_ids),
                           "tab": tab, **kwargs})
        return self.error


TITLE_PLAN = [
    {"interchange": "300-A", "listing_ids": ["1", "2"], "new_title": "A Better Title"},
    {"interchange": "300-B", "listing_ids": ["3"], "new_title": "Another Title"},
]


class Caps(unittest.TestCase):
    """The cap counts *listings*, not plan entries, and only binds a real write."""

    def test_dry_run_is_never_capped(self):
        results = workflow.apply_titles(Recorder(), TITLE_PLAN, dry_run=True, cap=1)
        self.assertEqual([item["status"] for item in results], ["DRY-RUN", "DRY-RUN"])

    def test_cap_counts_listings_not_plan_entries(self):
        client = Recorder()
        with self.assertRaises(workflow.ApplyGuard):
            workflow.apply_titles(client, TITLE_PLAN, dry_run=False, cap=2)
        self.assertEqual(client.calls, [], "refused write must send nothing")

    def test_guard_is_raised_before_the_first_write(self):
        """A partial write is worse than none: the first entry fits the cap on its own."""
        client = Recorder()
        with self.assertRaises(workflow.ApplyGuard):
            workflow.apply_prices(
                client,
                [{"listing_ids": ["1", "2"], "new_price": "10.00"},
                 {"listing_ids": ["3"], "new_price": "20.00"}],
                dry_run=False, cap=2,
            )
        self.assertEqual(client.calls, [])

    def test_force_lifts_the_cap(self):
        client = Recorder()
        workflow.apply_titles(client, TITLE_PLAN, dry_run=False, cap=1, force=True)
        self.assertEqual(len(client.calls), 2)

    def test_cap_message_names_the_flag_that_lifts_it(self):
        with self.assertRaises(workflow.ApplyGuard) as caught:
            workflow.check_cap(30, 25, False)
        self.assertIn("--yes-i-mean-it", str(caught.exception))


class Writes(unittest.TestCase):
    def test_titles_write_one_bulk_change_per_interchange_group(self):
        client = Recorder()
        workflow.apply_titles(client, TITLE_PLAN, tab="listed", dry_run=False, cap=25)
        self.assertEqual([call["listing_ids"] for call in client.calls],
                         [["1", "2"], ["3"]])
        self.assertEqual(client.calls[0]["changes"],
                         [{"field": "title", "action": "change_to",
                           "value": "A Better Title"}])
        self.assertEqual(client.calls[0]["tab"], "listed")

    def test_prices_are_bucketed_so_equal_prices_cost_one_write(self):
        client = Recorder()
        priced = [{"listing_id": "1", "new_price": "249.99"},
                  {"listing_id": "2", "new_price": "249.99"},
                  {"listing_id": "3", "new_price": "99.99"}]
        results = workflow.apply_prices(client, priced, dry_run=False, cap=25)
        self.assertEqual(len(client.calls), 2, "one write per distinct price")
        self.assertEqual([item["price"] for item in results], ["99.99", "249.99"],
                         "buckets are ordered by price, not by hash order")
        self.assertEqual(sorted(client.calls[1]["listing_ids"]), ["1", "2"])

    def test_starting_price_is_only_touched_when_asked(self):
        client = Recorder()
        priced = [{"listing_id": "1", "new_price": "249.99"}]
        workflow.apply_prices(client, priced, dry_run=False, cap=25)
        self.assertEqual([change["field"] for change in client.calls[0]["changes"]],
                         ["fixed_price"])
        client = Recorder()
        workflow.apply_prices(client, priced, dry_run=False, cap=25,
                              also_starting_price=True)
        self.assertEqual([change["field"] for change in client.calls[0]["changes"]],
                         ["fixed_price", "starting_price"])

    def test_a_portal_error_is_reported_per_group_not_raised(self):
        """One rejected group must not abandon the groups after it."""
        results = workflow.apply_titles(Recorder("field is locked"), TITLE_PLAN,
                                        dry_run=False, cap=25)
        self.assertEqual([item["status"] for item in results],
                         ["ERROR: field is locked", "ERROR: field is locked"])

    def test_reset_titles_sends_no_field_changes(self):
        client = Recorder()
        workflow.reset_titles(client, ["1"], tab="unlisted")
        self.assertEqual(client.calls[0]["changes"], [])
        self.assertTrue(client.calls[0]["reset_titles"])

    def test_reset_prices_resets_both_price_fields_to_retail(self):
        client = Recorder()
        workflow.reset_prices(client, ["1"], tab="unlisted")
        self.assertEqual(
            [(change["field"], change["action"]) for change in client.calls[0]["changes"]],
            [("fixed_price", "reset_to_retail"), ("starting_price", "reset_to_retail")],
        )


ROW = {"listing_id": "1", "interchange_number": "300-A",
       "title": "2011-2014 FORD F150 5.0L V8 VIN F Engine"}


class Aspects(unittest.TestCase):
    def test_identical_patches_share_one_write(self):
        rows = [ROW, {**ROW, "listing_id": "2"}, {**ROW, "listing_id": "3"}]
        plan = workflow.aspect_plan(rows)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["n"], 3)
        self.assertEqual(plan[0]["listing_ids"], ["1", "2", "3"])

    def test_plan_is_ordered_widest_first(self):
        rows = [ROW, {**ROW, "listing_id": "2"},
                {"listing_id": "3", "interchange_number": "300-B",
                 "title": "2005-2010 HONDA CIVIC 1.8L Engine"}]
        plan = workflow.aspect_plan(rows)
        self.assertEqual([item["n"] for item in plan], [2, 1])

    def test_patch_keys_are_underscored_for_the_portal(self):
        patch = aspects.to_patch({"Engine Size": "5 L", "Brand": "Ford"})
        self.assertEqual(patch, {"Engine_Size": "5 L", "Brand": "Ford"})

    def test_warranty_is_added_only_when_the_site_supplies_one(self):
        self.assertNotIn("Warranty", aspects.to_patch({"Brand": "Ford"}))
        self.assertEqual(aspects.to_patch({"Brand": "Ford"}, warranty="30 Day"),
                         {"Brand": "Ford", "Warranty": "30 Day"})

    def test_aspects_are_sent_as_item_specifics_not_field_changes(self):
        client = Recorder()
        workflow.apply_aspects(client, [ROW], dry_run=False, cap=25)
        self.assertEqual(client.calls[0]["changes"], [])
        self.assertIn("Brand", client.calls[0]["item_specifics"])


class Derivation(unittest.TestCase):
    """A wrong item specific ranks worse than a missing one, so silence is the default."""

    def test_brand_and_size_come_from_the_title(self):
        derived = aspects.derive(ROW)
        self.assertEqual(derived["Brand"], "Ford")
        self.assertEqual(derived["Engine Size"], "5 L")
        self.assertEqual(derived["Number of Cylinders"], "8")

    def test_interchange_is_carried_through_as_the_customer_facing_number(self):
        self.assertEqual(aspects.derive(ROW)["Interchange Part Number"], "300-A")

    def test_donor_make_is_preferred_over_a_title_guess(self):
        row = {"listing_id": "1", "title": "2011 5.0L V8 Engine"}
        self.assertNotIn("Brand", aspects.derive(row))
        self.assertEqual(aspects.derive(row, {"make": "CHEVY"})["Brand"], "Chevrolet")

    def test_mileage_is_banded_never_reported_exactly(self):
        self.assertEqual(aspects.mileage_band(84000), "75,000-100,000 miles")
        self.assertEqual(aspects.mileage_band(120000), "More Than 100,000 miles")
        self.assertIsNone(aspects.mileage_band(0), "no odometer is not zero miles")
        self.assertIsNone(aspects.mileage_band("unknown"))

    def test_unparseable_input_yields_no_guess(self):
        self.assertIsNone(aspects.engine_size("no displacement here"))
        self.assertIsNone(aspects.fuel_type("2011 Ford Engine"))

    def test_selection_only_aspect_rejects_a_value_ebay_does_not_list(self):
        metadata = {"Fuel Type": {"mode": "SELECTION_ONLY",
                                  "values": ["Gasoline", "Diesel"]}}
        self.assertEqual(aspects.canonical("Fuel Type", "gasoline", metadata),
                         "Gasoline", "a listed value is corrected to eBay's own casing")
        self.assertIsNone(aspects.canonical("Fuel Type", "Coal", metadata))

    def test_free_text_aspect_keeps_an_unlisted_value(self):
        metadata = {"Brand": {"mode": "FREE_TEXT", "values": ["Ford"]}}
        self.assertEqual(aspects.canonical("Brand", "Sterling", metadata), "Sterling")

    def test_missing_metadata_refuses_to_apply_but_still_derives(self):
        self.assertEqual(aspects.vocab("/nonexistent/aspects.json"), {})
        with self.assertRaises(aspects.AspectError):
            aspects.vocab("/nonexistent/aspects.json", required=True)

    def test_coverage_counts_each_aspect_across_the_slice(self):
        rows = [ROW, {"listing_id": "2", "title": "no facts at all"}]
        tally = aspects.coverage(rows)
        self.assertEqual(tally["Brand"], 1)
        self.assertEqual(tally["Type"], 2, "constant aspects cover every row")


class PatchIdentity(unittest.TestCase):
    def test_bucket_key_is_stable_across_derivation_order(self):
        """Buckets are keyed by sorted JSON, so key order never splits one write in two."""
        first = json.dumps(aspects.to_patch({"Brand": "Ford", "Type": "Complete"}),
                           sort_keys=True)
        second = json.dumps(aspects.to_patch({"Type": "Complete", "Brand": "Ford"}),
                            sort_keys=True)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
