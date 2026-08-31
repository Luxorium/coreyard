"""The deterministic engine title builder, the guarded AI title proposals, and grouping.

Titles are the one thing this channel writes that a shopper reads, and the portal escapes
them before eBay sees them — so length is measured *escaped*, and a title that loses a fact
the source title carried is rejected rather than published.
"""

import unittest

from coreyard.ebay import catalog_titles, engine_titles, util


SOURCE = "2011-2014 FORD F150 5.0L V8 VIN F Engine 123456"


class Length(unittest.TestCase):
    """The portal escapes before eBay counts, so & and " cost more than one character."""

    def test_plain_text_costs_its_own_length(self):
        self.assertEqual(util.title_length("Ford Engine"), 11)

    def test_ampersand_and_quote_are_charged_at_their_escaped_width(self):
        self.assertEqual(util.title_length("A&B"), 3 + 4)
        self.assertEqual(util.title_length('A"B'), 3 + 5)

    def test_the_budget_is_the_escaped_length(self):
        title = "Engine " + "&" * 15
        self.assertLess(len(title), util.TITLE_MAX)
        self.assertGreater(util.title_length(title), util.TITLE_MAX)


class Grouping(unittest.TestCase):
    def test_rows_group_by_interchange(self):
        rows = [{"listing_id": "1", "interchange_number": "300-A"},
                {"listing_id": "2", "interchange_number": "300-A"},
                {"listing_id": "3", "interchange_number": "300-B"}]
        groups = util.groups_by_interchange(rows)
        self.assertEqual(sorted(groups), ["300-A", "300-B"])
        self.assertEqual(len(groups["300-A"]), 2)

    def test_a_missing_interchange_becomes_its_own_group_never_a_shared_blank(self):
        """Blank interchanges must not collapse into one group and share a title."""
        rows = [{"listing_id": "1", "interchange_number": ""},
                {"listing_id": "2", "interchange_number": None}]
        self.assertEqual(len(util.groups_by_interchange(rows)), 2)

    def test_chunks_covers_every_item_exactly_once(self):
        values = list(range(10))
        batched = list(util.chunks(values, 3))
        self.assertEqual([len(item) for item in batched], [3, 3, 3, 1])
        self.assertEqual([item for batch in batched for item in batch], values)

    def test_a_zero_batch_size_is_refused_rather_than_looping_forever(self):
        with self.assertRaises(ValueError):
            list(util.chunks([1, 2], 0))

    def test_truthy_reads_portal_booleans_conservatively(self):
        for value in (True, "1", "true", "YES", " y "):
            self.assertTrue(util.truthy(value), value)
        for value in (False, "", None, "0", "no", "maybe", "2"):
            self.assertFalse(util.truthy(value), value)


class EngineParse(unittest.TestCase):
    def test_years_make_and_displacement_come_out_of_the_source_grammar(self):
        facts = engine_titles.parse(SOURCE)
        self.assertEqual(facts["years"], [(2011, 2014)])
        self.assertEqual(facts["make"], "Ford")
        self.assertEqual(facts["displacement"], "5.0L")
        self.assertEqual(facts["vin_code"], "F")

    def test_a_model_alone_identifies_the_make_in_the_fits_grammar(self):
        facts = engine_titles.parse("ENGINE 1.8L Fits 05-10 CIVIC")
        self.assertEqual(facts["make"], "Honda", "the model implies the make")
        self.assertEqual(facts["model"], "Civic")

    def test_two_digit_fitment_years_expand_to_four(self):
        """05-10 is 2005-2010; publishing "05-10" would read as a price or a code."""
        self.assertEqual(engine_titles.parse("ENGINE 1.8L Fits 05-10 CIVIC")["years"],
                         [(2005, 2010)])

    def test_an_unrecognised_make_leaves_it_blank_rather_than_guessing(self):
        """validate() then rejects the title, which is the safe outcome."""
        facts = engine_titles.parse("2005-2010 CIVIC 1.8L Engine")
        self.assertEqual(facts["make"], "")
        self.assertFalse(engine_titles.validate("anything", "x", facts)[0])

    def test_donor_make_fills_in_and_is_marked_as_borrowed(self):
        facts = engine_titles.parse("2011 5.0L V8 Engine", {"make": "FORD TRUCK"})
        self.assertEqual(facts["make"], "Ford")
        self.assertTrue(facts["make_from_donor"],
                        "a make the title did not state must be traceable")


class EngineCompose(unittest.TestCase):
    def test_a_composed_title_fits_the_escaped_budget(self):
        facts = engine_titles.parse(SOURCE)
        title, _ = engine_titles.compose(facts)
        self.assertLessEqual(util.title_length(title), util.TITLE_MAX)
        self.assertIn("Engine", title)

    def test_overflow_drops_the_least_valuable_segment_first(self):
        facts = engine_titles.parse(SOURCE)
        roomy, _ = engine_titles.compose(facts)
        tight, dropped = engine_titles.compose(facts, limit=40)
        self.assertLessEqual(util.title_length(tight), 40)
        self.assertTrue(dropped)
        self.assertLess(len(tight), len(roomy))

    def test_the_identifying_facts_survive_a_tight_budget(self):
        """Year, make and displacement are rank 1: nothing may evict them."""
        facts = engine_titles.parse(SOURCE)
        tight, dropped = engine_titles.compose(facts, limit=40)
        self.assertIn("2011-2014", tight)
        self.assertIn("Ford", tight)
        self.assertIn("5.0L", tight)
        self.assertNotIn("years", dropped)


class EngineValidate(unittest.TestCase):
    def setUp(self):
        self.facts = engine_titles.parse(SOURCE)

    def test_a_good_rewrite_is_accepted(self):
        ok, reason = engine_titles.validate(
            "2011-2014 Ford F150 5.0L V8 VIN F Engine", SOURCE, self.facts)
        self.assertTrue(ok, reason)

    def test_a_title_that_did_not_change_is_not_a_write(self):
        ok, reason = engine_titles.validate(SOURCE, SOURCE, self.facts)
        self.assertFalse(ok)
        self.assertEqual(reason, "unchanged")

    def test_a_dropped_vin_code_is_refused(self):
        """The VIN code is what distinguishes two engines that fit the same truck."""
        ok, reason = engine_titles.validate(
            "2011-2014 Ford F150 5.0L V8 Engine", SOURCE, self.facts)
        self.assertFalse(ok)
        self.assertIn("VIN", reason)

    def test_losing_the_word_engine_is_refused(self):
        ok, reason = engine_titles.validate(
            "2011-2014 Ford F150 5.0L V8 VIN F Motor", SOURCE, self.facts)
        self.assertFalse(ok)
        self.assertIn("Engine", reason)

    def test_escaping_characters_are_refused_not_silently_escaped(self):
        ok, reason = engine_titles.validate(
            '2011-2014 Ford F150 5.0L V8 VIN F Engine & Motor', SOURCE, self.facts)
        self.assertFalse(ok)
        self.assertIn("&", reason)

    def test_missing_fitment_years_are_refused(self):
        ok, reason = engine_titles.validate("Ford Engine", SOURCE, {"make": "Ford"})
        self.assertFalse(ok)
        self.assertIn("years", reason)


class EngineBuild(unittest.TestCase):
    def test_one_title_covers_the_whole_interchange_group(self):
        groups = {"300-A": [{"listing_id": "1", "title": SOURCE},
                            {"listing_id": "2", "title": SOURCE}]}
        accepted, rejected = engine_titles.build(groups, {})
        self.assertEqual(len(accepted), 1, rejected)
        self.assertEqual(accepted[0]["listing_ids"], ["1", "2"])

    def test_two_groups_may_not_publish_under_the_same_title(self):
        """Distinct interchanges are distinct parts; one title for both misleads buyers."""
        groups = {
            "300-A": [{"listing_id": "1", "title": "2011-2014 FORD F150 5.0L V8 Engine 1"}],
            "300-B": [{"listing_id": "2", "title": "2011-2014 FORD F150 5.0L V8 Engine 2"}],
        }
        accepted, rejected = engine_titles.build(groups, {})
        self.assertEqual(accepted, [])
        self.assertTrue(all("unique" in item["reason"] for item in rejected), rejected)

    def test_a_rejection_says_why_and_keeps_its_listing_ids(self):
        groups = {"300-A": [{"listing_id": "1", "title": "no facts here"}]}
        accepted, rejected = engine_titles.build(groups, {})
        self.assertEqual(accepted, [])
        self.assertEqual(rejected[0]["listing_ids"], ["1"])
        self.assertTrue(rejected[0]["reason"])


class ProposalGuards(unittest.TestCase):
    """The AI title path is guarded on facts, not on trust in the model."""

    def test_a_proposal_that_drops_the_wheel_size_is_refused(self):
        source = {"current_title": "17x7 Alloy Wheel"}
        ok, reason = catalog_titles.validate("Alloy Wheel Rim OEM", source)
        self.assertFalse(ok)
        self.assertIn("17x7", reason)

    def test_a_proposal_that_keeps_the_size_is_accepted(self):
        source = {"current_title": "17x7 Alloy Wheel"}
        ok, reason = catalog_titles.validate(
            "17x7 Alloy Wheel Rim OEM Take Off", source)
        self.assertTrue(ok, reason)

    def test_an_over_length_proposal_is_refused(self):
        ok, reason = catalog_titles.validate("Wheel " + "y" * 90, {"current_title": "x"})
        self.assertFalse(ok)
        self.assertIn("too long", reason)

    def test_escaping_characters_are_refused_even_well_inside_the_budget(self):
        """The portal escapes them, so what eBay shows is not what was reviewed."""
        ok, reason = catalog_titles.validate("Wheel & Rim", {"current_title": "x"})
        self.assertFalse(ok)
        self.assertIn("&", reason)

    def test_an_empty_or_unchanged_proposal_is_not_a_write(self):
        self.assertFalse(catalog_titles.validate("   ", {"current_title": "x"})[0])
        self.assertEqual(catalog_titles.validate("x", {"current_title": "x"})[1],
                         "unchanged")


if __name__ == "__main__":
    unittest.main()
