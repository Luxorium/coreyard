"""The one Shopify client: pagination, mutation error handling, throttling, REST.

Every command that talks to the store goes through this class, so the retry and throttle
behaviour is tested once here rather than diverging across five hand-rolled clients.
"""

import unittest
from unittest.mock import MagicMock

from coreyard.sink.shopify_api import (
    DEFAULT_API_VERSION,
    THROTTLE_FLOOR,
    ShopifyClient,
    ShopifyCreds,
)


def client() -> ShopifyClient:
    """A client with the network replaced, built without touching credentials."""
    instance = ShopifyClient.__new__(ShopifyClient)
    instance.creds = ShopifyCreds("example.myshopify.com", "shpat_x")
    instance._requests = MagicMock()
    instance._requests.RequestException = RuntimeError
    instance._session = MagicMock()
    instance._resume_at = 0.0
    return instance


def response(payload: dict, status: int = 200):
    reply = MagicMock()
    reply.status_code = status
    reply.json.return_value = payload
    return reply


class Endpoints(unittest.TestCase):
    def test_graphql_endpoint_pins_the_tested_api_version(self):
        creds = ShopifyCreds("example.myshopify.com", "t")
        self.assertIn(f"/admin/api/{DEFAULT_API_VERSION}/graphql.json", creds.endpoint)

    def test_rest_paths_share_the_api_version(self):
        creds = ShopifyCreds("example.myshopify.com", "t", "2099-01")
        self.assertEqual(creds.rest("orders/1.json"),
                         "https://example.myshopify.com/admin/api/2099-01/orders/1.json")

    def test_a_leading_slash_does_not_double_up(self):
        creds = ShopifyCreds("example.myshopify.com", "t")
        self.assertNotIn("//orders", creds.rest("/orders/1.json").split("://", 1)[1])


class Pagination(unittest.TestCase):
    def test_every_page_is_walked(self):
        api = client()
        api._session.post.side_effect = [
            response({"data": {"products": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                "nodes": [{"id": 1}, {"id": 2}]}}}),
            response({"data": {"products": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": 3}]}}}),
        ]
        nodes = list(api.paginate("query", "products"))
        self.assertEqual([n["id"] for n in nodes], [1, 2, 3])

    def test_the_cursor_is_carried_forward(self):
        api = client()
        api._session.post.side_effect = [
            response({"data": {"products": {
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": []}}}),
            response({"data": {"products": {
                "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}),
        ]
        list(api.paginate("query", "products", page_size=100))
        second = api._session.post.call_args_list[1].kwargs["json"]["variables"]
        self.assertEqual(second["cursor"], "c1")
        self.assertEqual(second["first"], 100)

    def test_max_pages_stops_a_walk_early(self):
        api = client()
        api._session.post.return_value = response({"data": {"products": {
            "pageInfo": {"hasNextPage": True, "endCursor": "c"}, "nodes": [{"id": 1}]}}})
        self.assertEqual(len(list(api.paginate("q", "products", max_pages=3))), 3)


class Mutations(unittest.TestCase):
    def test_user_errors_are_raised_rather_than_returned(self):
        """Shopify reports a refusal as a 200; a caller checking exceptions would miss it."""
        api = client()
        api._session.post.return_value = response(
            {"data": {"productUpdate": {"userErrors": [{"field": "title",
                                                        "message": "too long"}]}}})
        with self.assertRaises(RuntimeError) as caught:
            api.mutate("mutation", {}, "productUpdate")
        self.assertIn("too long", str(caught.exception))

    def test_a_clean_mutation_returns_its_payload(self):
        api = client()
        api._session.post.return_value = response(
            {"data": {"productUpdate": {"product": {"id": "1"}, "userErrors": []}}})
        self.assertEqual(api.mutate("mutation", {}, "productUpdate")["product"]["id"], "1")

    def test_a_null_mutation_root_does_not_crash(self):
        api = client()
        api._session.post.return_value = response({"data": {"productUpdate": None}})
        self.assertEqual(api.mutate("mutation", {}, "productUpdate"), {})


class Throttling(unittest.TestCase):
    def test_a_low_budget_schedules_a_pause(self):
        api = client()
        api._session.post.return_value = response({
            "data": {"shop": {"name": "Test Yard"}},
            "extensions": {"cost": {"throttleStatus": {
                "currentlyAvailable": THROTTLE_FLOOR - 1}}},
        })
        api.graphql("{ shop { name } }")
        self.assertGreater(api._resume_at, 0.0)

    def test_a_healthy_budget_does_not(self):
        api = client()
        api._session.post.return_value = response({
            "data": {"shop": {"name": "Test Yard"}},
            "extensions": {"cost": {"throttleStatus": {"currentlyAvailable": 1000}}},
        })
        api.graphql("{ shop { name } }")
        self.assertEqual(api._resume_at, 0.0)

    def test_graphql_errors_that_are_not_throttling_raise(self):
        api = client()
        api._session.post.return_value = response({"errors": [{"message": "boom"}]})
        with self.assertRaises(RuntimeError):
            api.graphql("{ shop { name } }")


class Rest(unittest.TestCase):
    def test_json_comes_back_unchanged(self):
        api = client()
        api._session.get.return_value = response({"order": {"id": 1}})
        self.assertEqual(api.rest_get("orders/1.json"), {"order": {"id": 1}})

    def test_the_path_is_appended_to_the_versioned_base(self):
        api = client()
        api._session.get.return_value = response({})
        api.rest_get("orders/1.json")
        self.assertEqual(api._session.get.call_args.args[0],
                         api.creds.rest("orders/1.json"))


class Helpers(unittest.TestCase):
    def test_access_scopes_are_returned_as_a_set(self):
        api = client()
        api._session.post.return_value = response({"data": {"currentAppInstallation": {
            "accessScopes": [{"handle": "read_orders"}, {"handle": "write_products"}]}}})
        self.assertEqual(api.access_scopes(), {"read_orders", "write_products"})

    def test_a_publication_is_found_by_name_case_insensitively(self):
        api = client()
        api._session.post.return_value = response({"data": {"publications": {"nodes": [
            {"id": "gid://shopify/Publication/1", "name": "Online Store"}]}}})
        self.assertEqual(api.publication_id("online store"),
                         "gid://shopify/Publication/1")

    def test_an_unknown_publication_is_none(self):
        api = client()
        api._session.post.return_value = response(
            {"data": {"publications": {"nodes": []}}})
        self.assertIsNone(api.publication_id("Online Store"))


if __name__ == "__main__":
    unittest.main()
