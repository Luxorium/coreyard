"""SYNC-02: what a run leaves behind when it fails part of the way through.

The criterion names four places to inject a failure — before the request, after the remote
write but before the local checkpoint, during a batch, and during checkpoint persistence —
and one property that has to survive all four: restarting converges, with no duplicate
product and no forgotten update.

Duplicates are the reason these tests drive a real :class:`ShopifyPublisher` against a fake
store rather than a fake publisher. What decides whether a part published-but-not-banked
becomes a second product on the next run is ``productByIdentifier``: the handle is the
identity, and nothing CoreYard remembers locally is consulted. A test that stubs the
publisher cannot see that, because it stubs out the only thing under test.

The complement of "no duplicates" is "no forgotten updates", and that is the failure this
file guards hardest: a part that did *not* publish must keep the fingerprint the storefront
actually holds, so the next diff still finds it. The moment a failed part is banked at its
new fingerprint, the change is lost permanently — nothing ever proposes it again.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from coreyard import status
from coreyard.run_sync import CHANNEL, _publish_batch
from coreyard.sink.shopify_write import ShopifyPublisher
from coreyard.state import SyncState, fingerprints_all
from coreyard.state import subset as fp_subset
from coreyard.yms.images import SmbError

from tests.test_sync_plan import NO_IMG, STORE, part


class FakeStore:
    """A Shopify store whose one real rule is that a handle is an identity.

    ``productSet`` upserts: a handle it has never seen creates a product, a handle it holds
    updates that product. That is Shopify's own behaviour, and it is the whole reason an
    interrupted run is safe to repeat.
    """

    def __init__(self):
        self.products: dict[str, dict] = {}
        self.created: list[str] = []
        self.upserts: list[str] = []
        self.refuse: dict[str, Exception] = {}

    def graphql(self, query, variables=None):
        if "productByIdentifier" in query:
            found = self.products.get(variables["h"])
            if not found:
                return {"productByIdentifier": None}
            return {"productByIdentifier": {
                "id": found["id"], "status": found["status"], "tags": [],
                "media": {"nodes": []}, "metafields": {"nodes": []}}}
        raise AssertionError(f"unexpected query: {query[:40]}")

    def mutate(self, mutation, variables, root):
        if root == "productSet":
            handle = variables["input"]["handle"]
            refusal = self.refuse.get(handle)
            if refusal is not None:
                raise refusal
            self.upserts.append(handle)
            product = self.products.get(handle)
            if product is None:
                product = {"id": f"gid://shopify/Product/{len(self.products) + 1}",
                           "status": variables["input"]["status"]}
                self.products[handle] = product
                self.created.append(handle)
            return {"product": {"id": product["id"], "handle": handle}}
        if root == "publishablePublish":
            return {"userErrors": []}
        raise AssertionError(f"unexpected mutation: {root}")


class NoPhotos:
    """A photo share holding nothing, or refusing to answer at all."""

    def __init__(self, error: Exception | None = None):
        self.error = error

    def fetch(self, r_number, dest_dir):
        if self.error is not None:
            raise self.error
        return []

    def fetch_vehicle(self, donor_key, dest_dir):
        return []


def publisher_for(store: FakeStore, images=None) -> ShopifyPublisher:
    """A real publisher with the network replaced and nothing else stubbed."""
    publisher = ShopifyPublisher.__new__(ShopifyPublisher)
    publisher.client = store
    publisher.store = STORE
    publisher.images = images or NoPhotos()
    publisher.location = "gid://shopify/Location/1"
    publisher.status = "ACTIVE"
    publisher.retire_status = "ARCHIVED"
    publisher.require_images = False
    publisher.publications = []
    publisher.donor_files = None
    return publisher


class Run:
    """One sync run against a shared store and state database.

    ``bank`` and ``fail`` are the closures ``cmd_sync`` builds, kept in step with it by
    :class:`BothRunPathsRecordFailures` below rather than by hope.
    """

    def __init__(self, db: Path, store: FakeStore, parts, images=None, bank_error=None):
        self.db, self.store, self.parts = db, store, list(parts)
        self.images, self.bank_error = images, bank_error
        self.banked: list[set[str]] = []

    def go(self):
        fingerprints = fingerprints_all(self.parts, NO_IMG, STORE)
        with SyncState(self.db) as state:
            diff = state.diff(fingerprints)
            todo = [p for p in self.parts
                    if p.uid() in set(diff.added) | set(diff.changed)]

            def bank(pending):
                if self.bank_error is not None and not self.banked:
                    self.banked.append(set(pending))
                    raise self.bank_error
                self.banked.append(set(pending))
                subset = fp_subset(fingerprints, pending)
                state.update(subset)
                state.record_channel(CHANNEL, subset)

            def fail(r_number, reason):
                state.record_channel_failure(CHANNEL, r_number, reason)

            published, _ = _publish_batch(publisher_for(self.store, self.images), todo,
                                          set(), {}, bank, fail)
            return published, [p.uid() for p in todo]


def yard(*r_numbers, price="100.00"):
    from decimal import Decimal

    return [part(r, price=Decimal(price)) for r in r_numbers]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        self.store = FakeStore()
        self.addCleanup(self.tmp.cleanup)

    def snapshot(self) -> dict:
        with SyncState(self.db) as state:
            return state.load()

    def failures(self) -> dict:
        with SyncState(self.db) as state:
            return state.channel_failures(CHANNEL)


class BeforeTheRequest(Fixture):
    """The photo share is unreachable, so the part never reaches the store at all."""

    def setUp(self):
        super().setUp()
        self.store.products = {}

    def test_a_part_that_fails_while_staging_is_never_sent(self):
        run = Run(self.db, self.store, yard("51"),
                  images=NoPhotos(SmbError("share unreachable")))
        published, _ = run.go()
        self.assertEqual(published, set())
        self.assertEqual(self.store.upserts, [])

    def test_it_keeps_no_fingerprint_so_the_next_run_still_owes_it(self):
        Run(self.db, self.store, yard("51"),
            images=NoPhotos(SmbError("share unreachable"))).go()
        self.assertEqual(self.snapshot(), {})

        published, _ = Run(self.db, self.store, yard("51")).go()
        self.assertEqual(published, {"51"})
        self.assertEqual(self.store.created, ["coreyard-51"])

    def test_the_reason_is_recorded_rather_than_only_printed(self):
        Run(self.db, self.store, yard("51"),
            images=NoPhotos(SmbError("share unreachable"))).go()
        self.assertIn("share unreachable", self.failures().get("51", ""))

    def test_a_later_success_clears_it(self):
        Run(self.db, self.store, yard("51"),
            images=NoPhotos(SmbError("share unreachable"))).go()
        Run(self.db, self.store, yard("51")).go()
        self.assertEqual(self.failures(), {})


class AfterTheRemoteWriteBeforeTheCheckpoint(Fixture):
    """The part is live on the storefront and the snapshot never heard about it."""

    def _publish_without_banking(self):
        parts = yard("51")
        fingerprints = fingerprints_all(parts, NO_IMG, STORE)
        publisher = publisher_for(self.store)
        # The process dies between the remote write and the local write: banking never runs.
        publisher.publish(parts[0])
        return fingerprints

    def test_the_next_run_updates_that_product_instead_of_creating_a_second(self):
        self._publish_without_banking()
        self.assertEqual(self.store.created, ["coreyard-51"])

        published, _ = Run(self.db, self.store, yard("51")).go()

        self.assertEqual(published, {"51"})
        self.assertEqual(self.store.created, ["coreyard-51"])
        self.assertEqual(self.store.upserts, ["coreyard-51", "coreyard-51"])
        self.assertEqual(len(self.store.products), 1)

    def test_and_the_update_it_missed_is_not_forgotten(self):
        """The interrupted run published the old price; the snapshot must not say so."""
        self._publish_without_banking()
        published, todo = Run(self.db, self.store, yard("51", price="175.00")).go()
        self.assertEqual(todo, ["51"])
        self.assertEqual(published, {"51"})


class DuringTheBatch(Fixture):
    def test_only_the_parts_that_published_advance_their_fingerprint(self):
        self.store.refuse["coreyard-52"] = RuntimeError("productSet: throttled")
        published, _ = Run(self.db, self.store, yard("51", "52", "53")).go()

        self.assertEqual(published, {"51", "53"})
        self.assertEqual(sorted(self.snapshot()), ["51", "53"])
        self.assertIn("throttled", self.failures().get("52", ""))

    def test_the_refused_part_is_retried_and_lands(self):
        self.store.refuse["coreyard-52"] = RuntimeError("productSet: throttled")
        Run(self.db, self.store, yard("51", "52", "53")).go()
        del self.store.refuse["coreyard-52"]

        published, todo = Run(self.db, self.store, yard("51", "52", "53")).go()

        self.assertEqual(todo, ["52"])
        self.assertEqual(published, {"52"})
        self.assertEqual(sorted(self.snapshot()), ["51", "52", "53"])
        self.assertEqual(self.failures(), {})
        self.assertEqual(sorted(self.store.created),
                         ["coreyard-51", "coreyard-52", "coreyard-53"])

    def test_an_unexpected_error_still_banks_what_had_published(self):
        """Not every failure is a RuntimeError. The run ends, the work is still banked."""
        self.store.refuse["coreyard-52"] = MemoryError("out of memory")
        with self.assertRaises(MemoryError):
            Run(self.db, self.store, yard("51", "52", "53")).go()
        self.assertEqual(sorted(self.snapshot()), ["51"])


class DuringCheckpointPersistence(Fixture):
    """The state database itself refuses the write that records progress."""

    def test_a_checkpoint_that_cannot_be_written_claims_nothing(self):
        run = Run(self.db, self.store, yard("51", "52"),
                  bank_error=OSError("database or disk is full"))
        with self.assertRaises(OSError):
            run.go()
        self.assertEqual(self.snapshot(), {})
        self.assertEqual(sorted(self.store.upserts), ["coreyard-51", "coreyard-52"])

    def test_the_next_run_converges_without_duplicating_them(self):
        with self.assertRaises(OSError):
            Run(self.db, self.store, yard("51", "52"),
                bank_error=OSError("database or disk is full")).go()

        published, todo = Run(self.db, self.store, yard("51", "52")).go()

        self.assertEqual(sorted(todo), ["51", "52"])
        self.assertEqual(published, {"51", "52"})
        self.assertEqual(sorted(self.store.created), ["coreyard-51", "coreyard-52"])
        self.assertEqual(len(self.store.products), 2)


class StillFailingIsVisible(Fixture):
    """The count alone cannot say whether it is the same three parts every tick."""

    def test_status_names_the_parts_and_why(self):
        report = {"reachability": [], "counts": {}, "pending": {}, "runs": {},
                  "failing": {"shopify": {"51": "productSet: throttled"}}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status._render(report)
        self.assertIn("R#51", out.getvalue())
        self.assertIn("throttled", out.getvalue())
        self.assertIn("still pending", out.getvalue())

    def test_a_long_list_is_summarised_rather_than_dumped(self):
        report = {"reachability": [], "counts": {}, "pending": {}, "runs": {},
                  "failing": {"shopify": {str(r): "throttled" for r in range(10)}}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status._render(report)
        self.assertIn("and 7 more", out.getvalue())

    def test_a_report_without_the_section_still_renders(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status._render({"reachability": [], "counts": {}, "pending": {}, "runs": {}})
        self.assertIn("CoreYard", out.getvalue())


class BothRunPathsRecordFailures(unittest.TestCase):
    """A failure recorded by one scheduled path and not the other is worse than neither."""

    def test_every_publish_loop_passes_a_failure_recorder(self):
        import ast
        import inspect

        from coreyard import run_sync

        tree = ast.parse(inspect.getsource(run_sync))
        calls = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and getattr(node.func, "id", "") == "_publish_batch"]
        self.assertEqual(len(calls), 2, "expected the full and delta publish loops")
        for call in calls:
            names = [a.id for a in call.args if isinstance(a, ast.Name)]
            self.assertIn("bank", names)
            self.assertIn("fail", names)


if __name__ == "__main__":
    unittest.main()
