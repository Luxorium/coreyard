"""`coreyard validate` — the contract check a storefront repository runs in CI.

It has to work with nothing but a path: no database, no store, no credentials, no `.env`.
That is what lets the other side of the boundary check its own files against these schemas
without either application importing the other.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard import validate

GOOD_SHIPPING = {
    "groups": {
        "A": {"tag": "ship:freight-299", "price": "299.99", "label": "Freight",
              "match": ["engine assembly"], "fulfillment": "yard"},
        "G": {"tag": "ship:free", "price": "0.00", "label": "Free",
              "default": True, "fulfillment": "external"},
    },
    "match_order": ["A"],
}
GOOD_PROFILE = {"condition": "Used, tested", "metafield_namespace": "abm"}
GOOD_WEIGHTS = {"unit": "POUNDS", "default": 15, "rules": [["engine assembly", 550]]}
GOOD_ORDERS = {"tags": ["invoiced"], "reference_tag": "wo-{reference}"}


class Base(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)

    def write(self, name, data) -> str:
        path = self.dir / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)


class Valid(Base):
    def test_a_complete_healthy_set_passes(self):
        result = validate.validate(
            profile=self.write("p.json", GOOD_PROFILE),
            weights=self.write("w.json", GOOD_WEIGHTS),
            orders=self.write("o.json", GOOD_ORDERS),
            shipping=self.write("s.json", GOOD_SHIPPING))
        self.assertTrue(result.ok, result.errors)

    def test_checking_nothing_is_not_a_failure(self):
        self.assertTrue(validate.validate().ok)

    def test_one_file_can_be_checked_alone(self):
        self.assertTrue(validate.validate(
            shipping=self.write("s.json", GOOD_SHIPPING)).ok)

    def test_catalog_overrides_can_be_checked_alone(self):
        value = {"version": 1, "parts": {"42": {
            "title": "Reviewed Engine"}}}
        result = validate.validate(overrides=self.write("c.json", value))
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(any("1 title" in note for note in result.notes))

    def test_it_reports_what_it_found(self):
        result = validate.validate(shipping=self.write("s.json", GOOD_SHIPPING))
        self.assertTrue(any("ship:free" in note for note in result.notes))


class Invalid(Base):
    def test_broken_json_is_reported_not_raised(self):
        path = self.dir / "s.json"
        path.write_text("{nope", encoding="utf-8")
        result = validate.validate(shipping=str(path))
        self.assertFalse(result.ok)
        self.assertIn("shipping policy", result.errors[0])

    def test_a_missing_configured_file_fails(self):
        result = validate.validate(profile=str(self.dir / "absent.json"))
        self.assertFalse(result.ok)

    def test_duplicate_shipping_tags_fail(self):
        broken = json.loads(json.dumps(GOOD_SHIPPING))
        broken["groups"]["G"]["tag"] = "ship:freight-299"
        self.assertFalse(validate.validate(shipping=self.write("s.json", broken)).ok)

    def test_a_profile_namespace_shopify_would_reject_fails(self):
        broken = dict(GOOD_PROFILE, metafield_namespace="a b!")
        result = validate.validate(profile=self.write("p.json", broken))
        self.assertFalse(result.ok)
        self.assertIn("metafield_namespace", result.errors[0])

    def test_an_unknown_profile_key_fails(self):
        broken = dict(GOOD_PROFILE, conditon="typo")
        self.assertFalse(validate.validate(profile=self.write("p.json", broken)).ok)

    def test_a_bad_weight_rule_fails(self):
        broken = {"rules": [["engine", "heavy"]]}
        self.assertFalse(validate.validate(weights=self.write("w.json", broken)).ok)

    def test_every_error_is_collected_not_just_the_first(self):
        result = validate.validate(
            profile=self.write("p.json", {"conditon": "typo"}),
            weights=self.write("w.json", {"rules": [["x", 0]]}))
        self.assertEqual(len(result.errors), 2)


class CrossFile(Base):
    """Most real mistakes live between two files rather than inside one."""

    def test_an_order_policy_naming_a_missing_shipping_group_fails(self):
        orders = dict(GOOD_ORDERS, fulfillment={"groups": ["ship:freight-999"],
                                                "default_group": "ship:free"})
        result = validate.validate(orders=self.write("o.json", orders),
                                   shipping=self.write("s.json", GOOD_SHIPPING))
        self.assertFalse(result.ok)
        self.assertIn("ship:freight-999", result.errors[0])

    def test_an_order_policy_naming_real_groups_passes(self):
        orders = dict(GOOD_ORDERS, fulfillment={"groups": ["ship:free"],
                                                "defer_groups": ["ship:free"],
                                                "default_group": "ship:free"})
        self.assertTrue(validate.validate(orders=self.write("o.json", orders),
                                          shipping=self.write("s.json", GOOD_SHIPPING)).ok)

    def test_a_derived_order_policy_is_reported_as_derived(self):
        result = validate.validate(orders=self.write("o.json", GOOD_ORDERS),
                                   shipping=self.write("s.json", GOOD_SHIPPING))
        self.assertTrue(any("derived" in note for note in result.notes))

    def test_group_names_are_not_checked_without_a_shipping_policy(self):
        """Nothing to check them against; the order policy still stands on its own."""
        orders = dict(GOOD_ORDERS, fulfillment={"groups": ["anything"]})
        self.assertTrue(validate.validate(orders=self.write("o.json", orders)).ok)


class Advice(Base):
    def test_a_group_that_can_never_match_is_pointed_out(self):
        quiet = json.loads(json.dumps(GOOD_SHIPPING))
        quiet["groups"]["B"] = {"tag": "ship:freight-199", "price": "199.99"}
        result = validate.validate(shipping=self.write("s.json", quiet))
        self.assertTrue(result.ok)
        self.assertTrue(any("never classified" in n or "no 'match'" in n
                            for n in result.notes))

    def test_a_weight_table_with_no_default_is_pointed_out(self):
        result = validate.validate(
            weights=self.write("w.json", {"rules": [["engine assembly", 550]]}))
        self.assertTrue(result.ok)
        self.assertTrue(any("empty box" in n for n in result.notes))


class Offline(unittest.TestCase):
    def test_validating_by_path_reads_no_environment(self):
        """CI has no .env, and must not need one."""
        import os

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "s.json"
        path.write_text(json.dumps(GOOD_SHIPPING), encoding="utf-8")
        before = dict(os.environ)
        validate.validate(shipping=str(path))
        self.assertEqual(dict(os.environ), before)

    def test_the_cli_returns_zero_for_a_valid_file(self):
        import contextlib
        import io

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "s.json"
        path.write_text(json.dumps(GOOD_SHIPPING), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(validate.main(["--shipping", str(path)]), 0)

    def test_the_cli_returns_nonzero_for_a_broken_file(self):
        import contextlib
        import io

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "s.json"
        path.write_text("{nope", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(validate.main(["--shipping", str(path)]), 1)


if __name__ == "__main__":
    unittest.main()
