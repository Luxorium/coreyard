"""Site policy: neutral by default, configurable, and never leaking into the repository.

The point of the profile is that CoreYard should not put words in a seller's mouth. It does
not know whether a yard tests every part, so its defaults claim only what the data supports,
and a site that does test says so in its own file.
"""

import json
import os
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.profile import CatalogProfile, ProfileError, from_dict, load
from coreyard.transform import seo
from coreyard.transform.render import render

STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="90-day warranty")


def part(**kw) -> Part:
    base = dict(r_number="51", part_type="Alternator", price=Decimal("120.00"),
                year=2010, make="Honda", model="Civic")
    base.update(kw)
    return Part(**base)


def with_profile(profile: CatalogProfile) -> StoreProfile:
    return StoreProfile(vendor="Test Yard", city="Testville, TX",
                        warranty="90-day warranty", catalog=profile)


class NeutralDefaults(unittest.TestCase):
    def test_no_untested_claim_of_testing(self):
        body = seo.build_body_html(part(), STORE)
        meta = seo.meta_description(part(), STORE)
        for text in (body, meta):
            self.assertNotIn("tested", text.lower())
            self.assertNotIn("inspected", text.lower())

    def test_no_claim_of_genuineness_beyond_oem(self):
        self.assertNotIn("Genuine", seo.build_body_html(part(), STORE))

    def test_condition_states_the_grade_when_there_is_one(self):
        self.assertIn("<strong>Condition:</strong> Grade A",
                      seo.build_body_html(part(grade="A"), STORE))

    def test_condition_says_only_used_without_a_grade(self):
        self.assertIn("<strong>Condition:</strong> Used</li>",
                      seo.build_body_html(part(), STORE))

    def test_oem_stays_out_of_the_title_by_default(self):
        self.assertNotIn("OEM", seo.build_title(part(), STORE))

    def test_the_body_still_names_the_part_as_used_oem(self):
        """Factual and searchable: it is an OEM part and it is used."""
        self.assertIn("Used OEM", seo.build_body_html(part(), STORE))
        self.assertIn("Used OEM", seo.build_tags(part(), STORE))


class SitePolicy(unittest.TestCase):
    def test_a_site_can_say_it_tests_its_parts(self):
        store = with_profile(CatalogProfile(
            condition="Used, tested",
            body_lead="Genuine OEM {part_type}, removed and inspected.",
            availability="In stock and tested at {origin}."))
        body = seo.build_body_html(part(), store)
        self.assertIn("Genuine OEM", body)
        self.assertIn("<strong>Condition:</strong> Used, tested", body)
        self.assertIn("In stock and tested at Test Yard, Testville, TX.",
                      seo.meta_description(part(), store))

    def test_a_site_can_put_oem_in_the_title(self):
        store = with_profile(CatalogProfile(title_include_oem=True))
        self.assertIn("OEM", seo.build_title(part(), store))

    def test_the_part_type_table_can_be_extended(self):
        store = with_profile(CatalogProfile(part_types={"alternator": "Dynamo"}))
        self.assertEqual(seo.expand_part_type("Alternator", store), "Dynamo")
        self.assertEqual(render(part(), [], store).product_type, "Dynamo")

    def test_the_vendor_tag_can_be_switched_off(self):
        store = with_profile(CatalogProfile(tag_vendor=False))
        self.assertNotIn("Test Yard", seo.build_tags(part(), store))

    def test_extra_tags_are_site_supplied(self):
        store = with_profile(CatalogProfile(tags=("Recycled", "Green")))
        tags = seo.build_tags(part(), store)
        self.assertIn("Recycled", tags)
        self.assertIn("Green", tags)
        self.assertNotIn("Used OEM", tags)

    def test_how_many_models_a_title_lists_is_a_site_decision(self):
        store = with_profile(CatalogProfile(title_max_models=1))
        self.assertEqual(store.catalog.title_max_models, 1)

    def test_a_site_can_add_an_exact_part_type_disclosure(self):
        policy = CatalogProfile(part_type_notices={"air bag": "Qualified installation."})
        store = with_profile(policy)
        body = seo.build_body_html(part(part_type="AIR BAG"), store)
        self.assertIn("Safety disclosure:", body)
        self.assertIn("Qualified installation.", body)
        self.assertNotIn("Safety disclosure:",
                         seo.build_body_html(part(part_type="Air Bag Sensor"), store))

    def test_profile_text_cannot_inject_markup(self):
        """A profile supplies words; the renderer owns the HTML."""
        store = with_profile(CatalogProfile(
            body_lead="<script>alert(1)</script> {part_type}"))
        body = seo.build_body_html(part(), store)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)


class Loading(unittest.TestCase):
    def _write(self, data) -> Path:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "catalog-profile.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_no_path_means_the_neutral_defaults(self):
        self.assertEqual(load(None), CatalogProfile())

    def test_a_partial_file_keeps_every_other_default(self):
        profile = load(self._write({"condition": "Used, tested"}))
        self.assertEqual(profile.condition, "Used, tested")
        self.assertEqual(profile.title_include_oem, CatalogProfile().title_include_oem)

    def test_comments_are_allowed(self):
        self.assertEqual(load(self._write({"_comment": "notes"})), CatalogProfile())

    def test_an_unknown_key_is_an_error(self):
        """A misspelled key that silently did nothing would be a policy nobody applied."""
        with self.assertRaises(ProfileError):
            from_dict({"conditon": "Used, tested"})

    def test_an_unknown_audit_key_is_an_error(self):
        with self.assertRaises(ProfileError):
            from_dict({"audit": {"min_prices": 5}})

    def test_a_configured_but_missing_file_is_an_error(self):
        with self.assertRaises(ProfileError):
            load("/nonexistent/catalog-profile.json")

    def test_audit_thresholds_load(self):
        profile = load(self._write({"audit": {"min_price": 5, "min_tags": 4,
                                              "require_weight": False}}))
        self.assertEqual(profile.audit.min_price, 5)
        self.assertEqual(profile.audit.min_tags, 4)
        self.assertFalse(profile.audit.require_weight)

    def test_lists_become_tuples_so_a_profile_stays_immutable(self):
        profile = load(self._write({"tags": ["A", "B"], "preserved_tag_prefixes": ["x-"]}))
        self.assertEqual(profile.tags, ("A", "B"))
        self.assertEqual(profile.preserved_tag_prefixes, ("x-",))

    def test_part_type_notices_are_normalized_for_exact_matching(self):
        profile = load(self._write({"part_type_notices": {" AIR BAG ": " Be careful. "}}))
        self.assertEqual(profile.part_type_notice("Air Bag"), "Be careful.")


class NoEnvironmentLeak(unittest.TestCase):
    def test_rendering_reads_no_environment(self):
        """A render-path config gate that loaded `.env` would leak a real site into tests."""
        before = dict(os.environ)
        render(part(), ["51_01.jpg"], STORE)
        self.assertEqual(dict(os.environ), before)

    def test_a_bare_store_profile_touches_no_files(self):
        self.assertEqual(StoreProfile().catalog, CatalogProfile())
        self.assertEqual(StoreProfile().weights.rules, ())


if __name__ == "__main__":
    unittest.main()
