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



def _publisher(status: str = "DRAFT", store: StoreProfile | None = None) -> ShopifyPublisher:
    """A publisher with every network dependency stubbed out."""
    publisher = ShopifyPublisher.__new__(ShopifyPublisher)
    publisher.client = MagicMock()
    publisher.location = "gid://shopify/Location/1"
    publisher.status = status
    publisher.retire_status = "ARCHIVED"
    publisher.require_images = False
    publisher.publications = []
    publisher.images = MagicMock()
    publisher.store = store or StoreProfile(vendor="Test Yard", city="Testville, TX",
                                            warranty="90-day warranty", handle_prefix="test")
    return publisher


def _found(status: str, media: int = 1, tags: list[str] | None = None) -> dict:
    return {"productByIdentifier": {
        "id": "gid://shopify/Product/1",
        "status": status,
        "tags": list(tags or []),
        "media": {"nodes": [{"id": f"gid://shopify/MediaImage/{i}"} for i in range(media)]},
    }}


def _sent_input(publisher: ShopifyPublisher) -> dict:
    """The ProductSetInput productSet was actually asked to write."""
    _, variables = publisher.client.mutate.call_args_list[-1].args[:2]
    return variables["input"]


def _sent_status(publisher: ShopifyPublisher) -> str:
    return _sent_input(publisher)["status"]


PART = Part(r_number="51", part_type="Alternator", price=Decimal("120.00"),
            year=2010, make="Honda", model="Civic")


class Revival(unittest.TestCase):
    """A part can come back — a voided work order returns it to the yard."""

    def test_archived_product_comes_back_as_its_old_status(self):
        publisher = _publisher()
        publisher.client.graphql.side_effect = [_found("ARCHIVED")]
        publisher.client.mutate.return_value = {"product": {"id": "gid://shopify/Product/1"}}

        publisher.publish(PART, revive_status="ACTIVE")

        self.assertEqual(_sent_status(publisher), "ACTIVE")

    def test_revival_never_overrides_a_live_product(self):
        """Only an archived product is revived; a hand-set status stands."""
        publisher = _publisher()
        publisher.client.graphql.side_effect = [_found("DRAFT")]
        publisher.client.mutate.return_value = {"product": {"id": "gid://shopify/Product/1"}}

        publisher.publish(PART, revive_status="ACTIVE")

        self.assertEqual(_sent_status(publisher), "DRAFT")

    def test_without_a_remembered_status_an_archived_product_stays_archived(self):
        """The pre-existing behaviour, kept deliberately: no memory, no change."""
        publisher = _publisher()
        publisher.client.graphql.side_effect = [_found("ARCHIVED")]
        publisher.client.mutate.return_value = {"product": {"id": "gid://shopify/Product/1"}}

        publisher.publish(PART)

        self.assertEqual(_sent_status(publisher), "ARCHIVED")

    def test_a_new_product_is_unaffected_by_a_stale_memory(self):
        publisher = _publisher(status="DRAFT")
        publisher._staged_files = lambda part, alt_for: []
        publisher.client.graphql.side_effect = [{"productByIdentifier": None}]
        publisher.client.mutate.return_value = {"product": {"id": "gid://shopify/Product/2"}}

        publisher.publish(PART, revive_status="ACTIVE")

        self.assertEqual(_sent_status(publisher), "DRAFT")


class SalesChannel(unittest.TestCase):
    """Status and channel publication are separate; only the second makes a URL resolve."""

    def _publisher_with_channel(self) -> ShopifyPublisher:
        publisher = _publisher(status="ACTIVE")
        publisher.publications = ["gid://shopify/Publication/1"]
        publisher._staged_files = lambda part, alt_for: []
        publisher.client.mutate.return_value = {"product": {"id": "gid://shopify/Product/1"}}
        return publisher

    def _mutations(self, publisher) -> list[str]:
        return [call.args[2] for call in publisher.client.mutate.call_args_list]

    def test_an_active_product_is_put_on_the_channel(self):
        publisher = self._publisher_with_channel()
        publisher.client.graphql.side_effect = [_found("ACTIVE", media=0)]

        publisher.publish(PART)

        self.assertIn("publishablePublish", self._mutations(publisher))

    def test_a_draft_is_not_published_to_a_channel(self):
        """DRAFT is the holding state for "not ready"; publishing it would mean nothing."""
        publisher = self._publisher_with_channel()
        publisher.status = "DRAFT"
        publisher.client.graphql.side_effect = [{"productByIdentifier": None}]

        publisher.publish(PART)

        self.assertNotIn("publishablePublish", self._mutations(publisher))

    def test_without_configured_channels_nothing_is_published(self):
        publisher = _publisher(status="ACTIVE")
        publisher._staged_files = lambda part, alt_for: []
        publisher.client.mutate.return_value = {"product": {"id": "gid://shopify/Product/1"}}
        publisher.client.graphql.side_effect = [{"productByIdentifier": None}]

        publisher.publish(PART)

        self.assertNotIn("publishablePublish", self._mutations(publisher))


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

    def test_a_draft_is_left_alone_when_retiring_from_absence(self):
        """A draft is already invisible; archiving it destroys "not ready" vs "gone"."""
        publisher = _publisher()
        publisher.client.graphql.side_effect = [{"productByIdentifier": {
            "id": "gid://shopify/Product/1", "status": "DRAFT",
            "variants": {"nodes": [{"inventoryItem": {
                "id": "gid://shopify/InventoryItem/1", "tracked": True}}]},
        }}]
        seen = []

        outcome = publisher.retire("51", record_prior=lambda r, s: seen.append((r, s)),
                                   skip_draft=True)

        self.assertEqual(outcome, "draft")
        self.assertEqual(seen, [])
        # Nothing was written: no stock change, no status change.
        self.assertEqual(publisher.client.graphql.call_count, 1)
        publisher.client.mutate.assert_not_called()

    def test_a_sale_still_archives_a_draft(self):
        """The order pipeline retires on positive evidence, so it does not skip."""
        publisher = _publisher()
        publisher.client.graphql.side_effect = [
            {"productByIdentifier": {
                "id": "gid://shopify/Product/1", "status": "DRAFT",
                "variants": {"nodes": [{"inventoryItem": {
                    "id": "gid://shopify/InventoryItem/1", "tracked": True}}]},
            }},
            {"inventorySetQuantities": {"userErrors": []}},
            {"productUpdate": {"userErrors": []}},
        ]

        self.assertEqual(publisher.retire("51"), "retired")

    def test_an_active_product_is_retired_even_with_the_draft_guard(self):
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

        self.assertEqual(publisher.retire("51", skip_draft=True), "retired")

    def test_an_absent_product_records_nothing(self):
        publisher = _publisher()
        publisher.client.graphql.side_effect = [{"productByIdentifier": None}]
        seen = []

        self.assertEqual(publisher.retire("51", record_prior=lambda r, s: seen.append((r, s))),
                         "absent")

        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
