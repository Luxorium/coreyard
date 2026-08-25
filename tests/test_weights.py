"""The externally supplied weight table: matching, units, and refusing bad input."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.transform import weights


def rules(**kw) -> weights.WeightRules:
    base = dict(rules=(("engine assembly", 550.0), ("engine", 90.0), ("alternator", 18.0)),
                unit="POUNDS")
    base.update(kw)
    return weights.WeightRules(**base)


class Matching(unittest.TestCase):
    def test_first_rule_wins_so_specific_beats_general(self):
        self.assertEqual(rules().lookup("Engine Assembly").value, 550)
        self.assertEqual(rules().lookup("Engine Oil Cooler").value, 90)

    def test_matching_is_case_insensitive_and_substring(self):
        self.assertEqual(rules().lookup("USED ALTERNATOR GENERATOR").rule, "alternator")

    def test_any_supplied_spelling_can_match(self):
        """The published product type and the source wording are both offered."""
        found = rules().lookup("Generator", "Alternator")
        self.assertIsNotNone(found)
        self.assertEqual(found.rule, "alternator")

    def test_no_match_and_no_default_means_no_weight(self):
        self.assertIsNone(rules().lookup("Sun Visor"))

    def test_the_default_catches_everything_else(self):
        self.assertEqual(rules(default=15.0).lookup("Sun Visor").rule, "(default)")
        self.assertEqual(rules(default=15.0).lookup("Sun Visor").value, 15)

    def test_nothing_to_match_against_falls_through_to_the_default(self):
        self.assertEqual(rules(default=15.0).lookup(None, "").value, 15)

    def test_the_safety_margin_rounds_up_to_a_whole_unit(self):
        """Carriers rebill an under-declared shipment, which costs more than the postage."""
        padded = rules(safety_multiplier=1.1)
        self.assertEqual(padded.lookup("Alternator").value, 20)   # 18 * 1.1 = 19.8 -> 20

    def test_grams_are_derived_from_the_declared_unit(self):
        self.assertEqual(weights.Weight(1.0, "POUNDS").grams, 454)
        self.assertEqual(weights.Weight(1.0, "KILOGRAMS").grams, 1000)
        self.assertEqual(weights.Weight(16.0, "OUNCES").grams, 454)
        self.assertEqual(weights.Weight(2500.0, "GRAMS").grams, 2500)


class Loading(unittest.TestCase):
    def _write(self, data) -> Path:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "weights.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_no_path_means_an_empty_table(self):
        self.assertIs(weights.load(None), weights.EMPTY)
        self.assertIsNone(weights.EMPTY.lookup("Anything"))

    def test_the_documented_shape_loads(self):
        path = self._write({
            "_comment": "site notes are ignored",
            "unit": "POUNDS", "default": 15, "safety_multiplier": 1.1,
            "rules": [["engine assembly", 550], ["alternator", 18]],
        })
        table = weights.load(path)
        self.assertEqual(table.unit, "POUNDS")
        self.assertEqual(table.default, 15.0)
        self.assertEqual(table.lookup("Engine Assembly").value, 605)

    def test_object_form_rules_load_too(self):
        path = self._write({"rules": [{"match": "wheel", "weight": 40}]})
        self.assertEqual(weights.load(path).lookup("Wheel Rim").value, 40)

    def test_a_configured_but_missing_file_is_an_error(self):
        """Falling back to "no weights" would quote every shopper for an empty box."""
        with self.assertRaises(weights.WeightRulesError):
            weights.load("/nonexistent/weights.json")

    def test_a_bad_unit_is_rejected(self):
        path = self._write({"unit": "STONES", "rules": [["wheel", 40]]})
        with self.assertRaises(weights.WeightRulesError):
            weights.load(path)

    def test_a_malformed_rule_is_rejected(self):
        for bad in ([["wheel"]], [["wheel", "heavy"]], [["", 40]], [["wheel", 0]]):
            with self.assertRaises(weights.WeightRulesError):
                weights.load(self._write({"rules": bad}))

    def test_invalid_json_is_reported_with_the_path(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "weights.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(weights.WeightRulesError):
            weights.load(path)


if __name__ == "__main__":
    unittest.main()
