"""What a full run is allowed to record as published.

The scoped and delta paths were careful about this from the start: they record only what
landed, because recording more would mark a part up to date that nothing had published. The
full path was not. It committed everything it extracted, so a part that failed to publish
was written at its *new* fingerprint and the next run found it unchanged — the change lost
silently and permanently, which is the one failure a sync must not have.
"""

import unittest

from coreyard.run_sync import _committable
from coreyard.state import DiffResult


def diff(added=(), changed=(), removed=()):
    return DiffResult(added=list(added), changed=list(changed), removed=list(removed))


class FailedPublishes(unittest.TestCase):
    def test_a_changed_part_that_failed_keeps_the_version_the_store_has(self):
        snapshot, _, outstanding = _committable(
            current={"1": "new", "2": "new2"}, images={},
            previous={"1": "old", "2": "old2"}, previous_images={},
            diff=diff(changed=["1", "2"]), published_ok={"2"},
        )
        self.assertEqual(snapshot["1"], "old", "a failed publish was recorded as done")
        self.assertEqual(snapshot["2"], "new2")
        self.assertEqual(outstanding, {"1"})

    def test_the_next_run_therefore_still_sees_it_as_changed(self):
        """The whole point: the retry has to happen."""
        snapshot, _, _ = _committable(
            current={"1": "new"}, images={}, previous={"1": "old"}, previous_images={},
            diff=diff(changed=["1"]), published_ok=set(),
        )
        self.assertNotEqual(snapshot["1"], "new")

    def test_a_new_part_that_failed_stays_absent_so_it_still_reads_as_new(self):
        snapshot, _, outstanding = _committable(
            current={"1": "new"}, images={}, previous={}, previous_images={},
            diff=diff(added=["1"]), published_ok=set(),
        )
        self.assertNotIn("1", snapshot)
        self.assertEqual(outstanding, {"1"})

    def test_a_successful_run_records_everything(self):
        snapshot, _, outstanding = _committable(
            current={"1": "new", "2": "new2"}, images={},
            previous={"1": "old"}, previous_images={},
            diff=diff(added=["2"], changed=["1"]), published_ok={"1", "2"},
        )
        self.assertEqual(snapshot, {"1": "new", "2": "new2"})
        self.assertEqual(outstanding, set())

    def test_unchanged_parts_are_untouched_by_any_of_this(self):
        """A part nobody tried to publish is not a failure; it is already correct."""
        snapshot, _, outstanding = _committable(
            current={"1": "same"}, images={}, previous={"1": "same"}, previous_images={},
            diff=diff(), published_ok=set(),
        )
        self.assertEqual(snapshot, {"1": "same"})
        self.assertEqual(outstanding, set())


class PhotoManifests(unittest.TestCase):
    """The photo manifest has to move with the fingerprint or the next run mis-reads it."""

    def test_a_failed_part_keeps_its_old_manifest(self):
        _, manifests, _ = _committable(
            current={"1": "new"}, images={"1": "img-new"},
            previous={"1": "old"}, previous_images={"1": "img-old"},
            diff=diff(changed=["1"]), published_ok=set(),
        )
        self.assertEqual(manifests["1"], "img-old")

    def test_a_failed_new_part_carries_no_manifest(self):
        _, manifests, _ = _committable(
            current={"1": "new"}, images={"1": "img-new"},
            previous={}, previous_images={},
            diff=diff(added=["1"]), published_ok=set(),
        )
        self.assertNotIn("1", manifests)

    def test_a_published_part_takes_the_new_manifest(self):
        _, manifests, _ = _committable(
            current={"1": "new"}, images={"1": "img-new"},
            previous={"1": "old"}, previous_images={"1": "img-old"},
            diff=diff(changed=["1"]), published_ok={"1"},
        )
        self.assertEqual(manifests["1"], "img-new")


class Removals(unittest.TestCase):
    def test_removed_parts_are_not_treated_as_outstanding(self):
        """Retirement has its own path; a removal is not a failed publish."""
        snapshot, _, outstanding = _committable(
            current={}, images={}, previous={"9": "old"}, previous_images={},
            diff=diff(removed=["9"]), published_ok=set(),
        )
        self.assertEqual(outstanding, set())
        self.assertEqual(snapshot, {})
