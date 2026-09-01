"""What each channel actually holds, as opposed to what the yard says.

The canonical snapshot answers "has this part been published", which was a complete
question while there was one storefront. With two, one channel succeeding while the other
fails is the normal case, and a snapshot that cannot express it marks a part synchronised
because Shopify took it while the listing channel never did — so the part is never retried
and nobody finds out until a buyer does not.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.state import Fingerprints, SyncState

CURRENT = {"51": "aaa", "77": "bbb", "90": "ccc"}


class ChannelState(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = SyncState(Path(self.tmp.name) / "state.sqlite3")
        self.addCleanup(self.state.close)


class Pending(ChannelState):
    def test_a_channel_that_holds_nothing_owes_everything(self):
        self.assertEqual(self.state.channel_pending("ebay", CURRENT), ["51", "77", "90"])

    def test_what_it_holds_at_the_current_fingerprint_is_not_pending(self):
        self.state.record_channel("ebay", CURRENT)
        self.assertEqual(self.state.channel_pending("ebay", CURRENT), [])

    def test_channels_are_independent(self):
        """The bug: Shopify succeeding must not settle the listing channel."""
        self.state.record_channel("shopify", CURRENT)
        self.state.record_channel("ebay", {"51": "aaa"})
        self.assertEqual(self.state.channel_pending("shopify", CURRENT), [])
        self.assertEqual(self.state.channel_pending("ebay", CURRENT), ["77", "90"])

    def test_a_changed_part_goes_pending_again(self):
        self.state.record_channel("ebay", CURRENT)
        moved = dict(CURRENT, **{"51": "aaa2"})
        self.assertEqual(self.state.channel_pending("ebay", moved), ["51"])

    def test_it_accepts_a_fingerprint_bundle(self):
        self.state.record_channel("ebay", Fingerprints(content=CURRENT))
        self.assertEqual(self.state.channel_pending("ebay", Fingerprints(content=CURRENT)),
                         [])

    def test_recording_nothing_is_not_an_error(self):
        self.state.record_channel("ebay", {})
        self.assertEqual(self.state.channels(), [])


class Failures(ChannelState):
    def test_a_failure_does_not_advance_the_fingerprint(self):
        """The crux. A failed publish must stay pending or it is never retried."""
        self.state.record_channel_failure("ebay", "77", "no aspect for VIN code")
        self.assertIn("77", self.state.channel_pending("ebay", CURRENT))
        self.assertEqual(self.state.channel_fingerprints("ebay"), {})

    def test_a_failure_is_reported_with_its_reason(self):
        self.state.record_channel_failure("ebay", "77", "no aspect for VIN code")
        self.assertEqual(self.state.channel_failures("ebay"),
                         {"77": "no aspect for VIN code"})

    def test_a_failure_after_a_success_keeps_the_version_already_held(self):
        """The channel still holds the old version; it just could not take the new one."""
        self.state.record_channel("ebay", {"51": "aaa"})
        self.state.record_channel_failure("ebay", "51", "portal rejected the title")
        self.assertEqual(self.state.channel_fingerprints("ebay"), {"51": "aaa"})
        self.assertEqual(self.state.channel_pending("ebay", {"51": "aaa2"}), ["51"])

    def test_a_later_success_clears_the_error(self):
        self.state.record_channel_failure("ebay", "77", "transient")
        self.state.record_channel("ebay", {"77": "bbb"})
        self.assertEqual(self.state.channel_failures("ebay"), {})
        self.assertNotIn("77", self.state.channel_pending("ebay", CURRENT))

    def test_a_failure_on_one_channel_does_not_mark_another(self):
        self.state.record_channel("shopify", CURRENT)
        self.state.record_channel_failure("ebay", "77", "boom")
        self.assertEqual(self.state.channel_failures("shopify"), {})


class RemoteIdentity(ChannelState):
    def test_a_channels_own_id_is_kept(self):
        self.state.record_channel("ebay", {"51": "aaa"}, remote_ids={"51": "offer-9"})
        self.assertEqual(self.state.channel_remote_ids("ebay"), {"51": "offer-9"})

    def test_a_later_write_without_an_id_does_not_erase_it(self):
        """A price-only update should not lose the listing id it was applied to."""
        self.state.record_channel("ebay", {"51": "aaa"}, remote_ids={"51": "offer-9"})
        self.state.record_channel("ebay", {"51": "aaa2"})
        self.assertEqual(self.state.channel_remote_ids("ebay"), {"51": "offer-9"})

    def test_status_is_preserved_the_same_way(self):
        self.state.record_channel("shopify", {"51": "aaa"}, status="ACTIVE")
        self.state.record_channel("shopify", {"51": "aaa2"})
        rows = dict(self.state.conn.execute(
            "SELECT r_number, status FROM channel_state WHERE channel='shopify'"))
        self.assertEqual(rows["51"], "ACTIVE")

    def test_ids_are_per_channel(self):
        self.state.record_channel("shopify", {"51": "aaa"}, remote_ids={"51": "gid://1"})
        self.state.record_channel("ebay", {"51": "aaa"}, remote_ids={"51": "offer-9"})
        self.assertEqual(self.state.channel_remote_ids("shopify"), {"51": "gid://1"})
        self.assertEqual(self.state.channel_remote_ids("ebay"), {"51": "offer-9"})


class Housekeeping(ChannelState):
    def test_channels_lists_what_has_been_heard_from(self):
        self.state.record_channel("shopify", {"51": "aaa"})
        self.state.record_channel("ebay", {"51": "aaa"})
        self.assertEqual(self.state.channels(), ["ebay", "shopify"])

    def test_forgetting_is_per_channel(self):
        self.state.record_channel("shopify", CURRENT)
        self.state.record_channel("ebay", CURRENT)
        self.state.forget_channel("ebay", ["51"])
        self.assertEqual(self.state.channel_pending("ebay", CURRENT), ["51"])
        self.assertEqual(self.state.channel_pending("shopify", CURRENT), [])

    def test_the_summary_reads_as_a_status_line(self):
        self.state.record_channel("ebay", {"51": "aaa"})
        self.state.record_channel_failure("ebay", "77", "boom")
        self.assertEqual(self.state.channel_summary("ebay", CURRENT),
                         "1 held, 2 pending, 1 failing")

    def test_the_summary_without_a_current_set_reports_only_what_is_held(self):
        self.state.record_channel("ebay", {"51": "aaa"})
        self.assertEqual(self.state.channel_summary("ebay"), "1 held")


class Compatibility(ChannelState):
    """The canonical snapshot keeps its exact meaning; this table is additive."""

    def test_recording_a_channel_does_not_touch_the_snapshot(self):
        self.state.record_channel("ebay", CURRENT)
        self.assertEqual(self.state.load(), {})

    def test_the_snapshot_still_works_untouched(self):
        self.state.commit(CURRENT)
        self.assertEqual(self.state.load(), CURRENT)
        self.assertEqual(self.state.channels(), [])

    def test_an_older_snapshot_gains_the_table_on_open(self):
        """A live database predating channels must open and be empty of them, not fail."""
        path = Path(self.tmp.name) / "old.sqlite3"
        with SyncState(path) as first:
            first.commit(CURRENT)
        with SyncState(path) as again:
            self.assertEqual(again.channels(), [])
            self.assertEqual(again.load(), CURRENT)
