"""SEO rendering: symbol-free titles, side wording, and catalogue qualifier data."""

import re
import unittest
from datetime import date
from decimal import Decimal

from coreyard.config import StoreProfile
from coreyard.profile import CatalogProfile
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

    def test_an_unrestricted_year_run_is_listed_beside_the_qualified_ones(self):
        """Only qualified applications used to be listed, so the year run that fits
        *without* a restriction vanished — and its owner read the remaining bullets as a
        list of requirements none of which mentions their car."""
        fit = [Fitment(make="Chevrolet", model="Impala", year_start=2012, year_end=2019,
                       applications=[Application(2012, 2013, ""),
                                     Application(2014, 2016, "VIN W (4th digit, Limited)"),
                                     Application(2017, 2019, "3.6L")])]
        text = re.sub(r"<[^>]+>", "", seo.build_body_html(part(fitment=fit), STORE))
        self.assertIn("2012-2013", text)
        self.assertIn("2017-2019 — 3.6L", text)

    def test_a_long_application_list_says_how_many_it_left_out(self):
        fit = [Fitment(make="Ford", model="F-150", year_start=2000, year_end=2011,
                       applications=[Application(2000 + n, 2000 + n, f"{n}.0L")
                                     for n in range(12)])]
        text = re.sub(r"<[^>]+>", "", seo.build_body_html(part(fitment=fit), STORE))
        self.assertIn("and 4 more year/option variants", text)

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

    def test_a_raw_storefront_description_loses_only_broken_legacy_punctuation(self):
        from coreyard.yms.inventory import row_to_part
        p = row_to_part({
            "r_number": "91", "part_type": "Anti-lock Brake Pts", "price": "40.00",
            "ecom_desc": "pump only),4 WHEEL ABS",
        })
        self.assertEqual(p.description, "pump only, 4 WHEEL ABS")


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
        self.assertIn(seo.expand_part_type(p.part_type), first)
        self.assertIn("from 2008 Ford F-150", first)
        self.assertNotEqual(first, seo.build_title(p))
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

    def test_alt_describes_the_part_not_the_whole_fitment_title(self):
        fit = [Fitment(make="GMC", model="Acadia", year_start=2008, year_end=2011),
               Fitment(make="Buick", model="Enclave", year_start=2009, year_end=2011)]
        alt = seo.image_alt(part(fitment=fit, year=2010, make="GMC", model="Acadia",
                                 part_type="Alternator", side=None), 2)
        self.assertIn("Used OEM Alternator Generator from 2010 GMC Acadia", alt)
        self.assertIn("photo 2", alt)
        self.assertNotIn("Buick", alt)


class PlaceholderYears(unittest.TestCase):
    """The yard marks open-ended runs with 1940/1950 and 2030 instead of nulls."""

    def test_placeholder_start_year_is_not_advertised(self):
        fit = [Fitment(make="Volvo", model="70 Series", year_start=1940, year_end=2011),
               Fitment(make="Volvo", model="70 Series", year_start=1999, year_end=2011)]
        self.assertEqual(seo._year_span(part(fitment=fit)), (1999, 2011))

    def test_the_yards_1950_placeholder_is_not_advertised(self):
        fit = [Fitment(make="Mazda", model="3", year_start=1950, year_end=1950),
               Fitment(make="Mazda", model="3", year_start=2014, year_end=2023)]
        self.assertEqual(seo._year_span(part(fitment=fit)), (2014, 2023))

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

    def test_no_placeholder_year_survives_body_or_tags(self):
        bad = Fitment(
            make="Mazda", model="3", year_start=1950, year_end=2030,
            applications=[Application(1950, 1950, "naturally aspirated"),
                          Application(2030, 2030, "2.5L")],
        )
        p = part(fitment=[bad], year=2018, make="Mazda", model="3")
        body = seo.build_body_html(p)
        tags = seo.build_tags(p)
        for sentinel in ("1940", "1950", "2030"):
            self.assertNotIn(sentinel, body)
            self.assertFalse(any(sentinel in tag for tag in tags))

    def test_make_less_artifacts_are_omitted_when_normal_rows_exist(self):
        fit = [Fitment(make="Mazda", model="3", year_start=2014, year_end=2023),
               Fitment(make=None, model="CX-", year_start=1950, year_end=1950),
               Fitment(make=None, model="Mazda CX-", year_start=2030, year_end=2030)]
        p = part(fitment=fit)
        self.assertEqual([f.model for f in seo.display_fitments(p)], ["3"])
        self.assertNotIn("CX-", seo.build_body_html(p))

    def test_real_model_only_fitment_is_not_lost(self):
        fit = [Fitment(make=None, model="Legacy", year_start=2015, year_end=2019)]
        self.assertEqual(seo.display_fitments(part(fitment=fit)), fit)


class VehicleLabels(unittest.TestCase):
    def test_shorter_make_inside_the_model_is_not_repeated(self):
        self.assertEqual(seo._vehicle_label("Mercedes-Benz", "Mercedes 450"), "Mercedes 450")

    def test_exact_make_prefix_still_collapses(self):
        self.assertEqual(seo._vehicle_label("Isuzu", "Isuzu I-290"), "Isuzu I-290")

    def test_a_model_that_merely_starts_with_a_letter_run_is_kept_whole(self):
        self.assertEqual(seo._vehicle_label("Ford", "E150 Van"), "Ford E150 Van")

    def test_a_structured_model_drops_the_make_its_own_column_already_has(self):
        self.assertEqual(seo.model_without_make("Lexus", "Lexus ES350"), "ES350")
        self.assertEqual(seo.model_without_make("Lincoln", "Lincoln & Town CAR"),
                         "Town Car")

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
        self.assertEqual(seo.expand_part_type("radiators"), "Radiator")


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

    def test_corrupt_only_fitment_falls_back_to_the_donor_vehicle_tag(self):
        bad = Fitment(make=None, model="CX-", year_start=1950, year_end=1950)
        tags = seo.build_tags(part(fitment=[bad], year=2018, make="Mazda", model="3"),
                              STORE)
        self.assertIn("2018 Mazda 3", tags)


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


class MetadataBudgets(unittest.TestCase):
    def test_meta_title_drops_vehicle_context_before_cutting_the_part_name(self):
        crowded = part(
            part_type="Anti-lock Brake Pts", side=None,
            fitment=[Fitment(make="General Motors", model="A Very Long Vehicle Model",
                             year_start=2008, year_end=2020)],
        )
        title = seo.meta_title(crowded)
        self.assertLessEqual(len(title), seo.META_TITLE_MAX)
        self.assertIn("ABS Anti Lock Brake Pump Control Module", title)

    def test_meta_description_adds_only_complete_sentences(self):
        crowded = part(
            stock_number="123456", part_type="Anti-lock Brake Pts", side="Right",
            fitment=[Fitment(make="General Motors", model=f"Long Vehicle Model {n}",
                             year_start=2008, year_end=2020) for n in range(4)],
        )
        description = seo.meta_description(crowded, STORE)
        self.assertLessEqual(len(description), seo.META_DESC_MAX)
        self.assertTrue(description.endswith("."), description)
        self.assertIn("ABS Anti Lock Brake Pump Control Module", description)
        self.assertNotRegex(description, r"\b(?:Test|90-day|Stock)\.$")


class SpendingTheWholeTitleBudget(unittest.TestCase):
    """``title_max_models = 0`` names vehicles until the characters run out.

    A capped title spends characters on the phrase "and 17 more", which names no vehicle
    and carries no search term. Those suppressed names are the long-tail queries a salvage
    listing wins on ("2009 Saturn Outlook ABS pump"), so a site with Shopify's 255
    characters to spend should be spending them on the names themselves.
    """

    MODELS = (("GMC", "Acadia"), ("Saturn", "Outlook"), ("Buick", "Enclave"),
              ("Chevrolet", "Traverse"), ("Chevrolet", "Malibu"), ("Buick", "Lacrosse"),
              ("Saturn", "Aura"), ("Pontiac", "G6"))

    def _wide(self):
        apps = [Application(year_start=2008, year_end=2011, note="")]
        return part(
            part_type="Anti-lock Brake Pts", side=None,
            fitment=[Fitment(year_start=2008, year_end=2011, make=make, model=model,
                             applications=apps) for make, model in self.MODELS],
        )

    def _store(self, cap):
        return StoreProfile(vendor="Test Yard", city="Testville, TX",
                            catalog=CatalogProfile(title_max_models=cap))

    def test_a_capped_title_hides_vehicles_behind_a_count(self):
        title = seo.build_title(self._wide(), self._store(4), compact=True)
        self.assertIn("and 4 more", title)
        self.assertNotIn("Pontiac G6", title)

    def test_an_uncapped_title_names_them_instead(self):
        title = seo.build_title(self._wide(), self._store(0), compact=True)
        self.assertNotIn("more", title)
        for make, model in self.MODELS:
            self.assertIn(model, title, title)

    def test_an_uncapped_title_still_respects_shopifys_limit(self):
        title = seo.build_title(self._wide(), self._store(0), compact=True)
        self.assertLessEqual(len(title), seo.TITLE_MAX)

    def test_the_marketplace_budget_still_binds_when_the_model_cap_is_lifted(self):
        # Lifting the cap is a Shopify decision; it must not push an 80-character
        # marketplace title over its own budget.
        title, dropped = seo.fit_title(self._wide(), self._store(0), limit=80,
                                       compact=True)
        self.assertLessEqual(len(title), 80)
        self.assertNotIn("truncated", dropped, title)
        self.assertIn("Module", title)

    def test_lifting_the_cap_leaves_a_single_vehicle_title_untouched(self):
        self.assertEqual(seo.build_title(part(), self._store(0)),
                         seo.build_title(part(), self._store(4)))


class AQualifierIsNeverLeftOnAConnector(unittest.TestCase):
    """A qualifier filtered down to "and" is not a qualifier any more.

    Title qualifiers are filtered against the words the title has already used. When a
    catalogue note qualifies a switch "w/ mirror and lock" and the part type already says
    both Mirror and Lock, every meaningful word goes and the connector is left holding the
    end of the title: "Front Door Window Master Switch Mirror And".
    """

    def _switch(self, note):
        return part(
            part_type="Front Door Switch", side="Left", make="Cadillac", model="CTS",
            fitment=[Fitment(year_start=2003, year_end=2007, make="Cadillac", model="CTS",
                             applications=[Application(year_start=2003, year_end=2007,
                                                       note=note)])],
        )

    def test_a_title_never_ends_on_a_connector(self):
        title = seo.build_title(self._switch("w/ mirror and lock"), STORE, compact=True)
        self.assertFalse(title.rstrip().lower().endswith(" and"), title)
        self.assertFalse(title.rstrip().lower().endswith(" with"), title)

    def test_a_surviving_qualifier_word_is_still_carried(self):
        title = seo.build_title(self._switch("w/ memory and lock"), STORE, compact=True)
        self.assertIn("Memory", title)
        self.assertFalse(title.rstrip().lower().endswith(" and"), title)

    def test_the_trim_only_touches_the_ends(self):
        self.assertEqual(seo._trim_connectors(["and", "Heated", "and", "Memory", "with"]),
                         ["Heated", "and", "Memory"])
        self.assertEqual(seo._trim_connectors(["and", "with"]), [])


class CatalogueAsidesInAModelName(unittest.TestCase):
    """The model column annotates itself in brackets, and casing must survive them.

    "SAFARI (GMC)" and "BLAZER/JIMMY (full size)" are the catalogue disambiguating a model
    name that two makes share. Casing the bracketed token whole upper-cased the bracket and
    lower-cased the word inside it, so a title carried "gmc" and "full Size" in the middle
    of otherwise clean vehicle names. Capping the model count hid it; spending the whole
    title budget puts it on the page.
    """

    def test_a_bracketed_token_keeps_its_own_casing(self):
        self.assertEqual(seo._fix_token("(GMC)"), "(GMC)")
        self.assertEqual(seo._fix_token("(full"), "(Full")
        self.assertEqual(seo._fix_token("size)"), "Size)")

    def test_length_is_measured_without_the_brackets(self):
        # "(GT)" is five characters but a three-letter acronym, and the acronym rule is
        # what keeps a trim from being written "Gt".
        self.assertEqual(seo._fix_token("(GT)"), "(GT)")

    def test_a_model_name_carrying_its_make_says_it_once(self):
        grouped = seo.group_model_labels(["GMC Safari gmc", "GMC Sierra 1500"])
        self.assertEqual(grouped, "GMC Safari Sierra 1500")

    def test_the_make_is_still_said_once_per_group(self):
        grouped = seo.group_model_labels(
            ["Chevrolet Silverado 1500", "Chevrolet Silverado 2500", "GMC Sierra 1500"])
        self.assertEqual(grouped, "Chevrolet Silverado 1500 2500 GMC Sierra 1500")


class GradeAndMileageInATitle(unittest.TestCase):
    """Two facts the yard recorded, stated only where they mean something."""

    PROFILE = dict(title_grade="{grade} Grade",
                   title_mileage_part_types=("engine assembly", "transmiss,transaxle"),
                   title_mileage_max=200_000)

    def _store(self, **over):
        return StoreProfile(vendor="Test Yard", city="Testville, TX",
                            catalog=CatalogProfile(**{**self.PROFILE, **over}))

    def _engine(self, **kw):
        base = dict(part_type="ENGINE ASSEMBLY", side=None, make="Nissan", model="Altima",
                    year=2012, grade="A", mileage=142_684, description="2.5L, VIN A, TESTED")
        base.update(kw)
        return part(**base)

    def test_grade_and_mileage_are_both_stated(self):
        title = seo.build_title(self._engine(), self._store(), compact=True)
        self.assertIn("A Grade", title)
        self.assertIn("142K Miles", title)
        self.assertIn("Tested", title)

    def test_mileage_above_the_ceiling_is_withheld(self):
        title = seo.build_title(self._engine(mileage=243_191), self._store(), compact=True)
        self.assertNotIn("Miles", title)
        self.assertIn("A Grade", title)

    def test_the_ceiling_is_inclusive(self):
        self.assertEqual(seo.title_mileage(self._engine(mileage=200_000), self._store()),
                         "200K Miles")
        self.assertEqual(seo.title_mileage(self._engine(mileage=200_001), self._store()), "")

    def test_thousands_are_floored_never_rounded_up(self):
        # Rounding up would overstate the odometer, which is the one direction a seller
        # must not err in.
        self.assertEqual(seo.title_mileage(self._engine(mileage=142_999), self._store()),
                         "142K Miles")

    def test_a_part_type_mileage_says_nothing_about_omits_it(self):
        glass = self._engine(part_type="DOOR GLASS, FRONT", side="Left")
        title = seo.build_title(glass, self._store(), compact=True)
        self.assertNotIn("Miles", title)

    def test_a_sub_thousand_reading_is_not_published_as_zero(self):
        self.assertEqual(seo.title_mileage(self._engine(mileage=400), self._store()), "")

    def test_both_are_off_until_the_site_asks_for_them(self):
        neutral = StoreProfile(vendor="Test Yard", city="Testville, TX")
        title = seo.build_title(self._engine(), neutral, compact=True)
        self.assertNotIn("Grade", title)
        self.assertNotIn("Miles", title)

    def test_a_title_carrying_neither_is_unchanged_by_the_feature(self):
        self.assertEqual(seo.build_title(part(), self._store()),
                         seo.build_title(part(), StoreProfile(vendor="Test Yard",
                                                              city="Testville, TX")))

    def test_the_marketplace_budget_drops_them_before_the_part_type(self):
        title, _ = seo.fit_title(self._engine(), self._store(), limit=80, compact=True)
        self.assertLessEqual(len(title), 80)
        self.assertIn("Engine", title)

    def test_neither_introduces_a_symbol(self):
        title = seo.build_title(self._engine(), self._store(), compact=True)
        self.assertFalse(BANNED_IN_TITLE & set(title), title)


class AReviewedTitleKeepsItsFacts(unittest.TestCase):
    """An override carries facts the database does not hold, so it is extended, not replaced.

    The engine titles reviewed through the listing portal state displacement, VIN code and
    cylinder configuration for parts whose own note in the yard system is empty — those
    facts came off the portal's detail page and cannot be rebuilt from the database. They
    are also deliberately narrower than fitment: one engine variant, not every model the
    interchange group covers. What they predate is the grade and the donor's mileage, and
    those are facts about this part rather than about the engine family.
    """

    TITLE = "2011-2017 Chevrolet Equinox 2.4L VIN K LEA Engine Motor Assembly OEM"

    def _store(self):
        return StoreProfile(vendor="Test Yard", city="Testville, TX",
                            catalog=CatalogProfile(
                                title_grade="{grade} Grade",
                                title_mileage_part_types=("engine assembly",)))

    def _engine(self, **kw):
        base = dict(part_type="ENGINE ASSEMBLY", side=None, grade="A", mileage=193_722,
                    description=None)
        base.update(kw)
        return part(**base)

    def test_the_reviewed_wording_is_never_rewritten(self):
        out = seo.extend_override_title(self.TITLE, self._engine(), self._store())
        self.assertTrue(out.startswith(self.TITLE), out)

    def test_it_collects_the_facts_it_predates(self):
        out = seo.extend_override_title(self.TITLE, self._engine(), self._store())
        self.assertIn("A Grade", out)
        self.assertIn("193K Miles", out)

    def test_mileage_over_the_ceiling_is_still_withheld(self):
        out = seo.extend_override_title(self.TITLE, self._engine(mileage=253_889),
                                        self._store())
        self.assertNotIn("Miles", out)

    def test_nothing_the_title_already_says_is_repeated(self):
        already = self.TITLE + " A Grade"
        out = seo.extend_override_title(already, self._engine(mileage=None), self._store())
        self.assertEqual(out, already)

    def test_a_reviewed_title_is_not_truncated_for_an_addition(self):
        long_title = "X" * (seo.TITLE_MAX - 3)
        out = seo.extend_override_title(long_title, self._engine(), self._store())
        self.assertEqual(out, long_title)

    def test_a_site_asking_for_neither_leaves_the_title_alone(self):
        neutral = StoreProfile(vendor="Test Yard", city="Testville, TX")
        self.assertEqual(seo.extend_override_title(self.TITLE, self._engine(), neutral),
                         self.TITLE)


class AnOptionCodeFromThePartsOwnNote(unittest.TestCase):
    """"opt LFW" is the manufacturer's code for the drivetrain, and buyers search it bare."""

    def _spec(self, note):
        return seo.part_spec(part(part_type="ENGINE ASSEMBLY", description=note))

    def test_the_code_is_carried_into_the_spec(self):
        self.assertIn("Opt LFW", self._spec("3.0L (VIN 5, 8th digit, opt LFW)"))
        self.assertIn("Opt LZE", self._spec("No Oil Filler 3.5 L VIN K Opt LZE"))

    def test_ordinary_prose_is_not_read_as_a_code(self):
        self.assertEqual(self._spec("optional equipment included"), [])
        self.assertEqual(self._spec("opt lfw"), [])

    def test_it_sits_beside_the_other_engine_facts(self):
        self.assertEqual(self._spec("2.2L (VIN W, 8th digit, opt LE8)"),
                         ["2.2L", "VIN W", "Opt LE8"])


class AVinCodeIsNotAStrayInitial(unittest.TestCase):
    """"VIN" is a label for the character after it, and that character is one letter.

    The qualifier cleaner drops single letters because a truncated catalogue note reads
    "Driver s". But in "2.4L (VIN B, 8th digit)" the single letter is the engine code — the
    most useful character in the phrase — so dropping it left the label naming nothing and
    published "Modulator Assembly 2.4L VIN 8th Digit" and "Generator Gasoline 1.0L VIN".
    """

    def phrase(self, text):
        return seo._title_phrase(seo.seo_clean(text), False)

    def test_the_code_after_vin_survives(self):
        self.assertEqual(self.phrase("2.4L (VIN B, 8th digit)"), "2.4L VIN B")
        self.assertEqual(self.phrase("VIN K (8th digit)"), "VIN K")

    def test_a_two_character_code_survives(self):
        self.assertEqual(self.phrase("VIN FP 7th and 8th digit"), "VIN FP")

    def test_a_numeric_code_survives(self):
        self.assertEqual(self.phrase("VIN 1 4th digit"), "VIN 1")
        self.assertEqual(self.phrase("VIN 2 11th digit"), "VIN 2")

    def test_a_label_naming_no_code_is_dropped(self):
        # A trailing "VIN" says less than no label at all.
        self.assertEqual(self.phrase("1.0L (VIN, 8th digit)"), "1.0L")
        self.assertEqual(self.phrase("2.4L VIN 8th digit"), "2.4L")

    def test_the_position_alone_never_reaches_a_title(self):
        self.assertEqual(self.phrase("4th digit"), "")
        self.assertEqual(self.phrase("7th and 8th digit"), "")

    def test_an_ordinary_stray_initial_is_still_dropped(self):
        self.assertEqual(self.phrase("Driver s"), "Driver")

    def test_a_title_never_ends_on_a_bare_vin(self):
        p = part(
            part_type="Alternator", side=None, make="Ford", model="Focus",
            fitment=[Fitment(year_start=2015, year_end=2018, make="Ford", model="Focus",
                             applications=[Application(year_start=2015, year_end=2018,
                                                       note="gasoline; 1.0L (VIN, 8th digit)")])],
        )
        title = seo.build_title(p, STORE, compact=True)
        self.assertFalse(title.rstrip().upper().endswith("VIN"), title)
        self.assertNotIn("Digit", title)
