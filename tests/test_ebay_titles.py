"""One title generator, two storefronts.

The point of these tests is that there is nothing here to test twice. A marketplace title
is the *same* title the Shopify catalogue publishes, composed by the same renderer from the
same part, differing only in how many characters the destination allows. What is asserted is
that the shared budget behaves, that the join to the part is refused rather than guessed at,
and that the eBay-specific rules are about the destination and not about the part.
"""

import unittest
from decimal import Decimal

from coreyard.config import StoreProfile
from coreyard.ebay import titles, util
from coreyard.models import Part
from coreyard.transform import seo


def part(r_number="51", **kwargs):
    values = dict(part_type="Engine Assembly", make="Ford", model="F150",
                  year=2012, price=Decimal("999.00"), quantity=1)
    values.update(kwargs)
    return Part(r_number=r_number, **values)


STORE = StoreProfile(vendor="Yard", city="Springfield")


class Length(unittest.TestCase):
    """The portal escapes before eBay counts, so & and " cost more than one character."""

    def test_plain_text_costs_its_own_length(self):
        self.assertEqual(util.title_length("Ford Engine"), 11)

    def test_ampersand_and_quote_are_charged_at_their_escaped_width(self):
        self.assertEqual(util.title_length("A&B"), 3 + 4)
        self.assertEqual(util.title_length('A"B'), 3 + 5)


class SharedBudget(unittest.TestCase):
    """`compose_title` is the one place a title is assembled, for either storefront."""

    SEGMENTS = [("years", "2012", 1), ("models", "Ford F150", 1),
                ("part_type", "Engine Assembly", 1), ("side", "Left Driver", 2),
                ("spec", "5.0L V8", 3), ("qualifiers", "With Oil Cooler", 4),
                ("oem", "OEM", 5)]

    def test_a_generous_budget_drops_nothing(self):
        title, dropped = seo.compose_title(self.SEGMENTS, limit=255)
        self.assertEqual(dropped, [])
        self.assertTrue(title.startswith("2012 Ford F150"))

    def test_a_tight_budget_drops_the_most_expendable_first(self):
        _, dropped = seo.compose_title(self.SEGMENTS, limit=45)
        self.assertEqual(dropped[0], "oem", "the last-ranked segment goes first")

    def test_the_load_bearing_facts_are_never_dropped(self):
        """A title without the year, vehicle or part type describes nothing at all."""
        title, dropped = seo.compose_title(self.SEGMENTS, limit=20)
        for name in ("years", "models", "part_type"):
            self.assertNotIn(name, dropped)
        self.assertIn("2012", title)

    def test_an_impossible_budget_truncates_rather_than_losing_the_subject(self):
        title, _ = seo.compose_title(self.SEGMENTS, limit=20)
        self.assertLessEqual(len(title), 20)

    def test_the_destination_decides_how_characters_are_counted(self):
        segments = [("models", "Smith & Sons", 1)]
        plain, _ = seo.compose_title(segments, limit=14)
        tight, _ = seo.compose_title(segments, limit=14, measure=util.title_length)
        self.assertEqual(plain, "Smith & Sons")
        self.assertLess(len(tight), len(plain), "escaping makes it overflow")


class OneGenerator(unittest.TestCase):
    def test_the_marketplace_title_is_the_catalogue_title(self):
        """Where the budget does not bind — as it does not for almost every real part —
        the two storefronts publish the identical string."""
        item = part()
        shopify = seo.build_title(item, STORE)
        marketplace, dropped = titles.render_title(item, STORE)
        self.assertLessEqual(util.title_length(shopify), util.TITLE_MAX)
        self.assertEqual(marketplace, shopify)
        self.assertEqual(dropped, [])

    def test_a_tighter_budget_shortens_the_same_title_it_does_not_rebuild_it(self):
        item = part()
        full = seo.build_title(item, STORE)
        short, dropped = titles.render_title(item, STORE, limit=30)
        self.assertLessEqual(util.title_length(short), 30)
        if dropped:
            self.assertTrue(full.startswith(short.split()[0]))


ROW = {"listing_id": "1", "interchange_number": "300-A", "title": "OLD PORTAL TITLE"}
DETAIL = {"1": {"r_number": "51"}}


class Decisions(unittest.TestCase):
    def test_a_listing_is_titled_from_the_part_it_actually_is(self):
        accepted, held = titles.decisions([ROW], DETAIL, {"51": part()}, STORE)
        self.assertFalse(held, held)
        self.assertEqual(accepted[0]["r_number"], "51")
        self.assertIn("F150", accepted[0]["new_title"])

    def test_a_listing_with_no_r_number_is_held_not_guessed_at(self):
        accepted, held = titles.decisions([ROW], {}, {"51": part()}, STORE)
        self.assertEqual(accepted, [])
        self.assertIn("R#", held[0]["reason"])

    def test_a_listing_whose_part_the_yard_does_not_have_is_held(self):
        """This is exactly when a wrong title is most likely, so nothing is written."""
        accepted, held = titles.decisions([ROW], DETAIL, {}, STORE)
        self.assertEqual(accepted, [])
        self.assertIn("no yard part", held[0]["reason"])

    def test_a_title_that_already_matches_is_not_a_write(self):
        item = part()
        row = {**ROW, "title": seo.build_title(item, STORE)}
        accepted, held = titles.decisions([row], DETAIL, {"51": item}, STORE)
        self.assertEqual(accepted, [])
        self.assertEqual(held[0]["reason"], "unchanged")

    def test_two_interchange_groups_may_not_share_one_title(self):
        rows = [ROW, {"listing_id": "2", "interchange_number": "300-B",
                      "title": "OTHER"}]
        parts = {"51": part(), "52": part(r_number="52")}
        accepted, held = titles.decisions(
            rows, {"1": {"r_number": "51"}, "2": {"r_number": "52"}}, parts, STORE)
        self.assertEqual(accepted, [])
        self.assertTrue(all("share one title" in h["reason"] for h in held), held)

    def test_two_listings_of_one_interchange_group_may_share_a_title(self):
        """Same interchange means the same part; one name for both is correct."""
        rows = [ROW, {"listing_id": "2", "interchange_number": "300-A",
                      "title": "OTHER"}]
        parts = {"51": part(), "52": part(r_number="52")}
        accepted, held = titles.decisions(
            rows, {"1": {"r_number": "51"}, "2": {"r_number": "52"}}, parts, STORE)
        self.assertEqual(len(accepted), 2, held)


class PortalWrites(unittest.TestCase):
    def test_listings_that_resolved_to_one_title_become_one_write(self):
        accepted = [
            {"listing_id": "1", "interchange": "300-A", "new_title": "T",
             "old_title": "a"},
            {"listing_id": "2", "interchange": "300-A", "new_title": "T",
             "old_title": "b"},
            {"listing_id": "3", "interchange": "300-B", "new_title": "U",
             "old_title": "c"},
        ]
        grouped = titles.group_for_portal(accepted)
        self.assertEqual(len(grouped), 2)
        self.assertEqual(grouped[0]["listing_ids"], ["1", "2"])
        self.assertEqual(sorted(grouped[0]["old_titles"]), ["a", "b"])


class DestinationRules(unittest.TestCase):
    """These guard the marketplace, never the wording — the renderer owns the wording."""

    def test_escaping_characters_are_refused(self):
        ok, reason = titles.validate("Wheel & Rim", "old")
        self.assertFalse(ok)
        self.assertIn("&", reason)

    def test_an_over_budget_title_is_refused(self):
        ok, reason = titles.validate("x" * 90, "old")
        self.assertFalse(ok)
        self.assertIn("too long", reason)

    def test_an_empty_title_is_refused(self):
        self.assertFalse(titles.validate("  ", "old")[0])


class Idempotence(unittest.TestCase):
    """The bug that motivated this: a builder that re-read its own output changed it."""

    def test_titling_a_listing_twice_is_a_no_op(self):
        item = part()
        accepted, _ = titles.decisions([ROW], DETAIL, {"51": item}, STORE)
        written = accepted[0]["new_title"]
        again, held = titles.decisions(
            [{**ROW, "title": written}], DETAIL, {"51": item}, STORE)
        self.assertEqual(again, [], "a second run must propose nothing")
        self.assertEqual(held[0]["reason"], "unchanged")

    def test_the_title_never_depends_on_what_the_portal_currently_says(self):
        item = part()
        first, _ = titles.decisions([ROW], DETAIL, {"51": item}, STORE)
        other = [{**ROW, "title": "COMPLETELY DIFFERENT TEXT"}]
        second, _ = titles.decisions(other, DETAIL, {"51": item}, STORE)
        self.assertEqual(first[0]["new_title"], second[0]["new_title"])


if __name__ == "__main__":
    unittest.main()
