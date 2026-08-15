import unittest
import sqlite3
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.state import (
    DiffResult,
    SyncState,
    fingerprints_for,
    fingerprints_with_images,
    product_fingerprint,
)


def part(r_number="1", price="10.00", **kw) -> Part:
    return Part(r_number=r_number, stock_number="251026", part_type="Alternator",
                price=Decimal(price), year=2010, make="Honda", model="Civic", **kw)


NO_IMG = lambda p: []
STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="90-day warranty")


class Fingerprint(unittest.TestCase):
    def test_stable_for_same_content(self):
        a = product_fingerprint(part(), [], STORE)
        b = product_fingerprint(part(), [], STORE)
        self.assertEqual(a, b)

    def test_changes_with_price(self):
        a = product_fingerprint(part(price="10.00"), [], STORE)
        b = product_fingerprint(part(price="12.00"), [], STORE)
        self.assertNotEqual(a, b)

    def test_changes_with_images(self):
        a = product_fingerprint(part(), [], STORE)
        b = product_fingerprint(part(), ["http://x/1.jpg"], STORE)
        self.assertNotEqual(a, b)


class Diff(unittest.TestCase):
    def test_added_changed_unchanged_removed(self):
        with TemporaryDirectory() as d:
            db = Path(d) / "state.sqlite3"
            with SyncState(db) as st:
                run1 = fingerprints_for([part("1"), part("2")], NO_IMG, STORE)
                self.assertEqual(st.diff(run1).added, ["1", "2"])
                st.commit(run1)

                # run2: part 1 price changes, part 2 gone (sold), part 3 new
                run2 = fingerprints_for(
                    [part("1", price="99.00"), part("3")], NO_IMG, STORE
                )
                d2 = st.diff(run2)
                self.assertEqual(d2.changed, ["1"])
                self.assertEqual(d2.added, ["3"])
                self.assertEqual(d2.removed, ["2"])
                st.commit(run2)

                # run3: identical to run2 -> all unchanged
                d3 = st.diff(run2)
                self.assertEqual(d3.unchanged, ["1", "3"])
                self.assertEqual(d3.added, [])
                self.assertEqual(d3.removed, [])

    def test_unlistable_excluded(self):
        fps = fingerprints_for(
            [part("1"), Part(r_number="2", part_type="X", price=None)], NO_IMG, STORE
        )
        self.assertEqual(list(fps.keys()), ["1"])

    def test_summary(self):
        r = DiffResult(added=["a"], removed=["b", "c"])
        self.assertIn("added=1", r.summary())
        self.assertIn("removed=2", r.summary())

    def test_old_misnamed_state_column_is_migrated(self):
        with TemporaryDirectory() as d:
            db = Path(d) / "state.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE parts (stock TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                "last_seen TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO parts VALUES ('51', 'fingerprint', 'then')")
            conn.commit()
            conn.close()

            with SyncState(db) as state:
                self.assertEqual(state.load(), {"51": "fingerprint"})
                columns = {row[1] for row in state.conn.execute("PRAGMA table_info(parts)")}
                self.assertIn("r_number", columns)
                self.assertNotIn("stock", columns)
                self.assertIn("image_fingerprint", columns)


class ImageChanges(unittest.TestCase):
    """A photo added or swapped on the share has to reach the storefront."""

    def _run(self, state, parts, images):
        resolver = lambda p: images.get(p.uid(), [])
        content, image_fps = fingerprints_with_images(parts, resolver, STORE)
        return state.diff(content, image_fps), content, image_fps

    def test_photo_change_is_reported_separately(self):
        with TemporaryDirectory() as d:
            with SyncState(Path(d) / "state.sqlite3") as st:
                parts = [part("1"), part("2")]
                diff, content, imgs = self._run(st, parts, {"1": ["1_01.jpg"], "2": ["2_01.jpg"]})
                st.commit(content, imgs)

                # Part 1 gains a second photo; part 2 is untouched.
                diff, content, imgs = self._run(
                    st, parts, {"1": ["1_01.jpg", "1_02.jpg"], "2": ["2_01.jpg"]}
                )
                self.assertEqual(diff.image_changed, ["1"])
                self.assertEqual(diff.changed, ["1"])
                self.assertEqual(diff.unchanged, ["2"])

    def test_text_only_change_does_not_refresh_photos(self):
        """Re-uploading media is expensive; a price edit must not trigger it."""
        with TemporaryDirectory() as d:
            with SyncState(Path(d) / "state.sqlite3") as st:
                diff, content, imgs = self._run(st, [part("1")], {"1": ["1_01.jpg"]})
                st.commit(content, imgs)

                diff, _, _ = self._run(st, [part("1", price="99.00")], {"1": ["1_01.jpg"]})
                self.assertEqual(diff.changed, ["1"])
                self.assertEqual(diff.image_changed, [])

    def test_upgrade_run_does_not_claim_every_photo_changed(self):
        """Rows written before the image column exists read as unknown, not as changed."""
        with TemporaryDirectory() as d:
            db = Path(d) / "state.sqlite3"
            with SyncState(db) as st:
                content = fingerprints_for([part("1")], lambda p: ["1_01.jpg"], STORE)
                st.commit(content)  # legacy commit: no image fingerprints
            with SyncState(db) as st:
                diff, _, _ = self._run(st, [part("1")], {"1": ["1_01.jpg"]})
                self.assertEqual(diff.image_changed, [])


if __name__ == "__main__":
    unittest.main()
