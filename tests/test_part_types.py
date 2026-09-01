"""The catalogue of part types, and where the renderer's vocabulary runs out.

The report exists to be believed at a glance, so the two things worth pinning are that a
type nobody stocks cannot be counted as a gap, and that a code the extract carries but the
catalogue has never heard of is reported rather than quietly dropped.
"""

import unittest
from decimal import Decimal

from coreyard.models import Part
from coreyard.yms import part_types
from coreyard.yms.part_types import PartType


def part(code, name="TAIL LAMP", r_number="1"):
    return Part(r_number=r_number, part_type=name, part_type_code=code,
                price=Decimal("50.00"))


CATALOGUE = [PartType("166", "TAIL LAMP"), PartType("300", "ENGINE ASSEMBLY"),
             PartType("999", "WHL CYL")]


class Coverage(unittest.TestCase):
    def test_a_stocked_type_the_renderer_names_is_not_a_gap(self):
        rows = {row.code: row for row in
                part_types.coverage(CATALOGUE, [part(166)])}
        self.assertTrue(rows["166"].named)
        self.assertFalse(rows["166"].needs_wording)
        self.assertEqual(rows["166"].shopper_name, "Tail Light Lamp Assembly")

    def test_a_stocked_type_left_shouting_an_abbreviation_is_a_gap(self):
        rows = {row.code: row for row in
                part_types.coverage(CATALOGUE, [part(999, "WHL CYL")])}
        self.assertTrue(rows["999"].needs_wording)
        self.assertEqual(rows["999"].abbreviations, ("WHL",))

    def test_a_type_that_title_cases_into_plain_words_is_not_a_gap(self):
        """"CALIPER" needs no curated wording: it is already what a shopper types."""
        rows = {row.code: row for row in part_types.coverage(
            [PartType("536", "CALIPER")], [part(536, "CALIPER")])}
        self.assertEqual(rows["536"].shopper_name, "Caliper")
        self.assertFalse(rows["536"].named)
        self.assertFalse(rows["536"].needs_wording)

    def test_an_expanded_abbreviation_no_longer_counts_as_a_gap(self):
        rows = {row.code: row for row in part_types.coverage(
            [PartType("541", "BRAKE MASTER CYL")], [part(541, "BRAKE MASTER CYL")])}
        self.assertEqual(rows["541"].shopper_name, "Brake Master Cylinder")
        self.assertFalse(rows["541"].needs_wording)

    def test_an_unstocked_type_is_listed_but_never_counted_as_a_gap(self):
        """Wording for a type the yard holds none of is work nobody needs done yet."""
        rows = {row.code: row for row in part_types.coverage(CATALOGUE, [])}
        self.assertEqual(rows["999"].parts, 0)
        self.assertTrue(rows["999"].abbreviations)
        self.assertFalse(rows["999"].needs_wording)

    def test_parts_are_counted_per_type(self):
        rows = {row.code: row for row in part_types.coverage(
            CATALOGUE, [part(166, r_number="1"), part(166, r_number="2"),
                        part(300, "ENGINE ASSEMBLY", r_number="3")])}
        self.assertEqual(rows["166"].parts, 2)
        self.assertEqual(rows["300"].parts, 1)

    def test_a_code_the_catalogue_does_not_define_is_reported_not_dropped(self):
        """The two systems disagreeing about what a part type is has to be visible."""
        rows = {row.code: row for row in
                part_types.coverage(CATALOGUE, [part(742, "SPOILER")])}
        self.assertIn("742", rows)
        self.assertFalse(rows["742"].in_catalogue)
        self.assertEqual(rows["742"].parts, 1)
        self.assertEqual(rows["742"].name, "SPOILER")

    def test_the_catalogue_name_wins_over_the_extracts_copy_of_it(self):
        rows = {row.code: row for row in
                part_types.coverage([PartType("166", "TAIL LAMP")],
                                    [part(166, "TAILLAMP RIGHT")])}
        self.assertEqual(rows["166"].name, "TAIL LAMP")

    def test_a_part_with_no_type_code_is_skipped_rather_than_counted_as_blank(self):
        rows = part_types.coverage(CATALOGUE, [part(None)])
        self.assertEqual([row.code for row in rows], ["166", "300", "999"])
        self.assertEqual(sum(row.parts for row in rows), 0)

    def test_codes_are_compared_as_trimmed_text(self):
        """The catalogue returns integers and the extract sometimes returns padded text."""
        rows = {row.code: row for row in
                part_types.coverage([PartType("166", "TAIL LAMP")], [part(" 166 ")])}
        self.assertEqual(rows["166"].parts, 1)


class Report(unittest.TestCase):
    def test_the_gap_count_names_the_types_and_the_parts_behind_them(self):
        rows = part_types.coverage(CATALOGUE, [part(999, "WHL CYL"),
                                               part(999, "WHL CYL", r_number="2")])
        text = "\n".join(part_types.render(rows))
        self.assertIn("1 stocked type(s), 2 part(s)", text)
        self.assertIn("3 part types defined; 1 with parts in stock.", text)

    def test_a_fully_covered_catalogue_says_so_rather_than_printing_nothing(self):
        rows = part_types.coverage([PartType("166", "TAIL LAMP")], [part(166)])
        self.assertIn("Every stocked part type renders as words a shopper would type.",
                      "\n".join(part_types.render(rows)))

    def test_gaps_only_hides_the_covered_types_but_keeps_the_totals(self):
        rows = part_types.coverage(CATALOGUE, [part(166), part(999, "WHL CYL")])
        text = "\n".join(part_types.render(rows, gaps_only=True))
        self.assertIn("WHL CYL", text)
        self.assertNotIn("Tail Light Lamp Assembly", text)
        self.assertIn("3 part types defined", text)


if __name__ == "__main__":
    unittest.main()
