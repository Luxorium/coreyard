"""A model number that is two digits sits exactly where a two-digit year would.

``VOLVO 60 SERIES 09`` read the 60 as 1960 and left the model a bare ``VOLVO``, which put
43 live listings under titles like "1960-2008 Volvo 70 Series 60 80 XC90". A year *range*
was safe by accident, because a model number cannot form one.
"""
import unittest

from coreyard.yms.interchange import _NOTE_MAX, _tidy_note, parse_application


class TwoDigitModelNumbers(unittest.TestCase):
    """A model number that is two digits sits where a two-digit year would.

    "VOLVO 60 SERIES 09" read the 60 as 1960 and left the model a bare "VOLVO", which put
    43 live listings under titles like "1960-2008 Volvo 70 Series 60 80 XC90". Ranges were
    safe by accident, because the model number could not form one.
    """

    def test_lone_year_after_a_two_digit_model_number(self):
        parsed = parse_application("VOLVO 60 SERIES 09 (turbo), SW (XC)")
        self.assertEqual(parsed["model"], "VOLVO 60 SERIES")
        self.assertEqual((parsed["y1"], parsed["y2"]), (2009, 2009))

    def test_bare_two_digit_year_after_the_model_number(self):
        parsed = parse_application("VOLVO 70 SERIES 98")
        self.assertEqual(parsed["model"], "VOLVO 70 SERIES")
        self.assertEqual((parsed["y1"], parsed["y2"]), (1998, 1998))

    def test_year_range_after_a_two_digit_model_number_still_parses(self):
        parsed = parse_application("VOLVO 60 SERIES 11-13 XC60, w/turbo")
        self.assertEqual(parsed["model"], "VOLVO 60 SERIES")
        self.assertEqual((parsed["y1"], parsed["y2"]), (2011, 2013))

    def test_three_digit_model_numbers_are_unaffected(self):
        parsed = parse_application("VOLVO 260 SERIES 79 262, from engine ID 4971")
        self.assertEqual(parsed["model"], "VOLVO 260 SERIES")
        self.assertEqual((parsed["y1"], parsed["y2"]), (1979, 1979))

    def test_a_word_model_named_series_is_unaffected(self):
        parsed = parse_application("LINCOLN MARK SERIES 81 dual exhaust, R.")
        self.assertEqual(parsed["model"], "LINCOLN MARK SERIES")
        self.assertEqual((parsed["y1"], parsed["y2"]), (1981, 1981))

    def test_no_year_at_all_leaves_the_whole_string_as_the_model(self):
        parsed = parse_application("VOLVO 60 SERIES")
        self.assertEqual(parsed["model"], "VOLVO 60 SERIES")
        self.assertIsNone(parsed["y1"])


class QualifierNotes(unittest.TestCase):
    """A fitment qualifier is the part of the row that decides whether a part fits.

    The cap used to be a bare slice, so "…, AT, California, thru VIN 090310" was stored as
    "…, AT, California, th" — not merely unfinished, but missing the production-date split
    entirely, and reading as though no such restriction existed.
    """

    def test_a_long_note_keeps_its_production_date_split(self):
        raw = ("Throttle Valve Assembly; Base (4AFE engine, opt L01, VIN 6, 8th digit), "
               "AT, California, thru VIN 090310")
        self.assertEqual(_tidy_note(raw), raw)

    def test_a_note_never_ends_mid_word(self):
        note = _tidy_note("word " * 80)
        self.assertFalse(note.rstrip("\u2026").endswith("wor"))
        self.assertTrue(note.endswith("\u2026"))
        self.assertLessEqual(len(note), _NOTE_MAX + 1)

    def test_a_clause_the_source_repeated_is_collapsed(self):
        self.assertEqual(
            _tidy_note("w/o automatic dimming; w/o automatic dimming; w/o compass"),
            "w/o automatic dimming; w/o compass")

    def test_collapsing_is_case_insensitive_and_keeps_first_order(self):
        self.assertEqual(_tidy_note("AT; California; at"), "AT; California")

    def test_a_short_note_is_returned_verbatim(self):
        self.assertEqual(_tidy_note("4x4, thru 12/82, Federal"), "4x4, thru 12/82, Federal")

    def test_whitespace_and_edge_punctuation_still_normalise(self):
        self.assertEqual(_tidy_note("  4x4,\n  thru 12/82 ;"), "4x4, thru 12/82")


if __name__ == "__main__":
    unittest.main()
