"""Comparable research that works for any part type, not one module per part type.

The thing being tested is that the *renderer's* decomposition of a part is enough to both
search for comparables and judge them — because if it is, a new part type needs no new
code, and every part type judges a match the same way.
"""

import unittest
from decimal import Decimal

from coreyard.config import StoreProfile
from coreyard.ebay import comps
from coreyard.models import Part


STORE = StoreProfile(vendor="Yard", city="Springfield")


def part(**kwargs):
    values = dict(part_type="TAIL LAMP", part_type_code=166, make="Ford", model="F150",
                  year=2012, price=Decimal("89.00"), quantity=1)
    values.update(kwargs)
    return Part(r_number=values.pop("r_number", "51"), **values)


def candidate(title, price=70.0, item="1"):
    return {"item": item, "title": title, "price": price}


class Queries(unittest.TestCase):
    def test_the_query_is_built_from_the_parts_own_facts(self):
        query = comps.query_for(part(), STORE)
        for token in ("2012", "Ford", "F150"):
            self.assertIn(token, query)

    def test_the_query_names_the_part_type_in_shopper_words(self):
        """The yard calls it "TAIL LAMP"; a buyer searches "tail light"."""
        self.assertIn("Light", comps.query_for(part(), STORE))

    def test_no_token_is_repeated(self):
        tokens = comps.query_for(part(model="Ford F150"), STORE).lower().split()
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_the_broad_query_drops_the_spec(self):
        item = part(part_type="ENGINE ASSEMBLY", part_type_code=300,
                    description="5.0L (VIN F, 8th digit),8 cyl")
        self.assertIn("5.0L", comps.query_for(item, STORE))
        self.assertNotIn("5.0L", comps.query_for(item, STORE, broad=True))

    def test_a_part_type_with_no_vehicle_still_produces_a_query(self):
        self.assertTrue(comps.query_for(part(make=None, model=None), STORE).strip())


class Matching(unittest.TestCase):
    def keep(self, title, **kwargs):
        return comps.score(part(**kwargs), STORE, candidate(title)) is not None

    def test_the_same_part_for_the_same_vehicle_is_a_comparable(self):
        self.assertTrue(self.keep("2012 Ford F150 Tail Light Lamp Right OEM"))

    def test_a_different_kind_of_part_is_rejected(self):
        """A tail lamp priced against headlamps is a wrong number, not a rough one."""
        self.assertFalse(self.keep("2012 Ford F150 Headlight Assembly"))

    def test_a_different_vehicle_is_rejected(self):
        self.assertFalse(self.keep("2004 Honda Civic Tail Light Lamp"))

    def test_a_non_overlapping_year_is_rejected(self):
        self.assertFalse(self.keep("1998 Ford F150 Tail Light Lamp"))

    def test_a_piece_of_the_part_is_never_a_comparable(self):
        for title in ("Tail Light Lamp Repair Kit", "Tail Light Lamp Bulb Socket",
                      "Tail Light Lamp Lens Only", "Ford F150 Repair Manual"):
            self.assertFalse(self.keep(title), title)

    def test_prices_outside_the_plausible_band_are_rejected(self):
        item = part()
        for price in (1.0, 99_000.0):
            self.assertIsNone(
                comps.score(item, STORE, candidate("2012 Ford F150 Tail Light", price)))

    def test_a_candidate_stating_no_year_is_still_usable(self):
        """Plenty of real listings omit the year; that is weak evidence, not wrong."""
        self.assertTrue(self.keep("Ford F150 Tail Light Lamp Right OEM"))


class SpecRules(unittest.TestCase):
    """Displacement is identity for an engine and a detail for most other parts."""

    def engine(self):
        return part(part_type="ENGINE ASSEMBLY", part_type_code=300,
                    description="5.0L (VIN F, 8th digit),8 cyl", price=Decimal("1500"))

    def test_an_engine_of_a_different_displacement_is_rejected(self):
        found = comps.score(self.engine(), STORE,
                            candidate("2012 Ford F150 3.5L Engine Motor", 1500.0))
        self.assertIsNone(found)

    def test_an_engine_of_the_same_displacement_is_kept(self):
        found = comps.score(self.engine(), STORE,
                            candidate("2012 Ford F150 5.0L Engine Motor", 1500.0))
        self.assertIsNotNone(found)

    def test_an_engine_stating_no_displacement_is_weak_not_wrong(self):
        found = comps.score(self.engine(), STORE,
                            candidate("2012 Ford F150 Engine Motor Assembly", 1500.0))
        self.assertIsNotNone(found)

    def test_a_part_type_without_the_strict_rule_keeps_a_spec_mismatch(self):
        """Rejecting on spec for every type would throw away whole comparable sets."""
        self.assertFalse(comps.rule_for(part()).spec_required)

    def test_every_part_type_has_a_rule_even_without_an_entry(self):
        self.assertIs(comps.rule_for(part(part_type_code=99999)), comps.DEFAULT_RULE)


class Pricing(unittest.TestCase):
    def test_the_price_sits_below_the_middle_of_the_comparables(self):
        item = part()
        found = [{"price": p, "title": "t", "score": 60}
                 for p in (60.0, 70.0, 80.0, 90.0, 100.0)]
        price = comps.price_for(item, found)
        self.assertLess(price, Decimal("80"))
        self.assertGreater(price, comps.floor_for(item))

    def test_no_comparables_means_the_floor_not_a_guess(self):
        item = part()
        self.assertEqual(comps.price_for(item, []), comps.floor_for(item))

    def test_the_floor_is_anchored_to_the_yard_price(self):
        """One number every part type has, instead of a 143-row table kept by hand."""
        self.assertEqual(comps.floor_for(part(price=Decimal("100.00"))),
                         Decimal("25.00"))

    def test_an_unpriced_part_still_has_a_floor(self):
        self.assertGreater(comps.floor_for(part(price=None)), 0)

    def test_a_market_below_the_floor_cannot_give_the_part_away(self):
        item = part(price=Decimal("400.00"))          # floor 100.00
        found = [{"price": 20.0, "title": "t", "score": 60}] * 5
        self.assertEqual(comps.price_for(item, found), comps.floor_for(item))

    def test_prices_land_on_a_charm_ending(self):
        item = part(price=Decimal("10.00"))
        found = [{"price": p, "title": "t", "score": 60}
                 for p in (60.0, 70.0, 80.0, 90.0, 100.0)]
        self.assertTrue(str(comps.price_for(item, found)).endswith(".99"))

    def test_confidence_follows_the_evidence(self):
        self.assertEqual(comps.confidence(0), "none")
        self.assertEqual(comps.confidence(1), "low")
        self.assertEqual(comps.confidence(3), "medium")
        self.assertEqual(comps.confidence(7), "high")


class Selection(unittest.TestCase):
    def test_the_same_listing_twice_counts_once(self):
        found = comps.select(part(), STORE,
                             [candidate("2012 Ford F150 Tail Light Lamp OEM")] * 5)
        self.assertEqual(len(found), 1)

    def test_results_come_back_strongest_first(self):
        found = comps.select(part(), STORE, [
            candidate("Ford F150 Tail Light Lamp", 60.0, "a"),
            candidate("2012 Ford F150 Tail Light Lamp OEM", 70.0, "b"),
        ])
        self.assertEqual(found[0]["item"], "b", "the fuller match ranks first")

    def test_nothing_usable_yields_an_empty_set_not_a_bad_one(self):
        found = comps.select(part(), STORE, [candidate("2004 Honda Civic Bumper", 50.0)])
        self.assertEqual(found, [])


class RecordShape(unittest.TestCase):
    def test_a_record_carries_its_evidence(self):
        item = part()
        found = comps.select(item, STORE, [
            candidate("2012 Ford F150 Tail Light Lamp OEM", 70.0, "a"),
            candidate("2012 Ford F150 Tail Light Lamp Left", 85.0, "b"),
        ])
        record = comps.Research(
            r_number="51", query="q", comps=found,
            price=comps.price_for(item, found), confidence=comps.confidence(len(found)),
        ).as_record()
        self.assertEqual(record["comp_count"], 2)
        self.assertIn("$70.00", record["evidence"])
        self.assertTrue(record["suggested_price"])


if __name__ == "__main__":
    unittest.main()
