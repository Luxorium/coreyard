"""Reconciliation: closing the gap between the yard and the store, safely.

The planner is where a catalogue can be emptied by a bad answer from the database, so every
guard has a test. Nothing here touches a network or a database.
"""

import unittest

from coreyard.config import StoreProfile
from coreyard.reconcile import plan
from coreyard.reconcile.planner import ACTIVE, ARCHIVED, DRAFT, Actions, ShopProduct
from coreyard.reconcile.store import scan


def product(r_number: str, status: str = ACTIVE, published: bool = True) -> ShopProduct:
    return ShopProduct(r_number=r_number, product_id=f"gid://shopify/Product/{r_number}",
                       status=status, published=published,
                       handle=f"coreyard-{r_number}")


def shop(**kw) -> dict[str, ShopProduct]:
    return {r: product(r, *(v if isinstance(v, tuple) else (v,)))
            for r, v in kw.items()}


class Buckets(unittest.TestCase):
    def test_a_listable_part_with_no_product_is_a_create(self):
        actions = plan(["1"], {})
        self.assertEqual(actions.create, ["1"])

    def test_a_listable_draft_is_only_activated_when_policy_allows(self):
        live = {"1": product("1", DRAFT)}
        self.assertEqual(plan(["1"], live).activate, [])
        self.assertEqual(plan(["1"], live, activate=True).activate, ["1"])

    def test_an_archived_part_that_is_listable_again_is_revived(self):
        actions = plan(["1"], {"1": product("1", ARCHIVED)}, activate=True)
        self.assertEqual(actions.revive, [("1", ACTIVE)])

    def test_a_revival_uses_the_remembered_pre_archive_status(self):
        """A hand-set status must survive a part leaving and returning to the yard."""
        actions = plan(["1"], {"1": product("1", ARCHIVED)},
                       remembered={"1": DRAFT}, activate=True)
        self.assertEqual(actions.revive, [("1", DRAFT)])

    def test_without_a_memory_the_conservative_status_is_used(self):
        actions = plan(["1"], {"1": product("1", ARCHIVED)}, activate=False)
        self.assertEqual(actions.revive, [("1", DRAFT)])

    def test_an_active_product_that_is_no_longer_listable_is_retired(self):
        live = {str(i): product(str(i)) for i in range(20)}
        actions = plan([str(i) for i in range(19)], live)
        self.assertEqual(actions.retire, ["19"])

    def test_a_draft_that_is_not_listable_is_left_alone(self):
        """It is invisible already; archiving every unfinished draft is noise, not repair."""
        actions = plan([], {"1": product("1", DRAFT)})
        self.assertEqual(actions.retire, [])

    def test_an_active_product_on_no_channel_is_a_404_and_gets_published(self):
        live = {"1": product("1", ACTIVE, False)}
        self.assertEqual(plan(["1"], live, publish_channels=True).publish, ["1"])
        self.assertEqual(plan(["1"], live).publish, [])

    def test_a_newly_activated_draft_is_published_in_the_same_run(self):
        """Activating a product that is on no channel leaves it just as invisible."""
        live = {"1": product("1", DRAFT, published=False)}
        actions = plan(["1"], live, activate=True, publish_channels=True)
        self.assertEqual(actions.activate, ["1"])
        self.assertEqual(actions.publish, ["1"])


class RetirementGuards(unittest.TestCase):
    def test_a_mass_retirement_is_refused(self):
        """A yard database that answered partially looks exactly like a sell-out."""
        live = {str(i): product(str(i)) for i in range(100)}
        actions = plan([], live)
        self.assertEqual(actions.retire, [])
        self.assertIn("REFUSING", actions.refused)

    def test_force_overrides_the_fraction(self):
        live = {str(i): product(str(i)) for i in range(100)}
        actions = plan([], live, force_retire=True)
        self.assertEqual(len(actions.retire), 100)
        self.assertEqual(actions.refused, "")

    def test_the_fraction_is_configurable(self):
        live = {str(i): product(str(i)) for i in range(10)}
        self.assertEqual(plan([str(i) for i in range(8)], live).retire, [])
        actions = plan([str(i) for i in range(8)], live, max_retire_fraction=0.5)
        self.assertEqual(actions.retire, ["8", "9"])

    def test_retirement_can_be_switched_off_entirely(self):
        live = {"1": product("1")}
        self.assertEqual(plan([], live, retire=False).retire, [])

    def test_an_ordinary_trickle_of_sales_is_retired(self):
        live = {str(i): product(str(i)) for i in range(100)}
        actions = plan([str(i) for i in range(99)], live)
        self.assertEqual(actions.retire, ["99"])
        self.assertEqual(actions.refused, "")


class Drift(unittest.TestCase):
    def test_a_snapshot_entry_with_no_product_is_stale(self):
        """This is the bug: the sync believes it published a part that does not exist."""
        actions = plan(["1"], {}, state=["1", "2"])
        self.assertEqual(actions.drift.stale, ["1", "2"])

    def test_a_product_the_snapshot_never_recorded_is_reported(self):
        actions = plan(["1"], {"1": product("1")}, state=[])
        self.assertEqual(actions.drift.untracked, ["1"])

    def test_no_state_means_no_drift_reported(self):
        actions = plan(["1"], {"1": product("1")})
        self.assertEqual(actions.drift.stale, [])
        self.assertEqual(actions.drift.untracked, [])


class Summary(unittest.TestCase):
    def test_it_counts_the_writes_it_would_make(self):
        actions = Actions(create=["1"], activate=["2"], revive=[("3", ACTIVE)],
                          retire=["4"], publish=["5"])
        self.assertEqual(actions.writes, 4)          # create is not a write here
        self.assertIn("retire=1", actions.summary())


class FakeClient:
    def __init__(self, nodes):
        self._nodes = nodes

    def paginate(self, query, connection, variables=None, page_size=250, max_pages=None):
        yield from self._nodes


def node(handle, status=ACTIVE, sku="1", published=True):
    return {
        "id": f"gid://shopify/Product/{sku}", "handle": handle, "title": "A part",
        "status": status, "publishedAt": "2026-01-01T00:00:00Z" if published else None,
        "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/1", "sku": sku,
                                "inventoryQuantity": 1,
                                "inventoryItem": {"id": "gid://shopify/InventoryItem/1",
                                                  "tracked": True}}]},
    }


class Scan(unittest.TestCase):
    STORE = StoreProfile(vendor="Test Yard", handle_prefix="coreyard")

    def test_only_products_under_our_handle_prefix_are_ours(self):
        """A store may also sell merchandise; archiving it would be someone else's problem."""
        client = FakeClient([node("coreyard-51"), node("t-shirt-large", sku="TSHIRT")])
        found = scan(client, self.STORE)
        self.assertEqual(list(found), ["51"])

    def test_channel_visibility_comes_from_published_at(self):
        client = FakeClient([node("coreyard-51", published=False)])
        self.assertFalse(scan(client, self.STORE)["51"].published)

    def test_a_product_with_no_variant_still_scans(self):
        stub = node("coreyard-51")
        stub["variants"]["nodes"] = []
        found = scan(FakeClient([stub]), self.STORE)
        self.assertEqual(found["51"].quantity, 0)
        self.assertEqual(found["51"].inventory_item_id, "")


if __name__ == "__main__":
    unittest.main()
