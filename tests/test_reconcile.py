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
        actions = plan(["0", "1"], live)
        self.assertEqual(actions.retire, [])
        self.assertIn("REFUSING", actions.refused)

    def test_force_overrides_the_fraction(self):
        live = {str(i): product(str(i)) for i in range(100)}
        actions = plan(["0", "1"], live, force_retire=True)
        self.assertEqual(len(actions.retire), 98)
        self.assertEqual(actions.refused, "")

    def test_a_yard_that_reported_nothing_is_refused_even_under_force(self):
        """Not a sell-out: on the named-pipe transport a refused statement and a query that
        matched no rows arrive as the same empty answer, so there is nothing to be sure of."""
        live = {str(i): product(str(i)) for i in range(100)}
        for forced in (False, True):
            with self.subTest(force_retire=forced):
                actions = plan([], live, force_retire=forced)
                self.assertEqual(actions.retire, [])
                self.assertIn("no listable parts at all", actions.refused)

    def test_the_last_part_selling_is_still_reachable_deliberately(self):
        """The guard is about an empty *source*, not about a small one."""
        live = {"1": product("1"), "2": product("2")}
        actions = plan(["1"], live, force_retire=True)
        self.assertEqual(actions.retire, ["2"])

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


class ApplyOrder(unittest.TestCase):
    """A draft must reach its sales channel BEFORE it is made ACTIVE.

    Status and channel publication are separate things in Shopify and only the second makes
    a URL resolve, so an ACTIVE product on no channel is a 404 to every shopper and to
    Google. The apply pass used to activate the whole catalogue and only then publish it,
    which left every product it had touched returning 404 for as long as the activate pass
    ran — hours, on a full catalogue — and a customer was sent to a part that would not
    load. Publishing a DRAFT is harmless, so publishing first has no window at all.
    """

    def _run_apply(self, products, fail_publish=()):
        import argparse
        from unittest import mock

        from coreyard.reconcile import cli

        calls: list[tuple[str, str]] = []

        class FakeClient:
            def mutate(self, document, variables, root):
                # The only mutation the driver issues itself is the status change.
                calls.append(("activate", variables["id"].rsplit("/", 1)[-1]))
                return {}

        class FakePublisher:
            publications = ["gid://shopify/Publication/1"]
            retire_status = ARCHIVED

            def __init__(self, store=None):
                pass

            def publish_to_channels(self, product_id):
                r_number = product_id.rsplit("/", 1)[-1]
                calls.append(("publish", r_number))
                if r_number in fail_publish:
                    raise RuntimeError("channel unavailable")

        class FakeState:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def load(self):
                return {}

            def retired_statuses(self):
                return {}

            def clear_retired(self, r_numbers):
                pass

            def forget(self, r_numbers):
                pass

        args = argparse.Namespace(
            apply=True, dry_run=False, activate=True, no_retire=True,
            force_retire=False, max_retire_fraction=0.1, repair_state=False,
        )
        with mock.patch.object(cli, "load_store", return_value=StoreProfile()), \
             mock.patch.object(cli, "scan", return_value=products), \
             mock.patch.object(cli, "SyncState", lambda *a, **k: FakeState()), \
             mock.patch.object(cli, "publication_names", return_value=["Online Store"]), \
             mock.patch("coreyard.sink.shopify_api.ShopifyClient", FakeClient), \
             mock.patch("coreyard.sink.shopify_write.ShopifyPublisher", FakePublisher), \
             mock.patch("coreyard.yms.inventory.is_configured", return_value=True), \
             mock.patch("coreyard.yms.inventory.photos_required", return_value=False), \
             mock.patch("coreyard.yms.inventory.listable_r_numbers",
                        return_value=set(products)):
            self.assertEqual(cli.run(args), 0)
        return calls

    def test_every_publish_happens_before_any_activate(self):
        products = {r: product(r, DRAFT, False) for r in ("1", "2", "3")}
        calls = self._run_apply(products)

        self.assertEqual({c[0] for c in calls}, {"publish", "activate"})
        last_publish = max(i for i, c in enumerate(calls) if c[0] == "publish")
        first_activate = min(i for i, c in enumerate(calls) if c[0] == "activate")
        self.assertLess(
            last_publish, first_activate,
            "a draft was activated before it reached a sales channel, which is a 404")

    def test_every_activated_draft_is_also_published(self):
        products = {r: product(r, DRAFT, False) for r in ("1", "2", "3")}
        calls = self._run_apply(products)
        published = {r for kind, r in calls if kind == "publish"}
        activated = {r for kind, r in calls if kind == "activate"}
        self.assertEqual(activated, {"1", "2", "3"})
        self.assertEqual(published, activated)

    def test_a_publication_failure_leaves_the_draft_in_its_safe_state(self):
        products = {r: product(r, DRAFT, False) for r in ("1", "2")}
        calls = self._run_apply(products, fail_publish={"2"})
        activated = {r for kind, r in calls if kind == "activate"}
        self.assertEqual(activated, {"1"})

    def test_a_publication_failure_does_not_revive_an_archived_product(self):
        products = {"1": product("1", ARCHIVED, False)}
        calls = self._run_apply(products, fail_publish={"1"})
        self.assertNotIn(("activate", "1"), calls)
