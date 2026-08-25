"""Shipping classification: the promise a listing makes about how a part gets to a buyer.

Getting this wrong is expensive in both directions — a 605 lb engine quoting free ground, or
a shippable alternator refusing to ship — and it used to be made twice, by a storefront
script and by whoever published the product. These pin the single contract.
"""

import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.orders.policy import DEFAULT_POLICY, from_dict
from coreyard.sink.shopify_api import product_set_input
from coreyard.transform import shipping
from coreyard.transform.render import render
from coreyard.transform.shipping import ShippingPolicy, ShippingPolicyError

POLICY = {
    "groups": {
        "PICKUP": {"tag": "ship:pickup-only", "price": None, "label": "Pickup only",
                   "handle": "pickup-only", "fulfillment": "yard",
                   "match": ["door assembly", "glass", "hood"]},
        "A": {"tag": "ship:freight-299", "price": "299.99", "label": "Freight — oversize",
              "handle": "freight-oversize", "fulfillment": "yard",
              "match": ["engine assembly", "axle assembly"]},
        "B": {"tag": "ship:freight-199", "price": "199.99", "label": "Freight — heavy",
              "handle": "freight-heavy", "fulfillment": "yard",
              "match": ["transfer case"]},
        "GROUND": {"tag": "ship:free", "price": "0.00", "label": "Free ground",
                   "handle": "free-ground", "default": True, "fulfillment": "external"},
    },
    "match_order": ["PICKUP", "A", "B"],
}


def policy(**overrides) -> ShippingPolicy:
    data = json.loads(json.dumps(POLICY))
    data.update(overrides)
    return ShippingPolicy.from_dict(data)


def store(**kw) -> StoreProfile:
    base = dict(vendor="Test Yard", city="Testville, TX", shipping=policy())
    base.update(kw)
    return StoreProfile(**base)


def part(part_type="Alternator", **kw) -> Part:
    base = dict(r_number="51", part_type=part_type, price=Decimal("120.00"),
                year=2014, make="Ford", model="F-150")
    base.update(kw)
    return Part(**base)


class Parsing(unittest.TestCase):
    def test_the_documented_shape_loads(self):
        p = policy()
        self.assertEqual(len(p.groups), 4)
        self.assertEqual(p.default_group.id, "GROUND")
        self.assertEqual(p.by_id("A").price, "299.99")
        self.assertEqual(p.by_tag("ship:free").id, "GROUND")

    def test_group_order_follows_match_order(self):
        self.assertEqual([g.id for g in policy().groups][:3], ["PICKUP", "A", "B"])

    def test_a_group_left_out_of_match_order_still_exists(self):
        p = policy(match_order=["PICKUP"])
        self.assertIsNotNone(p.by_id("A"))

    def test_a_group_needs_a_tag(self):
        broken = json.loads(json.dumps(POLICY))
        del broken["groups"]["A"]["tag"]
        with self.assertRaises(ShippingPolicyError):
            ShippingPolicy.from_dict(broken)

    def test_two_groups_cannot_share_a_tag(self):
        """A product would then carry two classifications at once."""
        broken = json.loads(json.dumps(POLICY))
        broken["groups"]["B"]["tag"] = "ship:freight-299"
        with self.assertRaisesRegex(ShippingPolicyError, "share the tag"):
            ShippingPolicy.from_dict(broken)

    def test_exactly_one_default_is_required(self):
        for count in (0, 2):
            broken = json.loads(json.dumps(POLICY))
            del broken["groups"]["GROUND"]["default"]
            if count == 2:
                broken["groups"]["A"]["default"] = True
                broken["groups"]["B"]["default"] = True
            with self.assertRaises(ShippingPolicyError):
                ShippingPolicy.from_dict(broken)

    def test_the_default_group_may_not_also_match(self):
        broken = json.loads(json.dumps(POLICY))
        broken["groups"]["GROUND"]["match"] = ["alternator"]
        with self.assertRaises(ShippingPolicyError):
            ShippingPolicy.from_dict(broken)

    def test_an_unknown_fulfillment_owner_is_refused(self):
        broken = json.loads(json.dumps(POLICY))
        broken["groups"]["A"]["fulfillment"] = "maybe"
        with self.assertRaises(ShippingPolicyError):
            ShippingPolicy.from_dict(broken)

    def test_match_order_naming_a_missing_group_is_refused(self):
        with self.assertRaises(ShippingPolicyError):
            policy(match_order=["PICKUP", "NOPE"])

    def test_the_legacy_top_level_pattern_arrays_still_load(self):
        """An existing file kept its patterns beside the groups, not inside them."""
        legacy = {"groups": {"A": {"tag": "ship:freight-299", "price": "299.99"},
                             "G": {"tag": "ship:free", "price": "0.00", "default": True}},
                  "match_order": ["A"],
                  "A": ["engine assembly"]}
        p = ShippingPolicy.from_dict(legacy)
        self.assertEqual(p.by_id("A").match, ("engine assembly",))
        self.assertEqual(p.tag_for("Engine Assembly"), "ship:freight-299")


class Classification(unittest.TestCase):
    def test_first_matching_group_wins(self):
        self.assertEqual(policy().classify("Engine Assembly").id, "A")
        self.assertEqual(policy().classify("Transfer Case").id, "B")
        self.assertEqual(policy().classify("Door Assembly").id, "PICKUP")

    def test_anything_unmatched_falls_to_the_default(self):
        self.assertEqual(policy().classify("Alternator Generator").id, "GROUND")

    def test_matching_is_case_insensitive_and_substring(self):
        self.assertEqual(policy().classify("USED ENGINE ASSEMBLY, 5.0L").id, "A")

    def test_either_part_type_spelling_can_match(self):
        """The published wording and the yard's own are both offered, as for weights."""
        self.assertEqual(policy().classify("Engine Motor Assembly", "Engine Assembly").id,
                         "A")

    def test_no_policy_classifies_nothing(self):
        self.assertIsNone(ShippingPolicy().classify("Engine Assembly"))
        self.assertIsNone(ShippingPolicy().tag_for("Engine Assembly"))

    def test_a_group_that_is_not_shipped_says_so(self):
        self.assertFalse(policy().by_id("PICKUP").shippable)
        self.assertTrue(policy().by_id("A").shippable)

    def test_the_namespace_it_owns_is_derived_from_the_tags(self):
        self.assertEqual(policy().owned_prefixes, ("ship:",))
        self.assertEqual(ShippingPolicy().owned_prefixes, ())


class InTheRenderedProduct(unittest.TestCase):
    def test_the_tag_is_applied_during_the_normal_render(self):
        product = render(part("Engine Assembly"), [], store())
        self.assertIn("ship:freight-299", product.tags)
        self.assertEqual(product.shipping_group, "A")

    def test_an_unmatched_part_gets_the_default_tag(self):
        self.assertIn("ship:free", render(part(), [], store()).tags)

    def test_without_a_policy_no_shipping_tag_is_added(self):
        product = render(part("Engine Assembly"), [], StoreProfile(vendor="Test Yard"))
        self.assertEqual(product.shipping_group, "")
        self.assertFalse([t for t in product.tags if t.startswith("ship:")])

    def test_classification_moves_the_fingerprint(self):
        """The tag is a shopper-visible promise, so a reclassification must republish."""
        engine = render(part("Engine Assembly"), [], store()).fingerprint()
        alternator = render(part("Alternator"), [], store()).fingerprint()
        self.assertNotEqual(engine, alternator)

    def test_changing_a_groups_tag_moves_the_fingerprint(self):
        renamed = json.loads(json.dumps(POLICY))
        renamed["groups"]["A"]["tag"] = "ship:freight-349"
        before = render(part("Engine Assembly"), [], store()).fingerprint()
        after = render(part("Engine Assembly"), [],
                       store(shipping=ShippingPolicy.from_dict(renamed))).fingerprint()
        self.assertNotEqual(before, after)

    def test_a_rate_change_alone_does_not_republish(self):
        """The rate is charged by the delivery profile; only the tag reaches the product."""
        repriced = json.loads(json.dumps(POLICY))
        repriced["groups"]["A"]["price"] = "349.99"
        before = render(part("Engine Assembly"), [], store()).fingerprint()
        after = render(part("Engine Assembly"), [],
                       store(shipping=ShippingPolicy.from_dict(repriced))).fingerprint()
        self.assertEqual(before, after)


class TagOwnership(unittest.TestCase):
    """CoreYard generates ship:* now, so a stale one is replaced rather than kept."""

    def _sent(self, existing):
        product = render(part("Alternator"), [], store())
        return product_set_input(product, "ACTIVE", existing,
                                 owned_prefixes=store().shipping.owned_prefixes)["tags"]

    def test_a_stale_classification_is_replaced(self):
        tags = self._sent(["ship:freight-299", "Ford"])
        self.assertIn("ship:free", tags)
        self.assertNotIn("ship:freight-299", tags)

    def test_a_correct_classification_is_not_duplicated(self):
        tags = self._sent(["ship:free"])
        self.assertEqual([t for t in tags if t.startswith("ship:")], ["ship:free"])

    def test_another_systems_namespaced_tags_still_survive(self):
        self.assertIn("promo:winter", self._sent(["promo:winter", "ship:freight-299"]))

    def test_without_a_policy_an_existing_ship_tag_is_left_alone(self):
        """A site that has not configured shipping keeps whatever wrote its tags."""
        product = render(part(), [], StoreProfile(vendor="Test Yard"))
        tags = product_set_input(product, "ACTIVE", ["ship:freight-299"])["tags"]
        self.assertIn("ship:freight-299", tags)


class OrderPolicyDerivation(unittest.TestCase):
    """The shipping policy already says who ships each group; do not say it twice."""

    def test_fulfillment_is_derived_when_the_order_policy_is_quiet(self):
        derived = DEFAULT_POLICY.with_shipping(policy()).fulfillment
        self.assertTrue(derived.enabled)
        self.assertEqual(derived.default_group, "ship:free")
        self.assertEqual(derived.defer_groups, ("ship:free",))
        self.assertTrue(derived.defers(["ship:free"]))
        self.assertFalse(derived.defers(["ship:freight-299"]))

    def test_every_group_reaches_the_derived_rules(self):
        derived = DEFAULT_POLICY.with_shipping(policy()).fulfillment
        self.assertEqual(set(derived.groups), set(policy().tags))

    def test_an_explicit_block_wins(self):
        explicit = from_dict({"fulfillment": {"groups": ["ship:pickup-only"],
                                              "default_group": "ship:pickup-only"}})
        self.assertTrue(explicit.explicit_fulfillment)
        self.assertEqual(explicit.with_shipping(policy()).fulfillment.groups,
                         ("ship:pickup-only",))

    def test_no_shipping_policy_leaves_the_order_policy_alone(self):
        self.assertIs(DEFAULT_POLICY.with_shipping(ShippingPolicy()), DEFAULT_POLICY)

    def test_a_group_nobody_ships_externally_defers_nothing(self):
        internal = json.loads(json.dumps(POLICY))
        internal["groups"]["GROUND"]["fulfillment"] = "yard"
        derived = DEFAULT_POLICY.with_shipping(
            ShippingPolicy.from_dict(internal)).fulfillment
        self.assertEqual(derived.defer_groups, ())


class Loading(unittest.TestCase):
    def _write(self, data) -> Path:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "freight.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_no_path_means_no_classification(self):
        self.assertIs(shipping.load(None), shipping.EMPTY)
        self.assertFalse(shipping.EMPTY.configured)

    def test_comments_are_ignored(self):
        data = dict(POLICY, _comment=["notes"])
        self.assertEqual(len(shipping.load(self._write(data)).groups), 4)

    def test_a_configured_but_missing_file_is_an_error(self):
        """Publishing an unclassified catalogue is the failure this file prevents."""
        with self.assertRaises(ShippingPolicyError):
            shipping.load("/nonexistent/freight.json")

    def test_invalid_json_names_the_file(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "freight.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ShippingPolicyError):
            shipping.load(path)


if __name__ == "__main__":
    unittest.main()
