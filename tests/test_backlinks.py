"""Writing a part's storefront address back into the yard record — offline.

Like ``test_orders`` and ``test_invoices``, the batches are built against the placeholders
in ``schema.example.json`` and never sent anywhere: for a write path the SQL text *is* the
behaviour. The guards asserted here are the two that decide whether this is safe to leave
on a schedule — that a run cannot overwrite wording somebody at the yard typed, and that
what it writes can never reach a shopper-facing description.
"""

import unittest

from coreyard.backlinks import Plan, listed, ours, plan
from coreyard.config import StoreProfile, bundled
from coreyard.models import Part
from coreyard.yms import schema
from coreyard.yms.backlinks import (BacklinkWriteError, build_clear, build_stamp,
                                    like_pattern)
from coreyard.yms.inventory import row_to_part

WRITE = schema.load(bundled("schema.example.json")).backlink_write
BASE = "https://example.test"
PREFIX = ours(BASE, "yard")


def stamp(pairs=(("44004", f"{BASE}/products/yard-44004"),), write=None, prefix=PREFIX):
    return build_stamp(pairs, write or WRITE, prefix)


class ExampleMapping(unittest.TestCase):
    def test_the_shipped_example_carries_a_complete_backlink_write(self):
        self.assertIsNotNone(WRITE)
        self.assertIn("UPDATE", stamp())

    def test_an_incomplete_block_names_what_is_missing(self):
        with self.assertRaisesRegex(schema.SchemaError, "stamp"):
            schema.BacklinkWrite.from_dict({"current": "SELECT 1"})


class TheGuardIsMandatory(unittest.TestCase):
    """A fragment that can overwrite anything is refused before it can run once."""

    def test_a_stamp_that_does_not_restrict_itself_is_refused(self):
        raw = {"current": "SELECT 1", "stamp": "UPDATE t SET c = v FROM (VALUES {rows}) v",
               "clear": "UPDATE t SET c = NULL WHERE id IN ({r_numbers}) AND c LIKE {ours}"}
        with self.assertRaisesRegex(schema.SchemaError, "typed by hand"):
            schema.BacklinkWrite.from_dict(raw)

    def test_a_clear_that_does_not_restrict_itself_is_refused(self):
        raw = {"current": "SELECT 1",
               "stamp": "UPDATE t SET c = v FROM (VALUES {rows}) v WHERE c LIKE {ours}",
               "clear": "UPDATE t SET c = NULL WHERE id IN ({r_numbers})"}
        with self.assertRaisesRegex(schema.SchemaError, "typed by hand"):
            schema.BacklinkWrite.from_dict(raw)

    def test_the_built_batches_carry_the_pattern(self):
        self.assertIn("'https://example.test/products/yard-%'", stamp())
        self.assertIn("'https://example.test/products/yard-%'",
                      build_clear(["44004"], WRITE, PREFIX))

    def test_a_wildcard_in_the_prefix_cannot_widen_the_pattern(self):
        """A `_` matches any character in LIKE, so an unescaped one matches other yards'."""
        self.assertEqual(like_pattern("a_b%c[d"), "N'a[_]b[%]c[[]d%'")


class WhatMayBeInterpolated(unittest.TestCase):
    def test_an_implausible_r_number_is_refused(self):
        """R#s arrive from store handles, which a person can set to arbitrary text."""
        with self.assertRaisesRegex(BacklinkWriteError, "implausible"):
            stamp([("44004'; DROP TABLE x --", f"{BASE}/products/yard-1")])

    def test_a_quote_in_an_address_is_doubled(self):
        self.assertIn("N'https://example.test/products/yard-o''hare'",
                      stamp([("1", f"{BASE}/products/yard-o'hare")]))

    def test_an_address_too_long_for_the_column_is_refused_not_truncated(self):
        """A link cut off at the column width is worse than no link at all."""
        with self.assertRaisesRegex(BacklinkWriteError, "truncated"):
            stamp([("1", f"{BASE}/products/yard-" + "x" * WRITE.limit)])

    def test_an_empty_batch_is_refused_rather_than_sent(self):
        with self.assertRaises(BacklinkWriteError):
            stamp([])
        with self.assertRaises(BacklinkWriteError):
            build_clear([], WRITE, PREFIX)

    def test_every_batch_reports_the_rows_it_really_changed(self):
        self.assertTrue(stamp().rstrip().endswith("SELECT @@ROWCOUNT AS changed;"))


class ThePlan(unittest.TestCase):
    LIVE = {"1": f"{BASE}/products/yard-1", "2": f"{BASE}/products/yard-2"}

    def test_a_part_with_no_link_is_stamped(self):
        self.assertEqual(plan(self.LIVE, {}, PREFIX).stamp,
                         [("1", self.LIVE["1"]), ("2", self.LIVE["2"])])

    def test_a_part_already_right_is_left_alone(self):
        result = plan(self.LIVE, dict(self.LIVE), PREFIX)
        self.assertEqual((result.stamp, result.clear, result.correct), ([], [], 2))

    def test_a_link_to_a_listing_that_has_gone_is_cleared(self):
        result = plan({}, {"9": f"{BASE}/products/yard-9"}, PREFIX)
        self.assertEqual(result.clear, ["9"])

    def test_wording_somebody_typed_is_skipped_never_overwritten(self):
        held = {"1": "OEM Mini Cooper tweeter trim ring, driver LH"}
        result = plan(self.LIVE, held, PREFIX)
        self.assertEqual(result.skipped, [("1", held["1"])])
        self.assertEqual([r for r, _ in result.stamp], ["2"])
        self.assertEqual(result.clear, [])

    def test_a_link_to_another_store_is_not_ours_to_clear(self):
        result = plan({}, {"9": "https://elsewhere.test/products/yard-9"}, PREFIX)
        self.assertEqual((result.clear, result.skipped), ([], []))

    def test_r_number_narrows_the_whole_plan(self):
        result = plan(self.LIVE, {"9": f"{BASE}/products/yard-9"}, PREFIX, only={"2"})
        self.assertEqual(([r for r, _ in result.stamp], result.clear), (["2"], []))


class WhatCountsAsPublished(unittest.TestCase):
    """Only a product a shopper can actually open earns a link."""

    class _Client:
        def __init__(self, nodes):
            self.nodes = nodes

        def paginate(self, *_args, **_kwargs):
            return iter(self.nodes)

    def scan(self, *nodes):
        return listed(self._Client(list(nodes)), StoreProfile(handle_prefix="yard"))

    def test_an_active_published_product_is_listed(self):
        self.assertEqual(
            self.scan({"handle": "yard-7", "status": "ACTIVE",
                       "onlineStoreUrl": f"{BASE}/products/yard-7"}),
            {"7": f"{BASE}/products/yard-7"})

    def test_a_product_on_no_channel_has_no_address_to_write(self):
        self.assertEqual(
            self.scan({"handle": "yard-7", "status": "ACTIVE", "onlineStoreUrl": None}), {})

    def test_an_archived_product_is_not_listed(self):
        self.assertEqual(
            self.scan({"handle": "yard-7", "status": "ARCHIVED",
                       "onlineStoreUrl": f"{BASE}/products/yard-7"}), {})

    def test_a_product_that_is_not_ours_is_ignored(self):
        self.assertEqual(
            self.scan({"handle": "t-shirt", "status": "ACTIVE",
                       "onlineStoreUrl": f"{BASE}/products/t-shirt"}), {})


class ABacklinkNeverReachesAShopper(unittest.TestCase):
    """The read guard. Without it the first run republishes the whole catalogue to say
    nothing, and puts a bare URL in the copy shoppers read."""

    def part(self, ecom, notes=None) -> Part:
        return row_to_part({"r_number": "1", "part_type": "Fender",
                            "ecom_desc": ecom, "notes": notes})

    def test_a_stamped_address_is_not_a_description(self):
        self.assertIsNone(self.part(f"{BASE}/products/yard-1").description)

    def test_the_part_falls_back_to_its_notes_exactly_as_if_unstamped(self):
        self.assertEqual(self.part(f"{BASE}/products/yard-1", "GOOD BLOCK BAD CRANK"),
                         self.part(None, "GOOD BLOCK BAD CRANK"))

    def test_a_description_that_merely_mentions_a_link_keeps_every_word(self):
        text = "see https://example.test/products/yard-1 for photos"
        self.assertEqual(self.part(text).description, text)

    def test_ordinary_wording_is_untouched(self):
        self.assertEqual(self.part("OEM tweeter trim ring").description,
                         "OEM tweeter trim ring")


class TheReportedShape(unittest.TestCase):
    def test_changes_counts_both_kinds_of_write(self):
        self.assertEqual(Plan(stamp=[("1", "u")], clear=["2", "3"]).changes, 3)


if __name__ == "__main__":
    unittest.main()
