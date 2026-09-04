"""Doctor checks that must speak up, and must stay quiet when nothing is wrong.

`check_unpublished` exists because of a real outage: reconciliation activated the catalogue
before publishing it to a sales channel, leaving 12,900 products ACTIVE, in stock, priced
and returning 404. Every count in every report included them, because status is what the
reports look at and status was fine. The one check that could see it lived behind
`status --deep`, which pages the whole store and so is never run casually.
"""

import unittest

from coreyard.doctor import FAIL, OK, WARN, check_unpublished


class FakeClient:
    """A Shopify client that answers one productsCount query."""

    def __init__(self, count=0, precision="EXACT", raises=None):
        self.count, self.precision, self.raises = count, precision, raises
        self.queries: list[str] = []

    def graphql(self, query, variables=None, tries=5):
        if self.raises:
            raise self.raises
        self.queries.append((variables or {}).get("q", ""))
        return {"productsCount": {"count": self.count, "precision": self.precision}}


class Unpublished(unittest.TestCase):
    def test_it_says_nothing_alarming_when_every_active_product_is_on_a_channel(self):
        (level, name, detail), = check_unpublished(FakeClient(count=0))
        self.assertEqual(level, OK)
        self.assertEqual(name, "unpublished")

    def test_one_unpublished_active_product_is_a_failure(self):
        (level, _, detail), = check_unpublished(FakeClient(count=1))
        self.assertEqual(level, FAIL)
        self.assertIn("1 ACTIVE", detail)
        self.assertIn("404", detail)

    def test_a_capped_count_is_reported_as_a_lower_bound(self):
        (level, _, detail), = check_unpublished(
            FakeClient(count=10000, precision="AT_LEAST"))
        self.assertEqual(level, FAIL)
        self.assertIn("at least 10000", detail)

    def test_it_asks_only_about_active_products_missing_a_channel(self):
        client = FakeClient(count=0)
        check_unpublished(client)
        self.assertEqual(client.queries,
                         ["status:active AND published_status:unpublished"])

    def test_an_unreachable_store_warns_rather_than_failing_the_run(self):
        (level, name, _), = check_unpublished(FakeClient(raises=RuntimeError("boom")))
        self.assertEqual(level, WARN)
        self.assertEqual(name, "unpublished")


if __name__ == "__main__":
    unittest.main()
