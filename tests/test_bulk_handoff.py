"""What a bulk load leaves behind for the incremental sync to find.

The two publish paths keep different progress stores: `coreyard bulk` appends to
`out/shopify_bulk_results.jsonl` so it can resume itself, and `coreyard sync` keeps
fingerprints in the sync state. Nothing joined them, so a bulk load of the whole catalogue
left the snapshot empty and the very next sync saw every product as new and published all of
it again through the slow path. The work was not lost; it simply was not *known*, which from
the next run's point of view is the same thing.

The property these pin is the handoff: after a bulk load, a sync has nothing to do — and a
part that did not publish is still waiting for one.
"""

import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock as mock
from decimal import Decimal
from pathlib import Path

from coreyard import run_sync
from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.sink import shopify_bulk
from coreyard.state import SyncState, fingerprints_all

STORE = StoreProfile()
NO_IMG = lambda part: []                                              # noqa: E731
PHOTOS = run_sync.Photos(resolve=NO_IMG, stamps=None)


def part(r_number: str) -> Part:
    return Part(r_number=r_number, part_type="Door", price=Decimal("100.00"),
                quantity=1, make="Honda", model="Civic", year=2015)


class Args:
    def __init__(self, log: Path, **kw):
        self.log = log
        self.limit = None
        self.workers = 2
        self.status = "DRAFT"
        self.max_attempts = 1
        self.no_resume = False
        self.images_only = False
        self.__dict__.update(kw)


class BulkLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.sqlite3"
        self.log = Path(self.tmp.name) / "bulk.jsonl"
        self.parts = [part("1"), part("2"), part("3")]
        self.failures: set[str] = set()

    def publish(self, part_, status, attempts, require_images):
        if part_.r_number in self.failures:
            return {"status": "error", "r_number": part_.r_number, "error": "throttled",
                    "attempts": attempts, "seconds": 0.0}
        return {"status": "ok", "r_number": part_.r_number, "product_id": "gid://x",
                "images_added": 0, "attempts": 1, "seconds": 0.0}

    def run_bulk(self, publish=None, **kw) -> int:
        with mock.patch.object(shopify_bulk, "fetch_parts", return_value=self.parts), \
                mock.patch.object(shopify_bulk, "photos_required", return_value=False), \
                mock.patch.object(shopify_bulk, "connect"), \
                mock.patch.object(shopify_bulk, "InterchangeResolver"), \
                mock.patch.object(shopify_bulk, "DEFAULT_STATE_DB", self.db), \
                mock.patch.object(shopify_bulk, "load_store", return_value=STORE), \
                mock.patch.object(shopify_bulk, "_publish_with_retry",
                                  side_effect=publish or self.publish), \
                mock.patch.object(run_sync, "_make_resolver", return_value=PHOTOS), \
                contextlib.redirect_stdout(io.StringIO()):
            return shopify_bulk.run(Args(self.log, **kw))

    def snapshot(self) -> dict:
        with SyncState(self.db) as state:
            return state.load()

    def what_a_sync_would_do(self):
        prints = fingerprints_all(self.parts, NO_IMG, STORE)
        with SyncState(self.db) as state:
            return state.diff(prints)

    def test_every_published_part_reaches_the_snapshot(self):
        self.assertEqual(self.run_bulk(), 0)
        self.assertEqual(sorted(self.snapshot()), ["1", "2", "3"])

    def test_a_sync_straight_afterwards_has_nothing_to_do(self):
        """The whole point. Before this, it had the entire catalogue to do."""
        self.run_bulk()
        diff = self.what_a_sync_would_do()
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.changed, [])
        self.assertEqual(sorted(diff.unchanged), ["1", "2", "3"])

    def test_a_part_that_failed_to_publish_is_not_recorded(self):
        """Recording it would say the storefront holds a product it never took."""
        self.failures = {"2"}
        self.assertEqual(self.run_bulk(), 1)
        self.assertEqual(sorted(self.snapshot()), ["1", "3"])
        self.assertEqual(self.what_a_sync_would_do().added, ["2"])

    def test_the_fingerprint_is_the_one_the_sync_computes(self):
        """A version of its own would strand the difference: the next sync would publish
        the part again and there would be no way to tell that from a real change."""
        self.run_bulk()
        expected = fingerprints_all(self.parts, NO_IMG, STORE).content
        self.assertEqual(self.snapshot(), expected)

    def test_the_resume_log_still_records_what_happened(self):
        self.failures = {"3"}
        self.run_bulk()
        entries = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual({e["r_number"]: e["status"] for e in entries},
                         {"1": "ok", "2": "ok", "3": "error"})

    def test_a_resumed_run_leaves_the_earlier_work_recorded(self):
        self.failures = {"2", "3"}
        self.run_bulk()
        self.failures = set()
        self.run_bulk()
        self.assertEqual(sorted(self.snapshot()), ["1", "2", "3"])

    def test_state_trouble_does_not_fail_a_run_that_published(self):
        """The cost of failing here is a sync that republishes — which is what used to
        happen every time. It is not worth killing a load that is otherwise working."""
        with mock.patch.object(SyncState, "update",
                               side_effect=OSError("database is locked")):
            self.assertEqual(self.run_bulk(), 0)
        self.assertEqual(self.snapshot(), {})

    def test_a_worker_dying_hard_does_not_discard_the_batch_it_was_in(self):
        """Banking only at the end means a run that never reaches the end banks nothing —
        the same lesson the publish loop's checkpoint learned the expensive way. The results
        collected alongside the one that raised describe products Shopify has already
        taken, so they are accounted for before the interrupt is let out."""
        published: list[str] = []

        def publish(part_, status, attempts, require_images):
            if part_.r_number == "3":
                raise KeyboardInterrupt
            published.append(part_.r_number)
            return self.publish(part_, status, attempts, require_images)

        with self.assertRaises(KeyboardInterrupt):
            self.run_bulk(publish=publish)
        self.assertTrue(published)
        self.assertEqual(sorted(self.snapshot()), sorted(published))


if __name__ == "__main__":
    unittest.main()
