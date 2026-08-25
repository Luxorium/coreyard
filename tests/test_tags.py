"""Tag ownership: CoreYard's tags stay current, everybody else's survive.

``productSet`` replaces the whole tag list, so a routine product update deletes any tag
another system added unless the merge puts it back. On a storefront that warns "pickup only"
from a tag, losing one turns a warning into a dead checkout the shopper finds at the end.
"""

import unittest
from decimal import Decimal
from unittest.mock import MagicMock

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.profile import CatalogProfile
from coreyard.sink.shopify_api import part_to_product_set_input, product_set_input
from coreyard.sink.shopify_write import ShopifyPublisher
from coreyard.transform import tags as tag_policy
from coreyard.transform.render import render

STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", handle_prefix="test")
PART = Part(r_number="51", part_type="Door Assembly", price=Decimal("250.00"),
            year=2014, make="Ford", model="F-150")

# What a storefront adds and CoreYard has never heard of.
SHIPPING_TAGS = ["ship:pickup-only", "ship:freight-299", "ship:freight-199", "ship:free"]


class Merge(unittest.TestCase):
    def test_a_new_product_has_nothing_to_preserve(self):
        self.assertEqual(tag_policy.merge(["A", "B"], None), ["A", "B"])

    def test_namespaced_tags_survive_without_being_configured(self):
        """CoreYard never emits `prefix:value`, so one on a product is somebody else's."""
        merged = tag_policy.merge(["Ford", "Used OEM"], ["Ford", "ship:pickup-only"])
        self.assertEqual(merged, ["Ford", "Used OEM", "ship:pickup-only"])

    def test_every_shipping_tag_survives(self):
        for tag in SHIPPING_TAGS:
            self.assertIn(tag, tag_policy.merge(["Ford"], ["Ford", tag]))

    def test_a_stale_generated_tag_is_dropped(self):
        """Otherwise tag repair could never repair anything."""
        merged = tag_policy.merge(["2019 Dodge 1500"], ["2019 Dodge Dodge 1500"])
        self.assertEqual(merged, ["2019 Dodge 1500"])

    def test_plain_external_tags_can_be_named_by_prefix(self):
        merged = tag_policy.merge(["Ford"], ["Ford", "promo winter"], prefixes=("promo",))
        self.assertEqual(merged, ["Ford", "promo winter"])

    def test_the_namespace_rule_can_be_switched_off(self):
        merged = tag_policy.merge(["Ford"], ["Ford", "ship:free"], namespaced=False)
        self.assertEqual(merged, ["Ford"])

    def test_case_differences_do_not_duplicate_a_tag(self):
        self.assertEqual(tag_policy.merge(["Ford"], ["ford"]), ["Ford"])

    def test_order_is_deterministic(self):
        """The merged list is compared against the live product to decide whether to write."""
        first = tag_policy.merge(["B", "A"], ["ship:free", "x:1"])
        second = tag_policy.merge(["B", "A"], ["ship:free", "x:1"])
        self.assertEqual(first, second)
        self.assertEqual(first, ["B", "A", "ship:free", "x:1"])

    def test_dropped_reports_only_what_a_merge_would_remove(self):
        dropped = tag_policy.dropped(["Ford"], ["Ford", "ship:free", "2019 Dodge Dodge 1500"])
        self.assertEqual(dropped, ["2019 Dodge Dodge 1500"])

    def test_blank_tags_are_ignored(self):
        self.assertEqual(tag_policy.merge(["", "  "], ["", "ship:free"]), ["ship:free"])


class Serializer(unittest.TestCase):
    def test_product_set_input_preserves_external_tags(self):
        product = render(PART, [], STORE)
        payload = product_set_input(product, "ACTIVE", existing_tags=["ship:pickup-only"])
        self.assertIn("ship:pickup-only", payload["tags"])
        for tag in product.tags:
            self.assertIn(tag, payload["tags"])

    def test_without_existing_tags_only_generated_ones_are_sent(self):
        payload = part_to_product_set_input(PART, STORE)
        self.assertEqual(payload["tags"], list(render(PART, [], STORE).tags))

    def test_a_configured_prefix_is_honoured_end_to_end(self):
        store = StoreProfile(vendor="Test Yard",
                             catalog=CatalogProfile(preserved_tag_prefixes=("legacy-",)))
        payload = part_to_product_set_input(PART, store,
                                            existing_tags=["legacy-import", "junk"])
        self.assertIn("legacy-import", payload["tags"])
        self.assertNotIn("junk", payload["tags"])


class Upsert(unittest.TestCase):
    """The publisher reads the live tags before it replaces them."""

    def _publisher(self) -> ShopifyPublisher:
        publisher = ShopifyPublisher.__new__(ShopifyPublisher)
        publisher.client = MagicMock()
        publisher.location = "gid://shopify/Location/1"
        publisher.status = "ACTIVE"
        publisher.retire_status = "ARCHIVED"
        publisher.require_images = False
        publisher.publications = []
        publisher.images = MagicMock()
        publisher.store = STORE
        return publisher

    def _sent_tags(self, publisher) -> list[str]:
        _, variables = publisher.client.mutate.call_args_list[-1].args[:2]
        return variables["input"]["tags"]

    def test_an_upsert_keeps_the_storefronts_tags(self):
        publisher = self._publisher()
        publisher.client.graphql.side_effect = [{"productByIdentifier": {
            "id": "gid://shopify/Product/1", "status": "ACTIVE",
            "tags": ["ship:freight-299", "2014 Ford F-150"],
            "media": {"nodes": [{"id": "gid://shopify/MediaImage/1"}]},
        }}]
        publisher.client.mutate.return_value = {"product": {"id": "gid://shopify/Product/1"}}

        publisher.publish(PART)

        self.assertIn("ship:freight-299", self._sent_tags(publisher))

    def test_a_brand_new_product_sends_only_generated_tags(self):
        publisher = self._publisher()
        publisher._staged_files = lambda part, alt_for: []
        publisher.client.graphql.side_effect = [{"productByIdentifier": None}]
        publisher.client.mutate.return_value = {"product": {"id": "gid://shopify/Product/2"}}

        publisher.publish(PART)

        self.assertEqual(self._sent_tags(publisher), list(render(PART, [], STORE).tags))

    def test_external_tags_are_not_part_of_the_fingerprint(self):
        """They belong to another system, so they must not make a part look changed."""
        product = render(PART, [], STORE)
        self.assertNotIn("ship:free", product.tags)
        self.assertEqual(product.fingerprint(), render(PART, [], STORE).fingerprint())


if __name__ == "__main__":
    unittest.main()
