"""The photo manifest, and the two bugs it exists to fix.

A yard corrects a bad photograph by saving the new one over the old, under the same
filename. A manifest of filenames alone cannot see that, so the storefront kept showing the
picture somebody had already replaced — indefinitely, because nothing ever moved.

The second bug is what the refresh did once it *had* noticed: it deleted the live media
first and uploaded afterwards, so any failure in between left a product on sale with no
photographs at all.
"""

import tempfile
import types
import unittest
from pathlib import Path

from coreyard.state import (IMAGE_MANIFEST_VERSION, Fingerprints, SyncState,
                            image_fingerprint)
from coreyard.yms.images import SmbImageStore

# A realistic `smbclient ls`: name, DOS attributes, size, modification time.
LISTING = """Domain=[WORKGROUP] OS=[Windows Server 2019] Server=[Windows]
  .                                   D        0  Mon Aug 25 14:30:11 2026
  ..                                  D        0  Mon Aug 25 14:30:11 2026
  10002_01.jpg                        A   184320  Tue Aug 25 09:14:02 2026
  10002_02.jpg                        A   201533  Tue Aug 25 09:14:07 2026
  100020_01.jpg                       A    99112  Wed Aug 26 08:01:44 2026
  51_01.jpg                           A    73211  Mon Aug 24 17:20:00 2026
  notes.txt                           A      512  Mon Aug 24 17:20:00 2026

                9204224 blocks of size 4096. 3110982 blocks available
"""

REPLACED = LISTING.replace(
    "10002_01.jpg                        A   184320  Tue Aug 25 09:14:02 2026",
    "10002_01.jpg                        A   190881  Wed Aug 26 11:02:35 2026")


def fake_store(listing):
    store = SmbImageStore.__new__(SmbImageStore)
    store.cfg = types.SimpleNamespace(inventory_subdir="pics")
    store._run = lambda *a, **k: listing
    return store


class Manifest(unittest.TestCase):
    def test_a_photo_replaced_under_its_own_name_is_detected(self):
        before = dict(SmbImageStore._parse_ls(LISTING, "10002"))
        after = dict(SmbImageStore._parse_ls(REPLACED, "10002"))
        self.assertEqual(sorted(before), sorted(after), "the filenames are identical")
        self.assertNotEqual(before["10002_01.jpg"], after["10002_01.jpg"])
        self.assertNotEqual(image_fingerprint(list(before.values())),
                            image_fingerprint(list(after.values())))

    def test_the_two_listing_paths_agree_exactly(self):
        """The full sync lists the share once; a delta run lists one part at a time. If the
        two spelled an unchanged photo differently, every delta run would re-fingerprint
        the parts it touched and re-upload their media."""
        per_part = dict(SmbImageStore._parse_ls(LISTING, "10002"))
        whole = dict(fake_store(LISTING).list_all_inventory_manifest()["10002"])
        self.assertEqual(per_part, whole)

    def test_anchoring_survives_the_manifest(self):
        """R#1000 must not pick up 10002_01.jpg, and R#10002 must not pick up 100020_01."""
        self.assertEqual(SmbImageStore._parse_ls(LISTING, "1000"), [])
        self.assertEqual([n for n, _ in SmbImageStore._parse_ls(LISTING, "10002")],
                         ["10002_01.jpg", "10002_02.jpg"])

    def test_non_photos_and_listing_noise_are_ignored(self):
        whole = fake_store(LISTING).list_all_inventory_manifest()
        self.assertEqual(sorted(whole), ["10002", "100020", "51"])

    def test_filenames_still_come_back_ordered_by_sequence(self):
        self.assertEqual(fake_store(LISTING).list_all_inventory_images()["10002"],
                         ["10002_01.jpg", "10002_02.jpg"])

    def test_an_unparsable_row_degrades_identically_on_both_paths(self):
        """A listing this parser cannot read must still produce the *same* stamp either
        way, or the two paths would disagree and thrash."""
        odd = "Domain=[X]\n  51_01.jpg\n"
        per_part = dict(SmbImageStore._parse_ls(odd, "51"))
        whole = dict(fake_store(odd).list_all_inventory_manifest()["51"])
        self.assertEqual(per_part, whole)


class Rebaseline(unittest.TestCase):
    """Upgrading the manifest must not re-upload the media of the whole catalogue."""

    def _state(self, tmp):
        return SyncState(Path(tmp) / "state.sqlite3")

    def test_the_upgrade_run_reports_no_photo_changes(self):
        with tempfile.TemporaryDirectory() as tmp, self._state(tmp) as state:
            state.conn.execute(
                "INSERT INTO parts(r_number, fingerprint, image_fingerprint, last_seen)"
                " VALUES ('1','fp','old-style-hash','2026-01-01')")
            state.conn.commit()
            diff = state.diff({"1": "fp"}, {"1": "brand-new-style-hash"})
            self.assertEqual(diff.image_changed, [],
                             "a manifest upgrade is not 10,000 photo changes")

    def test_a_full_commit_declares_the_new_version(self):
        with tempfile.TemporaryDirectory() as tmp, self._state(tmp) as state:
            state.commit({"1": "fp"}, {"1": "new-hash"})
            self.assertEqual(state.get_cursor("image_manifest_version"),
                             IMAGE_MANIFEST_VERSION)

    def test_a_partial_update_does_not_declare_it(self):
        """Only a commit rewrites every row. A scoped run that claimed the new version
        would leave most rows in the old shape, and the next run would compare the two."""
        with tempfile.TemporaryDirectory() as tmp, self._state(tmp) as state:
            state.update(Fingerprints(content={"1": "fp"}, images={"1": "new-hash"}))
            self.assertIsNone(state.get_cursor("image_manifest_version"))

    def test_a_run_that_scanned_no_photos_does_not_declare_the_version(self):
        """`--no-image-scan` establishes nothing about the manifest. Declaring the version
        from one would make the *next* run compare old-shape stored values against
        new-shape ones, and flag every photographed part as changed."""
        with tempfile.TemporaryDirectory() as tmp, self._state(tmp) as state:
            state.commit({"1": "fp"}, {"1": ""})
            self.assertIsNone(state.get_cursor("image_manifest_version"))

    def test_photo_changes_are_detected_again_once_rebaselined(self):
        with tempfile.TemporaryDirectory() as tmp, self._state(tmp) as state:
            state.commit({"1": "fp"}, {"1": "hash-a"})
            diff = state.diff({"1": "fp"}, {"1": "hash-b"})
            self.assertEqual(diff.image_changed, ["1"])


class FakeClient:
    """Records mutations, and can be told to fail one of them."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.variables = {}
        self.fail_on = fail_on

    def _maybe_fail(self, name):
        if self.fail_on == name:
            raise RuntimeError(f"{name} failed")

    def graphql(self, query, variables=None):
        if "stagedUploadsCreate" in query:
            self.calls.append("stage")
            self._maybe_fail("stage")
            return {"stagedUploadsCreate": {"userErrors": [], "stagedTargets": []}}
        raise AssertionError("unexpected query")

    def mutate(self, query, variables, name):
        self.calls.append(name)
        self.variables[name] = variables
        self._maybe_fail(name)
        if name == "productSet":
            return {"product": {"id": "gid://Product/1"}}
        return {}


class FailureSafeRefresh(unittest.TestCase):
    """A refresh that cannot finish must leave the product's existing photos alone."""

    def _publisher(self, client, staged):
        from coreyard.config import StoreProfile
        from coreyard.sink.shopify_write import ShopifyPublisher

        publisher = ShopifyPublisher.__new__(ShopifyPublisher)
        publisher.client = client
        publisher.store = StoreProfile()
        publisher.status = "DRAFT"
        publisher.retire_status = "ARCHIVED"
        publisher.publications = []
        publisher.location = "gid://Location/1"
        publisher.require_images = False
        publisher._find = lambda handle: (
            "gid://Product/1", ["gid://File/old"], "ACTIVE", [], [])
        publisher._staged_files = staged
        publisher._prune_metafields = lambda *a, **k: None
        publisher._upsert = lambda *a, **k: "gid://Product/1"
        return publisher

    def _part(self):
        from decimal import Decimal

        from coreyard.models import Part

        return Part(r_number="51", part_type="Door", price=Decimal("100.00"), quantity=1)

    def test_a_staging_failure_does_not_delete_the_live_photos(self):
        client = FakeClient()

        def staged(part, alt_for):
            raise RuntimeError("share unreachable")

        publisher = self._publisher(client, staged)
        with self.assertRaises(RuntimeError):
            publisher.publish(self._part(), refresh_images=True)
        self.assertNotIn("fileUpdate", client.calls,
                         "the product would have been left with no photographs")

    def test_a_failed_upsert_does_not_delete_the_live_photos(self):
        client = FakeClient()
        publisher = self._publisher(client, lambda part, alt_for: [{"originalSource": "x"}])

        def boom(*a, **k):
            raise RuntimeError("productSet failed")

        publisher._upsert = boom
        with self.assertRaises(RuntimeError):
            publisher.publish(self._part(), refresh_images=True)
        self.assertNotIn("fileUpdate", client.calls)

    def test_a_successful_refresh_removes_the_superseded_media_afterwards(self):
        client = FakeClient()
        publisher = self._publisher(client, lambda part, alt_for: [{"originalSource": "x"}])
        publisher.publish(self._part(), refresh_images=True)
        self.assertIn("fileUpdate", client.calls)
        self.assertNotIn("fileDelete", client.calls)
        self.assertEqual(
            client.variables["fileUpdate"]["files"],
            [{"id": "gid://File/old", "referencesToRemove": ["gid://Product/1"]}],
        )

    def test_nothing_stageable_keeps_the_existing_photos(self):
        """Better a stale photograph than none."""
        client = FakeClient()
        publisher = self._publisher(client, lambda part, alt_for: [])
        publisher.publish(self._part(), refresh_images=True)
        self.assertNotIn("fileUpdate", client.calls)

    def test_media_the_upsert_already_removed_is_not_a_failure(self):
        """``productSet(files=...)`` replaces the media set, so the follow-up detach finds
        the old ids gone. That is this step's goal already met — it must not raise, or a
        donor part refreshed on every delta tick never checkpoints and the shared sync
        lock stays wedged (the 2026-09 starvation).

        The id list here is long enough that ``mutate()``'s 400-char truncation drops the
        trailing "do not exist.", which is exactly what production saw."""
        import json

        ids = [f"gid://shopify/MediaImage/34109{n:09d}" for n in range(8)]
        errs = [{"field": ["files"],
                 "message": f"File ids {json.dumps(ids)} do not exist."}]
        truncated = f"fileUpdate: {json.dumps(errs)[:400]}"
        self.assertNotIn("do not exist", truncated)  # the phrase really is gone

        class GoneClient(FakeClient):
            def mutate(self, query, variables, name):
                self.calls.append(name)
                self.variables[name] = variables
                if name == "fileUpdate":
                    raise RuntimeError(truncated)
                if name == "productSet":
                    return {"product": {"id": "gid://Product/1"}}
                return {}

        client = GoneClient()
        publisher = self._publisher(client, lambda part, alt_for: [{"originalSource": "x"}])
        publisher.publish(self._part(), refresh_images=True)
        self.assertIn("fileUpdate", client.calls)

    def test_a_real_detach_refusal_still_raises(self):
        """Only an unresolvable-id error is tolerated; any other userError is a real
        problem and must still stop this part from checkpointing."""

        class RefusingClient(FakeClient):
            def mutate(self, query, variables, name):
                self.calls.append(name)
                self.variables[name] = variables
                if name == "fileUpdate":
                    raise RuntimeError(
                        'fileUpdate: [{"field": ["files"], "message": '
                        '"Access denied for fileUpdate."}]')
                if name == "productSet":
                    return {"product": {"id": "gid://Product/1"}}
                return {}

        client = RefusingClient()
        publisher = self._publisher(client, lambda part, alt_for: [{"originalSource": "x"}])
        with self.assertRaises(RuntimeError):
            publisher.publish(self._part(), refresh_images=True)

    def test_a_retained_shared_file_is_not_detached_or_globally_deleted(self):
        client = FakeClient()
        publisher = self._publisher(
            client,
            lambda part, alt_for: [{"id": "gid://File/old"},
                                   {"id": "gid://File/new"}],
        )
        publisher.publish(self._part(), refresh_images=True)
        self.assertNotIn("fileUpdate", client.calls)
        self.assertNotIn("fileDelete", client.calls)


if __name__ == "__main__":
    unittest.main()
