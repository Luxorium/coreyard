"""Resolving a portal listing to the part it is, from grid data alone.

The property that matters is the refusal. Resolving most listings cheaply is the point, but
resolving one *wrongly* retitles and reprices a listing as a different part, which is worse
than never touching it — so an ambiguous key must yield nothing at all.
"""

import unittest
from decimal import Decimal

from coreyard.ebay import link
from coreyard.models import Part


def part(r_number, stock="1001", code=166, **kwargs):
    return Part(r_number=r_number, part_type=kwargs.pop("part_type", "TAIL LAMP"),
                part_type_code=code, stock_number=stock,
                price=Decimal("50.00"), quantity=1, **kwargs)


def row(listing_id, stock="1001", part_type="166"):
    return {"listing_id": listing_id, "stock_number": stock, "part_type": part_type,
            "title": "a listing"}


class Resolution(unittest.TestCase):
    def test_one_part_of_a_type_from_one_donor_resolves(self):
        resolved, unresolved = link.resolve([row("L1")], [part("51")])
        self.assertEqual(resolved["L1"].r_number, "51")
        self.assertEqual(unresolved, [])

    def test_two_parts_sharing_a_donor_and_type_resolve_to_neither(self):
        """A car has two tail lamps. Picking one would be a coin flip on a live listing."""
        resolved, unresolved = link.resolve([row("L1")], [part("51"), part("52")])
        self.assertEqual(resolved, {})
        self.assertIn("share this donor", unresolved[0]["reason"])
        self.assertEqual(sorted(unresolved[0]["candidates"]), ["51", "52"])

    def test_a_donor_the_yard_does_not_have_resolves_to_nothing(self):
        resolved, unresolved = link.resolve([row("L1", stock="9999")], [part("51")])
        self.assertEqual(resolved, {})
        self.assertIn("no yard part", unresolved[0]["reason"])

    def test_a_listing_without_a_stock_number_is_refused(self):
        resolved, unresolved = link.resolve([row("L1", stock="")], [part("51")])
        self.assertEqual(resolved, {})
        self.assertIn("no stock number", unresolved[0]["reason"])

    def test_the_part_type_is_part_of_the_key(self):
        """Same donor, different part: a mirror must not resolve to the tail lamp."""
        resolved, _ = link.resolve([row("L1", part_type="128")], [part("51", code=166)])
        self.assertEqual(resolved, {})

    def test_the_same_donor_yields_different_parts_of_different_types(self):
        rows = [row("L1", part_type="166"), row("L2", part_type="128")]
        parts = [part("51", code=166), part("52", code=128, part_type="MIRROR")]
        resolved, unresolved = link.resolve(rows, parts)
        self.assertEqual(resolved["L1"].r_number, "51")
        self.assertEqual(resolved["L2"].r_number, "52")
        self.assertEqual(unresolved, [])

    def test_values_are_compared_as_trimmed_text_not_as_numbers(self):
        """The portal returns these as strings with padding; the yard as integers."""
        resolved, _ = link.resolve([row("L1", stock=" 1001 ", part_type=" 166 ")],
                                   [part("51", stock=1001, code=166)])
        self.assertEqual(resolved["L1"].r_number, "51")

    def test_resolution_scales_to_many_rows_without_rereading_the_yard(self):
        parts = [part(str(n), stock=str(n)) for n in range(500)]
        rows = [row(f"L{n}", stock=str(n)) for n in range(500)]
        resolved, unresolved = link.resolve(rows, parts)
        self.assertEqual(len(resolved), 500)
        self.assertEqual(unresolved, [])


class TitleRNumber(unittest.TestCase):
    """The portal ends its generated title with the R#, which settles the paired parts."""

    def test_a_confirmed_r_number_in_the_title_resolves_a_left_right_pair(self):
        left, right = part("52507", side="Left"), part("52506", side="Right")
        listing = {**row("L1"),
                   "title": "Driver Left Tail Light Fits 10-12 FUSION 52507"}
        resolved, unresolved = link.resolve([listing], [left, right])
        self.assertEqual(resolved["L1"].r_number, "52507")
        self.assertEqual(unresolved, [])

    def test_a_trailing_number_that_is_not_an_r_number_falls_back_to_the_donor_key(self):
        resolved, _ = link.resolve(
            [{**row("L1"), "title": "Tail Light Fits 10-12 FUSION 999999"}], [part("51")])
        self.assertEqual(resolved["L1"].r_number, "51")

    def test_an_r_number_contradicting_the_donor_is_refused_not_believed(self):
        """A number that names a part off a different donor was never an R# here."""
        other = part("77", stock="2002")
        resolved, unresolved = link.resolve(
            [{**row("L1", stock="1001"), "title": "Tail Light Fits 10-12 FUSION 77"}],
            [other, part("51"), part("52")])
        self.assertEqual(resolved, {})
        self.assertIn("share this donor", unresolved[0]["reason"])

    def test_an_r_number_matching_the_donor_but_not_the_type_is_refused(self):
        mirror = part("60", code=128, part_type="MIRROR")
        resolved, _ = link.resolve(
            [{**row("L1", part_type="166"), "title": "Tail Light 60"}],
            [mirror, part("51"), part("52")])
        self.assertEqual(resolved, {})

    def test_a_number_earlier_in_the_title_is_never_read_as_an_identity(self):
        """Only the trailing token is the R#; a year span or a size is not."""
        self.assertIsNone(link.r_number_in_title("Tail Light Fits 10-12 FUSION"))
        self.assertEqual(link.r_number_in_title("Fits 10-12 FUSION 52507"), "52507")

    def test_the_title_rule_settles_a_pair_the_donor_key_cannot(self):
        rows = [{**row("L1"), "title": "Driver Left Tail Light Fits 98-99 RANGER 53526"},
                {**row("L2"), "title": "Passenger Right Tail Light Fits 98-99 RANGER 53525"}]
        resolved, unresolved = link.resolve(rows, [part("53525"), part("53526")])
        self.assertEqual(resolved["L1"].r_number, "53526")
        self.assertEqual(resolved["L2"].r_number, "53525")
        self.assertEqual(unresolved, [])


class DetailRescue(unittest.TestCase):
    def test_a_cached_edit_form_rescues_an_ambiguous_listing(self):
        resolved, unresolved = link.resolve([row("L1")], [part("51"), part("52")])
        merged, still = link.merge_details(
            resolved, unresolved, {"L1": {"r_number": "52"}},
            {"51": part("51"), "52": part("52")})
        self.assertEqual(merged["L1"].r_number, "52")
        self.assertEqual(still, [])

    def test_an_unhelpful_cache_leaves_the_listing_unresolved(self):
        resolved, unresolved = link.resolve([row("L1")], [part("51"), part("52")])
        merged, still = link.merge_details(resolved, unresolved, {}, {})
        self.assertEqual(merged, {})
        self.assertEqual(len(still), 1)

    def test_a_cached_r_number_the_yard_does_not_have_is_not_trusted(self):
        resolved, unresolved = link.resolve([row("L1")], [part("51"), part("52")])
        merged, still = link.merge_details(
            resolved, unresolved, {"L1": {"r_number": "99"}}, {"51": part("51")})
        self.assertEqual(merged, {})
        self.assertEqual(len(still), 1)


if __name__ == "__main__":
    unittest.main()
