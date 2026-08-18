"""Offline tests for Shopify API configuration, retirement, and revival."""

import unittest
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.sink.shopify_api import DEFAULT_API_VERSION, ShopifyCreds
from coreyard.sink.shopify_oauth import DEFAULT_SCOPES
from coreyard.sink.shopify_write import ShopifyPublisher


class Credentials(unittest.TestCase):
    def test_default_endpoint_uses_the_tested_api_version(self):
        creds = ShopifyCreds("example.myshopify.com", "token")
        self.assertIn(f"/admin/api/{DEFAULT_API_VERSION}/graphql.json", creds.endpoint)

    def test_api_version_can_be_selected_per_credentials(self):
        creds = ShopifyCreds("example.myshopify.com", "token", "2099-01")
        self.assertIn("/admin/api/2099-01/graphql.json", creds.endpoint)

    def test_default_oauth_scopes_allow_paid_order_webhooks(self):
        self.assertIn("read_orders", DEFAULT_SCOPES.split(","))


class Retirement(unittest.TestCase):
    def test_zero_quantity_explicitly_opts_out_of_compare_and_swap(self):
        publisher = ShopifyPublisher.__new__(ShopifyPublisher)
        publisher.client = MagicMock()
        publisher.location = "gid://shopify/Location/1"
        publisher.retire_status = "ARCHIVED"
        publisher.store = MagicMock(handle_prefix="coreyard")
        publisher.client.graphql.side_effect = [
            {"productByIdentifier": {
                "id": "gid://shopify/Product/1",
                "status": "ACTIVE",
                "variants": {"nodes": [{"inventoryItem": {
                    "id": "gid://shopify/InventoryItem/1", "tracked": True,
                }}]},
            }},
            {"inventorySetQuantities": {"userErrors": []}},
            {"productUpdate": {"userErrors": []}},
        ]

        self.assertEqual(publisher.retire("51"), "retired")

        mutation, variables = publisher.client.graphql.call_args_list[1].args
        quantity = variables["input"]["quantities"][0]
        self.assertIn("changeFromQuantity", quantity)
        self.assertIsNone(quantity["changeFromQuantity"])
        self.assertIn("@idempotent(key:$idempotencyKey)", mutation)
        uuid.UUID(variables["idempotencyKey"])



def _publisher(status: str = "DRAFT") -> ShopifyPublisher:
    """A publisher with every network dependency stubbed out."""
    publisher = ShopifyPublisher.__new__(ShopifyPublisher)
    publisher.client = MagicMock()
    publisher.location = "gid://shopify/Location/1"
    publisher.status = status
    publisher.retire_status = "ARCHIVED"
    publisher.require_images = False
    publisher.images = MagicMock()
    publisher.store = StoreProfile(vendor="Test Yard", city="Testville, TX",
                                   warranty="90-day warranty", handle_prefix="test")
    return publisher


def _found(status: str, media: int = 1) -> dict:
    return {"productByIdentifier": {
        "id": "gid://shopify/Product/1",
        "status": status,
        "media": {"nodes": [{"id": f"gid://shopify/MediaImage/{i}"} for i in range(media)]},
    }}


def _sent_status(publisher: ShopifyPublisher) -> str:
    """The status productSet was actually asked to write."""
    _, variables = publisher.client.graphql.call_args_list[-1].args
    return variables["input"]["status"]


PART = Part(r_number="51", part_type="Alternator", price=Decimal("120.00"),
            year=2010, make="Honda", model="Civic")


class Revival(unittest.TestCase):
    """A part can come back — a voided work order returns it to the yard."""

    def test_archived_product_comes_back_as_its_old_status(self):
        publisher = _publisher()
        publisher.client.graphql.side_effect = [
            _found("ARCHIVED"),
            {"productSet": {"product": {"id": "gid://shopify/Product/1"}, "userErrors": []}},
        ]

        publisher.publish(PART, revive_status="ACTIVE")

        self.assertEqual(_sent_status(publisher), "ACTIVE")

    def test_revival_never_overrides_a_live_product(self):
        """Only an archived product is revived; a hand-set status stands."""
        publisher = _publisher()
        publisher.client.graphql.side_effect = [
            _found("DRAFT"),
            {"productSet": {"product": {"id": "gid://shopify/Product/1"}, "userErrors": []}},
        ]

        publisher.publish(PART, revive_status="ACTIVE")

        self.assertEqual(_sent_status(publisher), "DRAFT")

    def test_without_a_remembered_status_an_archived_product_stays_archived(self):
        """The pre-existing behaviour, kept deliberately: no memory, no change."""
        publisher = _publisher()
        publisher.client.graphql.side_effect = [
            _found("ARCHIVED"),
            {"productSet": {"product": {"id": "gid://shopify/Product/1"}, "userErrors": []}},
        ]

        publisher.publish(PART)

        self.assertEqual(_sent_status(publisher), "ARCHIVED")

    def test_a_new_product_is_unaffected_by_a_stale_memory(self):
        publisher = _publisher(status="DRAFT")
        publisher._staged_files = lambda part: []
        publisher.client.graphql.side_effect = [
            {"productByIdentifier": None},
            {"productSet": {"product": {"id": "gid://shopify/Product/2"}, "userErrors": []}},
        ]

        publisher.publish(PART, revive_status="ACTIVE")

        self.assertEqual(_sent_status(publisher), "DRAFT")


class RetirementMemory(unittest.TestCase):
    def test_the_status_being_replaced_is_recorded(self):
        publisher = _publisher()
        publisher.client.graphql.side_effect = [
            {"productByIdentifier": {
                "id": "gid://shopify/Product/1", "status": "ACTIVE",
                "variants": {"nodes": [{"inventoryItem": {
                    "id": "gid://shopify/InventoryItem/1", "tracked": True}}]},
            }},
            {"inventorySetQuantities": {"userErrors": []}},
            {"productUpdate": {"userErrors": []}},
        ]
        seen = []

        self.assertEqual(publisher.retire("51", record_prior=lambda r, s: seen.append((r, s))),
                         "retired")

        self.assertEqual(seen, [("51", "ACTIVE")])

    def test_re_retiring_does_not_overwrite_the_memory(self):
        """Otherwise the second pass would record ARCHIVED and strand the part."""
        publisher = _publisher()
        publisher.client.graphql.side_effect = [
            {"productByIdentifier": {
                "id": "gid://shopify/Product/1", "status": "ARCHIVED",
                "variants": {"nodes": [{"inventoryItem": {
                    "id": "gid://shopify/InventoryItem/1", "tracked": True}}]},
            }},
            {"inventorySetQuantities": {"userErrors": []}},
        ]
        seen = []

        self.assertEqual(publisher.retire("51", record_prior=lambda r, s: seen.append((r, s))),
                         "already")

        self.assertEqual(seen, [])

    def test_an_absent_product_records_nothing(self):
        publisher = _publisher()
        publisher.client.graphql.side_effect = [{"productByIdentifier": None}]
        seen = []

        self.assertEqual(publisher.retire("51", record_prior=lambda r, s: seen.append((r, s))),
                         "absent")

        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
