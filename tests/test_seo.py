"""SEO rendering: symbol-free titles, side wording, and catalogue qualifier data."""

import re
import unittest
from datetime import date
from decimal import Decimal

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.yms.interchange import Application, Fitment, parse_application
from coreyard.transform import seo

STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="90-day warranty")

# Anything that fragments a title for search engines.
BANNED_IN_TITLE = set("/\\|–—-()[]{}<>*#~^_+:;\"'&")


def part(**kw) -> Part:
    base = dict(r_number="51", part_type="Side View Mirror", side="Left",
                year=2008, make="Ford", model="F-150", price=Decimal("95.00"))
    base.update(kw)
    return Part(**base)


class Titles(unittest.TestCase):
    def test_title_matches_the_expected_shape(self):
        self.assertEqual(seo.build_title(part()),
                         "2008 Ford F150 Left Driver Side View Door Mirror")


    def test_oem_never_appears_in_a_title(self):
        cases = [part(), part(side=None, part_type="Alternator"),
                 part(part_type="Engine Assembly", side=None, description="3.0L, VIN C"),
                 part(side=None, make=None, model=None, year=None)]
        for p in cases:
            self.assertNotIn("OEM", seo.build_title(p))
            self.assertNotIn("OEM", seo.meta_title(p))
        # ...but it stays a search tag and stays in the description.
        self.assertIn("Used OEM", seo.build_tags(part(), STORE))
        self.assertIn("OEM", seo.build_body_html(part(), STORE))

    def test_title_has_no_seo_hostile_symbols(self):
        cases = [
            part(),
            part(part_type="Anti-Lock Brake Pts", side=None),
            part(part_type="Eng/Motor Cont Mod", side=None),
            part(part_type="AC Compressor", side=None, model="Pup (Pickup)"),
            part(side="Right", make="Chevrolet Truck", model="Silverado 1500"),
        ]
        for p in cases:
            title = seo.build_title(p)
            found = BANNED_IN_TITLE & set(title)
            self.assertFalse(found, f"{title!r} contains {sorted(found)}")

    def test_spanned_titles_carry_no_symbol_but_the_year_span(self):
        # Shopify publishes compact=True, so this is the form the catalogue actually
        # carries; the span's own hyphen is the one symbol the site chose to allow.
        for p in [part(), part(side="Right", part_type="Tail Lamp"),
                  part(part_type="Grille", description="BLACK TEXTURED")]:
            title = seo.build_title(p, compact=True)
            bare = re.sub(r"\b\d{4}-\d{4}\b", "", title)
            found = BANNED_IN_TITLE & set(bare)
            self.assertFalse(found, f"{title!r} contains {sorted(found)}")

    def test_letter_digit_hyphen_closes_up_for_search(self):
        # Shoppers type "F150", not "F-150".
        self.assertIn("F150", seo.build_title(part()))
        self.assertNotIn("F-150", seo.build_title(part()))

    def test_side_is_spelled_out_both_ways(self):
        # Part type already contains "Side", so it supplies the word once.
        self.assertIn("Left Driver Side View", seo.build_title(part(side="Left")))
        self.assertIn("Right Passenger Side View", seo.build_title(part(side="Right")))
        # Part type without "Side" gets the explicit phrasing instead.
        self.assertIn("Driver Side Left Alternator",
                      seo.build_title(part(side="Left", part_type="Alternator")))
        self.assertNotIn("Side", seo.build_title(part(side=None, part_type="Alternator")))

    def test_side_does_not_double_the_word_side(self):
        self.assertNotIn("Side Side", seo.build_title(part()))
        self.assertEqual(seo.build_title(part()).count("Side"), 1)

    def test_years_avoid_dashes(self):
        def titled(y1, y2):
            fit = [Fitment(make="Ford", model="F150", year_start=y1, year_end=y2)]
            return seo.build_title(part(fitment=fit))

        self.assertIn("2008", titled(2008, 2008))
        self.assertIn("2008 2009 2010", titled(2008, 2010))     # short span listed out
        self.assertIn("2004 thru 2012", titled(2004, 2012))     # long span uses a word

    def test_multiple_models_run_together_without_punctuation(self):
        fit = [
            Fitment(make="GMC", model="Acadia", year_start=2008, year_end=2011),
            Fitment(make="Buick", model="Enclave", year_start=2009, year_end=2011),
        ]
        title = seo.build_title(part(side=None, part_type="Alternator", fitment=fit))
        self.assertIn("GMC Acadia Buick Enclave", title)
        self.assertNotIn("/", title)
        self.assertNotIn(",", title)

    def test_a_make_is_never_said_twice(self):
        fit = [
            Fitment(make="Infiniti", model="QX60", year_start=2013, year_end=2017),
            Fitment(make="Nissan", model="Pathfinder", year_start=2013, year_end=2017),
            Fitment(make="Infiniti", model="JX35", year_start=2013, year_end=2017),
        ]
        title = seo.build_title(part(side=None, part_type="Alternator", fitment=fit),
                                compact=True)
        self.assertIn("Infiniti QX60 JX35 Nissan Pathfinder", title)
        self.assertEqual(1, title.count("Infiniti"))

    def test_a_repeated_model_word_collapses(self):
        fit = [Fitment(make="Chevrolet", model=f"Silverado {n}", year_start=2015,
                       year_end=2019) for n in (1500, 2500, 3500)]
        title = seo.build_title(part(side=None, part_type="Alternator", fitment=fit),
                                compact=True)
        self.assertIn("Chevrolet Silverado 1500 2500 3500", title)
        self.assertEqual(1, title.count("Silverado"))


class Qualifiers(unittest.TestCase):
    def test_parse_application_keeps_the_qualifier_note(self):
        parsed = parse_application("ISUZU PUP (PICKUP) 86-87 2.3L, California")
        self.assertEqual(parsed["y1"], 1986)
        self.assertEqual(parsed["y2"], 1987)
        self.assertEqual(parsed["note"], "2.3L, California")

    def test_qualifiers_are_listed_under_the_vehicle(self):
        fit = [Fitment(make="Isuzu", model="Pup", year_start=1982, year_end=1987,
                       applications=[
                           Application(1982, 1985, "California"),
                           Application(1986, 1986, "1.9L, California"),
                           Application(1986, 1987, "2.3L, California"),
                       ])]
        body = seo.build_body_html(part(side=None, part_type="Alternator", fitment=fit), STORE)
        self.assertIn("1986 — 1.9L, California", re.sub(r"<[^>]+>", "", body))
        self.assertIn("1986-1987 — 2.3L, California", re.sub(r"<[^>]+>", "", body))

    def test_vehicle_without_qualifiers_stays_a_flat_list_item(self):
        fit = [Fitment(make="GMC", model="Acadia", year_start=2008, year_end=2011,
                       applications=[Application(2008, 2011, "")])]
        body = seo.build_body_html(part(fitment=fit), STORE)
        self.assertIn("<li>2008-2011 GMC Acadia</li>", body)

    def test_engine_and_drivetrain_become_tags(self):
        fit = [Fitment(make="Mitsubishi", model="Pickup", year_start=1983, year_end=1985,
                       applications=[Application(1983, 1985, "manifold, 4WD, 2.0L")])]
        tags = seo.build_tags(part(side="Left", fitment=fit), STORE)
        self.assertIn("2.0L", tags)
        self.assertIn("4WD", tags)
        self.assertIn("Driver Side Left", tags)

    def test_side_appears_in_the_spec_list(self):
        body = seo.build_body_html(part(), STORE)
        self.assertIn("<strong>Side:</strong> Driver Side Left", body)


class PartSpecFromNotes(unittest.TestCase):
    """The source system hides the real remark behind a legacy header; it carries the specs."""

    def test_legacy_header_is_stripped_from_the_note(self):
        from coreyard.yms.inventory import row_to_part
        p = row_to_part({
            "r_number": "12265", "part_type": "Engine Assembly", "price": "1200.00",
            "notes": "Old ID 6565||ENG||2060||10000.001,,,(3.0L, VIN C, 4th digit, VQ30DE)",
        })
        self.assertEqual(p.description, "3.0L, VIN C, 4th digit, VQ30DE")

    def test_condition_disclosure_survives(self):
        from coreyard.yms.inventory import row_to_part
        p = row_to_part({
            "r_number": "12081", "part_type": "Engine Assembly", "price": "800.00",
            "notes": "Old ID 6565||ENG||189.02||1001.001,,,GOOD BLOCK BAD CRANK 2.2L",
        })
        self.assertIn("BAD CRANK", p.description)
        self.assertIn("BAD CRANK", seo.build_body_html(p, STORE))

    def test_engine_title_carries_displacement_and_vin_code(self):
        p = part(part_type="Engine Assembly", side=None, year=1997,
                 make="Nissan", model="Maxima",
                 description="3.0L, VIN C, 4th digit, VQ30DE")
        title = seo.build_title(p)
        self.assertIn("3.0L", title)
        self.assertIn("VIN C", title)
        self.assertIn("Engine Motor Assembly", title)
        self.assertFalse(BANNED_IN_TITLE & set(title), title)

    def test_cylinder_count_is_picked_up(self):
        p = part(part_type="Engine Assembly", side=None,
                 description="4.2L (VIN S, 8th digit),6 cyl")
        self.assertIn("6 Cylinder", seo.build_title(p))


class QualifierSafety(unittest.TestCase):
    def test_conflicting_engine_sizes_never_reach_the_title(self):
        fit = [Fitment(make="Mitsubishi", model="Pickup", year_start=1983, year_end=1985,
                       applications=[
                           Application(1983, 1985, "manifold, 2WD, 2.0L"),
                           Application(1983, 1983, "manifold, 2WD, 2.6L"),
                       ])]
        title = seo.build_title(part(side=None, part_type="Alternator", fitment=fit))
        self.assertNotIn("2.0L", title)      # would be a false spec claim
        self.assertNotIn("2.6L", title)
        self.assertIn("2WD", title)          # unanimous, so it is safe to state
        # ...but both sizes are still shown, attributed, in the description.
        body = seo.build_body_html(part(side=None, part_type="Alternator", fitment=fit), STORE)
        self.assertIn("2.0L", body)
        self.assertIn("2.6L", body)

    def test_unanimous_qualifier_reaches_the_title(self):
        fit = [Fitment(make="Isuzu", model="Pup", year_start=1986, year_end=1987,
                       applications=[
                           Application(1986, 1986, "1.9L, California"),
                           Application(1986, 1987, "1.9L, California"),
                       ])]
        title = seo.build_title(part(side=None, part_type="Alternator", fitment=fit))
        self.assertIn("1.9L", title)
        self.assertIn("California", title)




class Findability(unittest.TestCase):
    def test_interchange_number_is_tagged(self):
        tags = seo.build_tags(part(interchange_number="545-01883"), STORE)
        self.assertIn("Interchange 545-01883", tags)
        self.assertIn("545-01883", tags)   # bare form, how buyers paste it

    def test_photos_get_distinct_alt_text(self):
        p = part()
        first, second = seo.image_alt(p, 1), seo.image_alt(p, 2)
        self.assertEqual(first, seo.build_title(p))
        self.assertTrue(second.endswith("photo 2"))
        self.assertNotEqual(first, second)

    def test_alt_text_survives_a_part_with_no_vehicle(self):
        bare = Part(r_number="9", part_type="Alternator", price=Decimal("10.00"))
        self.assertTrue(seo.image_alt(bare, 1).strip())


class Casing(unittest.TestCase):
    def test_slash_separated_models_are_title_cased_individually(self):
        self.assertEqual(seo.clean_model("S10/S15/SONOMA"), "S10/S15/Sonoma")

    def test_common_words_are_not_shouted_as_acronyms(self):
        self.assertEqual(seo.clean_model("EXPRESS 1500 VAN"), "Express 1500 Van")

    def test_real_acronyms_stay_upper(self):
        self.assertEqual(seo.clean_model("CTS"), "CTS")
        self.assertEqual(seo.clean_model("SILVERADO 1500"), "Silverado 1500")




class AltLength(unittest.TestCase):
    def test_alt_text_stays_short_enough_to_be_read_aloud(self):
        fit = [Fitment(make="GMC", model=f"Model{i}", year_start=2008, year_end=2011)
               for i in range(6)]
        p = part(fitment=fit, part_type="Engine Assembly", side=None)
        for i in (1, 2, 9):
            self.assertLessEqual(len(seo.image_alt(p, i)), 140)
        self.assertFalse(seo.image_alt(p, 1).endswith(" "))


class PlaceholderYears(unittest.TestCase):
    """The yard marks open-ended runs with 1940 / 2030 instead of nulls."""

    def test_placeholder_start_year_is_not_advertised(self):
        fit = [Fitment(make="Volvo", model="70 Series", year_start=1940, year_end=2011),
               Fitment(make="Volvo", model="70 Series", year_start=1999, year_end=2011)]
        self.assertEqual(seo._year_span(part(fitment=fit)), (1999, 2011))

    def test_placeholder_end_year_is_not_advertised(self):
        fit = [Fitment(make="Mazda", model="3", year_start=1975, year_end=2030),
               Fitment(make="Mazda", model="3", year_start=1975, year_end=1999)]
        self.assertEqual(seo._year_span(part(fitment=fit)), (1975, 1999))

    def test_span_never_reaches_beyond_next_model_year(self):
        fit = [Fitment(make="Mazda", model="3", year_start=2001, year_end=2030)]
        _, end = seo._year_span(part(fitment=fit))
        self.assertLessEqual(end, date.today().year + 1)

    def test_all_placeholder_rows_fall_back_to_the_parts_own_year(self):
        fit = [Fitment(make="Volvo", model="70 Series", year_start=1940, year_end=2030)]
        self.assertEqual(seo._year_span(part(fitment=fit, year=2005)), (2005, 2005))

    def test_real_spans_are_left_alone(self):
        fit = [Fitment(make="Ford", model="Edge", year_start=2007, year_end=2015),
               Fitment(make="Ford", model="Edge", year_start=2009, year_end=2012)]
        self.assertEqual(seo._year_span(part(fitment=fit)), (2007, 2015))

    def test_no_placeholder_year_survives_into_a_title(self):
        fit = [Fitment(make="Volvo", model="70 Series", year_start=1940, year_end=2030),
               Fitment(make="Volvo", model="70 Series", year_start=2001, year_end=2008)]
        title = seo.build_title(part(fitment=fit))
        self.assertNotIn("1940", title)
        self.assertNotIn("2030", title)


class VehicleLabels(unittest.TestCase):
    def test_shorter_make_inside_the_model_is_not_repeated(self):
        self.assertEqual(seo._vehicle_label("Mercedes-Benz", "Mercedes 450"), "Mercedes 450")

    def test_exact_make_prefix_still_collapses(self):
        self.assertEqual(seo._vehicle_label("Isuzu", "Isuzu I-290"), "Isuzu I-290")

    def test_a_model_that_merely_starts_with_a_letter_run_is_kept_whole(self):
        self.assertEqual(seo._vehicle_label("Ford", "E150 Van"), "Ford E150 Van")

    def test_bare_make_is_dropped_when_a_specific_model_covers_it(self):
        fit = [Fitment(make="Volvo", model=None, year_start=2001, year_end=2008),
               Fitment(make="Volvo", model="70 Series", year_start=2001, year_end=2008)]
        self.assertEqual(seo._model_labels(part(fitment=fit)), ["Volvo 70 Series"])

    def test_bare_make_survives_when_it_is_all_we_know(self):
        fit = [Fitment(make="Volvo", model=None, year_start=2001, year_end=2008)]
        self.assertEqual(seo._model_labels(part(fitment=fit)), ["Volvo"])


class ShortWordCasing(unittest.TestCase):
    def test_short_ordinary_words_are_not_shouted(self):
        for token, want in (("CAP", "Cap"), ("SUN", "Sun"), ("BOX", "Box"), ("PAN", "Pan")):
            self.assertEqual(seo._fix_token(token), want)

    def test_genuine_trim_acronyms_still_shout(self):
        for token in ("CTS", "ESV", "XTS", "SUV", "GT"):
            self.assertEqual(seo._fix_token(token), token)

    def test_yard_shorthand_expands_to_shopper_wording(self):
        self.assertEqual(seo.expand_part_type("chassis cont mod"), "Chassis Control Module")
        self.assertEqual(seo.expand_part_type("center cap"), "Wheel Center Cap")


class TagsWithoutFitment(unittest.TestCase):
    """A part with no interchange rows still has to tag a sane vehicle.

    The fitment branch routes make and model through _vehicle_label; the fallback
    used to join them raw, so the yard's "DODGE TRUCK" / "DODGE 1500 PICKUP" pair
    became the tag "2019 Dodge Dodge 1500". 180 live parts carried a doubled make.
    """

    def _vehicle_tags(self, **kw):
        p = part(fitment=[], **kw)
        return [t for t in seo.build_tags(p, STORE) if t[:4].isdigit()]

    def test_make_is_not_doubled_when_the_model_carries_it(self):
        self.assertEqual(
            self._vehicle_tags(year=2019, make="DODGE TRUCK", model="DODGE 1500 PICKUP"),
            ["2019 Dodge 1500"])

    def test_short_form_of_the_make_in_the_model_is_also_caught(self):
        self.assertEqual(
            self._vehicle_tags(year=2010, make="MERCEDES-BENZ TRUCK",
                               model="MERCEDES SPRINTER 2500"),
            ["2010 Mercedes Sprinter 2500"])

    def test_an_ordinary_make_and_model_still_join(self):
        # Tags keep the model's hyphen; only titles strip it.
        self.assertEqual(
            self._vehicle_tags(year=2008, make="Ford", model="F-150"),
            ["2008 Ford F-150"])

    def test_the_bare_make_is_still_tagged(self):
        tags = seo.build_tags(part(fitment=[], make="DODGE TRUCK",
                                   model="DODGE 1500 PICKUP"), STORE)
        self.assertIn("Dodge", tags)


if __name__ == "__main__":
    unittest.main()


class TitleQualifierWording(unittest.TestCase):
    """The catalogue writes for a parts counter; a title is read on a results page."""

    def _with_note(self, note: str, **kw) -> Part:
        app = Application(year_start=1998, year_end=2000, note=note)
        fitment = Fitment(year_start=1998, year_end=2000, make="Ford", model="Ranger",
                          applications=[app])
        return part(fitment=[fitment], **kw)

    def test_years_span_instead_of_listing(self):
        self.assertIn("1998-2000", seo.build_title(self._with_note("2.3L"), compact=True))

    def test_a_bare_side_letter_is_dropped_when_the_part_states_its_side(self):
        title = seo.build_title(self._with_note("L"), compact=True)
        self.assertNotRegex(title, r"\bL\b")

    def test_a_bare_side_letter_is_spelled_out_when_the_part_has_none(self):
        title = seo.build_title(self._with_note("R", side=None), compact=True)
        self.assertIn("Right", title)

    def test_an_exclusion_qualifier_never_reaches_a_title(self):
        title = seo.build_title(self._with_note("exc electric vehicle"), compact=True)
        self.assertNotIn("electric", title.lower())

    def test_counter_abbreviations_are_expanded(self):
        self.assertIn("Sedan", seo.build_title(self._with_note("Sdn"), compact=True))


class FinishAndConditionFromNote(unittest.TestCase):
    """What the yard wrote about this one part, normalised but not embellished."""

    def test_finish_is_stated(self):
        self.assertEqual("Chrome", seo.title_condition(part(description="CHROME")))

    def test_the_yards_own_wording_and_order_is_kept(self):
        self.assertEqual("Black Textured",
                         seo.title_condition(part(description="BLACK TEXTURED")))

    def test_a_flaw_is_disclosed_beside_the_finish(self):
        # "CHROME BUBBLED" is bubbled chrome: the buyer sees it in the photographs either
        # way, so it belongs in the title rather than in a not-as-described case.
        self.assertEqual("Chrome Bubbled",
                         seo.title_condition(part(description="CHROME BUBBLED")))

    def test_misspelled_flaws_normalise_to_one_word(self):
        for spelling in ("faded", "fading", "fadding", "fadded", "faddin"):
            self.assertEqual("Faded", seo.title_condition(part(description=spelling)),
                             f"{spelling!r} should normalise")

    def test_a_positive_note_is_stated_too(self):
        self.assertEqual("Tested", seo.title_condition(part(description="TESTED, RUNS")))

    def test_a_note_it_cannot_read_makes_no_claim(self):
        self.assertEqual("", seo.title_condition(part(description="upper, 4DR")))

    def test_condition_reaches_the_title_after_the_part_type(self):
        title = seo.build_title(part(part_type="Grille", side=None,
                                     description="CHROME BUBBLED"), compact=True)
        self.assertTrue(title.rstrip().endswith("Grille Chrome Bubbled"), title)


class TruncationIsTheLastResort(unittest.TestCase):
    """Naming one vehicle and the whole part beats four vehicles and half the part."""

    def _many_models(self):
        apps = [Application(year_start=2008, year_end=2011, note="")]
        return part(
            part_type="Anti-lock Brake Pts", side=None,
            fitment=[Fitment(year_start=2008, year_end=2011, make=make, model=model,
                             applications=apps)
                     for make, model in (("GMC", "Acadia"), ("Saturn", "Outlook"),
                                         ("Buick", "Enclave"), ("Chevrolet", "Traverse"))],
        )

    def test_a_marketplace_title_keeps_the_part_type_whole(self):
        title, dropped = seo.fit_title(self._many_models(), STORE, limit=80, compact=True)
        self.assertNotIn("truncated", dropped, title)
        self.assertLessEqual(len(title), 80)
        # The part type is the last segment in compact order, so this is what truncation ate.
        self.assertTrue(title.rstrip().endswith("Module"), title)

    def test_dropping_a_vehicle_name_is_preferred_to_cutting_the_title(self):
        title, _ = seo.fit_title(self._many_models(), STORE, limit=80, compact=True)
        self.assertIn("GMC Acadia", title)
        self.assertNotIn("Chevrolet Traverse", title)


class WhatATightTitleGivesUpFirst(unittest.TestCase):
    """On eBay's 80 characters, what is sacrificed matters as much as how much."""

    def _crowded(self):
        apps = [Application(year_start=2005, year_end=2010, note="")]
        return part(
            part_type="Side View Mirror", side="Right",
            fitment=[Fitment(year_start=2005, year_end=2010, make=make, model=model,
                             applications=apps)
                     for make, model in (("Chevrolet", "Cobalt"), ("Pontiac", "G5"),
                                         ("Pontiac", "Pursuit"), ("Saturn", "Ion"))],
        )

    def test_a_vehicle_name_is_given_up_before_the_side(self):
        # A mirror sold without a side is a return, not a sale; another vehicle name is not.
        title, dropped = seo.fit_title(self._crowded(), STORE, limit=80, compact=True)
        self.assertNotIn("side", dropped, title)
        self.assertNotIn("truncated", dropped, title)
        self.assertIn("Right", title)
        self.assertLessEqual(len(title), 80)

    def test_the_part_type_always_survives(self):
        title, _ = seo.fit_title(self._crowded(), STORE, limit=80, compact=True)
        self.assertIn("Mirror", title)
