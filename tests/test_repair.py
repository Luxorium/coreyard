"""Catalog repair: rewrite what an older renderer produced, and nothing else.

Two properties matter most. Repair must be idempotent — a second run finds nothing, which is
what lets an interrupted run simply resume — and it must not delete tags another system owns
while it is fixing CoreYard's own.
"""

import unittest
from dataclasses import replace
from decimal import Decimal

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.repair import engine
from coreyard.repair.engine import LiveProduct
from coreyard.transform.render import render
from coreyard.transform.weights import Weight, WeightRules

STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="90-day warranty")
WEIGHED = StoreProfile(vendor="Test Yard", city="Testville, TX",
                       warranty="90-day warranty",
                       weights=WeightRules(rules=(("alternator", 18.0),), unit="POUNDS"))

PART = Part(r_number="51", part_type="Alternator", price=Decimal("120.00"),
            year=2010, make="Honda", model="Civic", stock_number="251026")


def wanted(store: StoreProfile = STORE) -> dict:
    return {"51": render(PART, [], store)}


def live(**kw) -> dict[str, LiveProduct]:
    """A product that already matches the renderer, unless a field is overridden."""
    product = render(PART, [], STORE)
    base = dict(r_number="51", product_id="gid://shopify/Product/1", status="ACTIVE",
                handle=product.handle, title=product.title,
                product_type=product.product_type, tags=product.tags,
                description_html=product.description_html,
                seo_title=product.seo_title, seo_description=product.seo_description,
                inventory_item_id="gid://shopify/InventoryItem/1",
                metafields=tuple((m.key, m.type, m.value) for m in product.metafields))
    base.update(kw)
    return {"51": LiveProduct(**base)}


class FakeClient:
    """Records mutations instead of making them."""

    def __init__(self):
        self.calls = []

    def mutate(self, document, variables, root):
        self.calls.append((root, variables))
        return {}


class Idempotence(unittest.TestCase):
    def test_a_product_that_already_matches_needs_no_write(self):
        self.assertEqual(engine.plan(live(), wanted(), store=STORE), [])

    def test_a_second_pass_over_repaired_output_finds_nothing(self):
        changes = engine.plan(live(title="2010 Honda Civic - Alternator - OEM"),
                              wanted(), ("title",), store=STORE)
        self.assertEqual(len(changes), 1)
        repaired = live(title=changes[0].fields["title"][1])
        self.assertEqual(engine.plan(repaired, wanted(), ("title",), store=STORE), [])


class Fields(unittest.TestCase):
    def test_a_stale_title_is_repaired(self):
        changes = engine.plan(live(title="2010 / 2011 Honda Civic Alternator & 3 more"),
                              wanted(), ("title",), store=STORE)
        self.assertEqual(changes[0].fields["title"][1], render(PART, [], STORE).title)

    def test_only_the_requested_field_is_touched(self):
        changes = engine.plan(live(title="wrong", seo_title="also wrong"),
                              wanted(), ("title",), store=STORE)
        self.assertEqual(changes[0].names(), ["title"])

    def test_seo_metadata_is_repaired_as_a_pair(self):
        changes = engine.plan(live(seo_description=""), wanted(), ("seo",), store=STORE)
        title, description = changes[0].fields["seo"][1]
        self.assertEqual(description, render(PART, [], STORE).seo_description)
        self.assertEqual(title, render(PART, [], STORE).seo_title)

    def test_a_missing_weight_is_filled_in(self):
        changes = engine.plan(live(), wanted(WEIGHED), ("weight",), store=WEIGHED)
        self.assertEqual(changes[0].fields["weight"][1], (18.0, "POUNDS"))

    def test_a_weight_that_already_means_the_same_mass_is_left_alone(self):
        already = live(weight_value=8165.0, weight_unit="GRAMS")   # 18 lb, in grams
        self.assertEqual(engine.plan(already, wanted(WEIGHED), ("weight",), store=WEIGHED),
                         [])

    def test_no_rule_for_a_part_means_no_weight_write(self):
        self.assertEqual(engine.plan(live(), wanted(), ("weight",), store=STORE), [])

    def test_a_product_absent_from_the_yard_can_still_get_a_weight(self):
        """An archived part still needs a sane weight if it is ever revived."""
        changes = engine.plan(live(), {}, ("weight",), store=WEIGHED,
                              weights_by_type={"alternator generator": Weight(18.0, "POUNDS")})
        self.assertEqual(changes[0].fields["weight"][1], (18.0, "POUNDS"))

    def test_text_is_never_rewritten_for_a_part_the_yard_no_longer_lists(self):
        """Rendering off a bare extract would replace a real title with a guess."""
        self.assertEqual(engine.plan(live(title="anything"), {}, ("title",), store=STORE), [])


class MetafieldRepair(unittest.TestCase):
    """Structured data a product was published without, or published wrong."""

    def test_a_product_with_no_metafields_gets_them(self):
        changes = engine.plan(live(metafields=()), wanted(), ("metafields",), store=STORE)
        added = changes[0].fields["metafields"][1]
        self.assertIn("condition", added)
        self.assertIn("fitment", added)

    def test_a_stale_value_is_corrected(self):
        product = render(PART, [], STORE)
        stale = tuple(("condition", "single_line_text_field", "WRONG") if m.key == "condition"
                      else (m.key, m.type, m.value) for m in product.metafields)
        changes = engine.plan(live(metafields=stale), wanted(), ("metafields",), store=STORE)
        self.assertEqual(set(changes[0].fields["metafields"][1]), {"condition"})

    def test_matching_metafields_need_no_write(self):
        self.assertEqual(engine.plan(live(), wanted(), ("metafields",), store=STORE), [])

    def test_the_namespace_travels_with_the_change(self):
        changes = engine.plan(live(metafields=()), wanted(), ("metafields",), store=STORE)
        self.assertEqual(changes[0].namespace, STORE.catalog.metafield_namespace)

    def test_metafields_are_written_with_metafieldsSet(self):
        client = FakeClient()
        changes = engine.plan(live(metafields=()), wanted(), ("metafields",), store=STORE)
        engine.apply(client, changes[0])
        self.assertEqual([root for root, _ in client.calls], ["metafieldsSet"])
        sent = client.calls[0][1]["fields"]
        self.assertTrue(all(set(f) == {"ownerId", "namespace", "key", "type", "value"}
                            for f in sent))

    def test_a_metafield_change_does_not_replace_the_live_catalogue_map(self):
        """Planning must continue after the first product with stale structured data."""
        products = live(metafields=())
        second = replace(
            next(iter(live(title="wrong").values())),
            r_number="52",
            product_id="gid://shopify/Product/2",
        )
        products["52"] = second
        rendered = next(iter(wanted().values()))

        changes = engine.plan(
            products,
            {"51": rendered, "52": rendered},
            ("title", "metafields"),
            store=STORE,
        )

        self.assertEqual([change.r_number for change in changes], ["51", "52"])
        self.assertIn("metafields", changes[0].fields)
        self.assertEqual(changes[1].names(), ["title"])


class TagRepair(unittest.TestCase):
    def test_a_doubled_make_tag_is_replaced(self):
        product = render(PART, [], STORE)
        changes = engine.plan(live(tags=tuple(product.tags) + ("2019 Dodge Dodge 1500",)),
                              wanted(), ("tags",), store=STORE)
        self.assertNotIn("2019 Dodge Dodge 1500", changes[0].fields["tags"][1])

    def test_storefront_tags_survive_a_tag_repair(self):
        product = render(PART, [], STORE)
        changes = engine.plan(live(tags=tuple(product.tags) + ("ship:freight-299",)),
                              wanted(), ("tags",), store=STORE)
        self.assertEqual(changes, [])       # nothing to do: the merge already matches

    def test_a_repair_that_must_write_still_keeps_the_storefront_tags(self):
        changes = engine.plan(live(tags=("stale", "ship:pickup-only")),
                              wanted(), ("tags",), store=STORE)
        merged = changes[0].fields["tags"][1]
        self.assertIn("ship:pickup-only", merged)
        self.assertNotIn("stale", merged)
        self.assertEqual(changes[0].dropped_tags, ["stale"])

    def test_what_would_be_deleted_is_reported_before_it_is(self):
        changes = engine.plan(live(tags=("old one", "old two")), wanted(), ("tags",),
                              store=STORE)
        self.assertEqual(changes[0].dropped_tags, ["old one", "old two"])


class Apply(unittest.TestCase):
    def test_all_product_fields_go_in_one_mutation(self):
        client = FakeClient()
        changes = engine.plan(live(title="wrong", seo_title="wrong", tags=("stale",)),
                              wanted(), ("title", "seo", "tags"), store=STORE)
        engine.apply(client, changes[0])
        self.assertEqual([root for root, _ in client.calls], ["productUpdate"])
        payload = client.calls[0][1]["input"]
        self.assertIn("title", payload)
        self.assertIn("tags", payload)
        self.assertIn("seo", payload)
        self.assertEqual(payload["id"], "gid://shopify/Product/1")

    def test_weight_is_written_against_the_inventory_item(self):
        client = FakeClient()
        changes = engine.plan(live(), wanted(WEIGHED), ("weight",), store=WEIGHED)
        engine.apply(client, changes[0])
        self.assertEqual([root for root, _ in client.calls], ["inventoryItemUpdate"])
        self.assertEqual(client.calls[0][1]["id"], "gid://shopify/InventoryItem/1")
        self.assertEqual(client.calls[0][1]["u"], "POUNDS")


class Scan(unittest.TestCase):
    class FakeClient:
        def __init__(self, nodes):
            self._nodes = nodes

        def paginate(self, query, connection, variables=None, page_size=250, max_pages=None):
            yield from self._nodes

    def _node(self, handle):
        return {
            "id": "gid://shopify/Product/1", "handle": handle, "title": "A part",
            "status": "ACTIVE", "productType": "Alternator Generator",
            "tags": ["Honda"], "descriptionHtml": "<p>x</p>",
            "seo": {"title": "t", "description": "d"},
            "variants": {"nodes": [{"id": "v", "sku": "51", "inventoryItem": {
                "id": "gid://shopify/InventoryItem/1",
                "measurement": {"weight": {"value": 18.0, "unit": "POUNDS"}}}}]},
        }

    def test_foreign_products_are_skipped(self):
        client = self.FakeClient([self._node("coreyard-51"), self._node("t-shirt")])
        found = engine.scan(client, STORE)
        self.assertEqual(list(found), ["51"])
        self.assertEqual(found["51"].weight_value, 18.0)


if __name__ == "__main__":
    unittest.main()
