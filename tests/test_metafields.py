"""Structured product data: grade, mileage, condition and fitment as metafields.

The theme renders a fitment table and a spec row from these. That makes them catalogue
content, not decoration — so they are part of the rendered product and therefore of the
fingerprint, and a value that disappears from the yard has to disappear from the storefront
rather than sitting there being wrong.
"""

import json
import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.profile import CatalogProfile
from coreyard.sink.shopify_api import product_set_input
from coreyard.sink.shopify_write import ShopifyPublisher
from coreyard.transform.render import Metafield, build_metafields, fitment_rows, render
from coreyard.yms.interchange import Application, Fitment

STORE = StoreProfile(vendor="Test Yard", city="Testville, TX",
                     catalog=CatalogProfile(metafield_namespace="abm"))


def part(**kw) -> Part:
    base = dict(r_number="51", part_type="Alternator", price=Decimal("120.00"),
                year=2014, make="FORD", model="F-150 PICKUP")
    base.update(kw)
    return Part(**base)


def fitment(make="FORD", model="F-150", y1=2011, y2=2014, notes=()) -> Fitment:
    return Fitment(make=make, model=model, year_start=y1, year_end=y2,
                   applications=[Application(y1, y2, note) for note in notes])


def by_key(product) -> dict:
    return {m.key: m for m in product.metafields}


class Values(unittest.TestCase):
    def test_grade_is_published_when_the_yard_recorded_one(self):
        fields = by_key(render(part(grade="A"), [], STORE))
        self.assertEqual(fields["grade"].value, "A")
        self.assertEqual(fields["grade"].type, "single_line_text_field")
        self.assertEqual(fields["grade"].namespace, "abm")

    def test_mileage_is_published_as_an_integer(self):
        fields = by_key(render(part(mileage=88000), [], STORE))
        self.assertEqual(fields["mileage"].value, "88000")
        self.assertEqual(fields["mileage"].type, "number_integer")

    def test_condition_comes_from_the_site_profile(self):
        fields = by_key(render(part(), [], STORE))
        self.assertEqual(fields["condition"].value, "Used")
        tested = StoreProfile(vendor="Test Yard",
                              catalog=CatalogProfile(metafield_namespace="abm",
                                                     condition="Used, tested"))
        self.assertEqual(by_key(render(part(), [], tested))["condition"].value,
                         "Used, tested")

    def test_a_grade_makes_the_condition_specific(self):
        self.assertEqual(by_key(render(part(grade="B"), [], STORE))["condition"].value,
                         "Grade B")

    def test_absent_values_are_left_out_rather_than_published_empty(self):
        fields = by_key(render(part(grade=None, mileage=None), [], STORE))
        self.assertNotIn("grade", fields)
        self.assertNotIn("mileage", fields)

    def test_a_zero_mileage_is_treated_as_no_reading(self):
        self.assertNotIn("mileage", by_key(render(part(mileage=0), [], STORE)))

    def test_the_namespace_is_the_sites_choice(self):
        neutral = StoreProfile(vendor="Test Yard")
        self.assertEqual(neutral.catalog.metafield_namespace, "coreyard")
        self.assertTrue(all(m.namespace == "coreyard"
                            for m in render(part(grade="A"), [], neutral).metafields))


class StructuredFitment(unittest.TestCase):
    def test_each_catalogue_application_becomes_a_row(self):
        p = part(fitment=[fitment(), fitment("LINCOLN", "Mark LT", 2006, 2008)])
        rows = fitment_rows(p, STORE)
        self.assertEqual([r["make"] for r in rows], ["Ford", "Lincoln"])
        self.assertEqual(rows[0]["years"], "2011-2014")
        self.assertEqual(rows[0]["model"], "F-150")

    def test_the_theme_contract_keys_are_all_present(self):
        rows = fitment_rows(part(fitment=[fitment()]), STORE)
        self.assertEqual(set(rows[0]), {"years", "make", "model", "note", "label"})

    def test_a_single_year_reads_as_one_year(self):
        rows = fitment_rows(part(fitment=[fitment(y1=2014, y2=2014)]), STORE)
        self.assertEqual(rows[0]["years"], "2014")

    def test_qualifiers_reach_the_note(self):
        rows = fitment_rows(part(fitment=[fitment(notes=("4x4", "California"))]), STORE)
        self.assertIn("4x4", rows[0]["note"])

    def test_the_label_is_exactly_the_vehicle_tag(self):
        """The theme links to a tag-filtered collection; it must not have to guess it."""
        p = part(fitment=[fitment()])
        product = render(p, [], STORE)
        row = json.loads(by_key(product)["fitment"].value)[0]
        self.assertIn(row["label"], product.tags)

    def test_a_part_with_no_catalogue_fitment_still_fits_its_donor(self):
        rows = fitment_rows(part(), STORE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["make"], "Ford")
        self.assertEqual(rows[0]["model"], "F-150")
        self.assertEqual(rows[0]["years"], "2014")

    def test_a_part_with_no_vehicle_at_all_has_no_fitment(self):
        rows = fitment_rows(part(year=None, make=None, model=None), STORE)
        self.assertEqual(rows, [])
        self.assertNotIn("fitment", by_key(render(part(year=None, make=None, model=None),
                                                  [], STORE)))

    def test_the_value_is_json_of_the_documented_shape(self):
        field = by_key(render(part(fitment=[fitment()]), [], STORE))["fitment"]
        self.assertEqual(field.type, "json")
        rows = json.loads(field.value)
        self.assertIsInstance(rows, list)
        self.assertEqual(rows[0]["make"], "Ford")

    def test_a_doubled_make_is_not_repeated_in_the_row(self):
        """Make "DODGE TRUCK" with model "DODGE 1500" must not read "Dodge Dodge 1500"."""
        rows = fitment_rows(part(fitment=[fitment("DODGE TRUCK", "DODGE 1500")]), STORE)
        self.assertEqual(rows[0]["label"], "Dodge 1500")

    def test_a_make_prefix_is_removed_from_the_separate_model_column(self):
        rows = fitment_rows(
            part(fitment=[fitment("LEXUS", "LEXUS ES350")]), STORE
        )
        self.assertEqual(rows[0]["make"], "Lexus")
        self.assertEqual(rows[0]["model"], "ES350")
        self.assertEqual(rows[0]["label"], "Lexus ES350")

    def test_identical_qualifier_notes_are_not_repeated_in_the_table(self):
        repeated = fitment(notes=("3.5L, VIN 1", "3.5L, VIN 1", "Federal emissions"))
        rows = fitment_rows(part(fitment=[repeated]), STORE)
        self.assertEqual(rows[0]["note"], "3.5L, VIN 1; Federal emissions")

    def test_a_split_row_keeps_the_years_each_qualifier_belongs_to(self):
        """Starter 604-00122: the Equinox took the 3.0L for 2010-2012 only.

        The row's span is the union, so the summary used to read "2008-2017 ... 3.6L; 3.0L"
        and a 2011 owner had no way to tell which of the two was theirs.
        """
        equinox = Fitment(make="CHEVROLET TRUCK", model="EQUINOX",
                          year_start=2008, year_end=2017,
                          applications=[Application(2008, 2009, "3.6L"),
                                        Application(2010, 2012, "3.0L"),
                                        Application(2013, 2017, "3.6L")])
        rows = fitment_rows(part(fitment=[equinox]), STORE)
        self.assertEqual(rows[0]["years"], "2008-2017")
        self.assertEqual(rows[0]["note"],
                         "2008-2009 — 3.6L; 2010-2012 — 3.0L; 2013-2017 — 3.6L")

    def test_a_qualifier_repeated_in_another_year_run_is_kept(self):
        """Deduping on the note alone would delete the 2013-2017 3.6L run entirely."""
        rows = fitment_rows(part(fitment=[
            Fitment(make="FORD", model="F-150", year_start=2011, year_end=2016,
                    applications=[Application(2011, 2012, "3.5L"),
                                  Application(2015, 2016, "3.5L")])]), STORE)
        self.assertEqual(rows[0]["note"], "2011-2012 — 3.5L; 2015-2016 — 3.5L")

    def test_an_unrestricted_year_run_is_stated_on_a_split_row(self):
        """A 2012 Impala fits with no qualifier at all; listing only the qualified runs
        left every note naming a restriction that owner could not meet."""
        rows = fitment_rows(part(fitment=[
            Fitment(make="CHEVROLET", model="IMPALA", year_start=2012, year_end=2019,
                    applications=[Application(2012, 2013, ""),
                                  Application(2014, 2016, "VIN W (4th digit, Limited)"),
                                  Application(2017, 2019, "3.6L")])]), STORE)
        self.assertTrue(rows[0]["note"].startswith("2012-2013; "))

    def test_a_row_that_is_not_split_states_qualifiers_without_years(self):
        """The years column already says 2011-2014; repeating it in every note is noise."""
        rows = fitment_rows(part(fitment=[fitment(notes=("4x4", "California"))]), STORE)
        self.assertEqual(rows[0]["note"], "4x4; California")

    def test_a_row_with_no_qualifiers_at_all_has_an_empty_note(self):
        rows = fitment_rows(part(fitment=[fitment(notes=("",))]), STORE)
        self.assertEqual(rows[0]["note"], "")

    def test_a_truncated_note_says_that_it_was_truncated(self):
        """A dropped year run reads as an excluded one, so the cut has to be visible."""
        many = Fitment(make="FORD", model="F-150", year_start=2000, year_end=2011,
                       applications=[Application(2000 + n, 2000 + n, f"{n}.0L")
                                     for n in range(12)])
        note = fitment_rows(part(fitment=[many]), STORE)[0]["note"]
        self.assertEqual(len(note.split("; ")), 9)
        self.assertTrue(note.endswith("; +4 more"))

    def test_placeholder_years_and_make_less_artifacts_do_not_reach_the_theme(self):
        p = part(fitment=[
            fitment("MAZDA", "3", 2014, 2023),
            fitment(None, "CX-", 1950, 1950),
            fitment(None, "Mazda CX-", 2030, 2030),
        ])
        rows = fitment_rows(p, STORE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["years"], "2014-2023")
        self.assertEqual(rows[0]["model"], "3")


class Serialization(unittest.TestCase):
    def test_metafields_reach_the_graphql_input(self):
        payload = product_set_input(render(part(grade="A", mileage=88000), [], STORE))
        keys = {m["key"]: m for m in payload["metafields"]}
        self.assertEqual(keys["grade"]["namespace"], "abm")
        self.assertEqual(keys["mileage"]["type"], "number_integer")
        self.assertEqual(set(keys), {"grade", "mileage", "condition", "fitment"})

    def test_every_entry_carries_the_four_fields_shopify_requires(self):
        payload = product_set_input(render(part(grade="A"), [], STORE))
        for entry in payload["metafields"]:
            self.assertEqual(set(entry), {"namespace", "key", "type", "value"})
            self.assertTrue(entry["value"])

    def test_a_product_with_nothing_structured_sends_no_metafields_key(self):
        bare = part(year=None, make=None, model=None, grade=None, mileage=None)
        neutral = StoreProfile(vendor="Test Yard",
                               catalog=CatalogProfile(metafield_namespace="abm",
                                                      condition=""))
        payload = product_set_input(render(bare, [], neutral))
        self.assertNotIn("metafields", payload)


class Fingerprint(unittest.TestCase):
    def test_a_changed_grade_moves_the_fingerprint(self):
        self.assertNotEqual(render(part(grade="A"), [], STORE).fingerprint(),
                            render(part(grade="B"), [], STORE).fingerprint())

    def test_a_changed_mileage_moves_the_fingerprint(self):
        self.assertNotEqual(render(part(mileage=88000), [], STORE).fingerprint(),
                            render(part(mileage=91000), [], STORE).fingerprint())

    def test_changed_fitment_moves_the_fingerprint(self):
        one = render(part(fitment=[fitment()]), [], STORE).fingerprint()
        two = render(part(fitment=[fitment(), fitment("LINCOLN", "Mark LT")]), [],
                     STORE).fingerprint()
        self.assertNotEqual(one, two)

    def test_a_grade_appearing_moves_the_fingerprint(self):
        self.assertNotEqual(render(part(), [], STORE).fingerprint(),
                            render(part(grade="A"), [], STORE).fingerprint())

    def test_the_namespace_is_part_of_it(self):
        other = StoreProfile(vendor="Test Yard", city="Testville, TX",
                             catalog=CatalogProfile(metafield_namespace="parts"))
        self.assertNotEqual(render(part(grade="A"), [], STORE).fingerprint(),
                            render(part(grade="A"), [], other).fingerprint())


class Staleness(unittest.TestCase):
    """A value that vanished from the yard must vanish from the storefront."""

    def _publisher(self):
        publisher = ShopifyPublisher.__new__(ShopifyPublisher)
        publisher.client = MagicMock()
        publisher.location = "gid://shopify/Location/1"
        publisher.status = "ACTIVE"
        publisher.retire_status = "ARCHIVED"
        publisher.require_images = False
        publisher.publications = []
        publisher.images = MagicMock()
        publisher.store = STORE
        publisher._staged_files = lambda p, alt_for: []
        publisher.client.mutate.return_value = {"product": {"id": "gid://shopify/Product/1"}}
        return publisher

    def _found(self, metafield_keys):
        return {"productByIdentifier": {
            "id": "gid://shopify/Product/1", "status": "ACTIVE", "tags": [],
            "media": {"nodes": [{"id": "gid://shopify/MediaImage/1"}]},
            "metafields": {"nodes": [{"id": f"gid://x/{k}", "key": k}
                                     for k in metafield_keys]},
        }}

    def _mutations(self, publisher):
        return [call.args[2] for call in publisher.client.mutate.call_args_list]

    def test_a_metafield_we_no_longer_generate_is_deleted(self):
        publisher = self._publisher()
        publisher.client.graphql.side_effect = [
            self._found(["grade", "mileage", "condition", "fitment"])]

        publisher.publish(part(grade=None, mileage=None))   # no grade, no mileage now

        self.assertIn("metafieldsDelete", self._mutations(publisher))
        deleted = next(c.args[1] for c in publisher.client.mutate.call_args_list
                       if c.args[2] == "metafieldsDelete")
        self.assertEqual({m["key"] for m in deleted["ids"]}, {"grade", "mileage"})
        self.assertTrue(all(m["namespace"] == "abm" for m in deleted["ids"]))

    def test_nothing_is_deleted_when_everything_is_still_generated(self):
        publisher = self._publisher()
        publisher.client.graphql.side_effect = [self._found(["grade", "condition",
                                                             "fitment"])]

        publisher.publish(part(grade="A"))

        self.assertNotIn("metafieldsDelete", self._mutations(publisher))

    def test_a_brand_new_product_deletes_nothing(self):
        publisher = self._publisher()
        publisher.client.graphql.side_effect = [{"productByIdentifier": None}]

        publisher.publish(part())

        self.assertNotIn("metafieldsDelete", self._mutations(publisher))

    def test_a_product_predating_metafields_is_handled(self):
        """An older payload has no metafields node at all."""
        publisher = self._publisher()
        publisher.client.graphql.side_effect = [{"productByIdentifier": {
            "id": "gid://shopify/Product/1", "status": "ACTIVE", "tags": [],
            "media": {"nodes": []}}}]

        publisher.publish(part())

        self.assertNotIn("metafieldsDelete", self._mutations(publisher))


class Neutrality(unittest.TestCase):
    def test_no_site_specific_namespace_in_the_generic_default(self):
        self.assertNotIn("abm", CatalogProfile().metafield_namespace)
        self.assertEqual(build_metafields(part(grade="A"), StoreProfile())[0].namespace,
                         "coreyard")

    def test_metafield_is_a_plain_value_object(self):
        field = Metafield("abm", "grade", "single_line_text_field", "A")
        self.assertEqual(field.qualified, "abm.grade")


if __name__ == "__main__":
    unittest.main()
