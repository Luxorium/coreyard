"""The unlisted title queue is bounded, resumable, and verifies portal writes."""

import copy
import unittest
from unittest.mock import patch

from coreyard.ebay import auto_titles, titles, workflow
from tests.test_ebay_daily import STORE, part, row


class Client:
    def __init__(self, rows, discard=False):
        self.rows = copy.deepcopy(rows)
        self.calls = []
        self.discard = discard

    def iter_listings(self, *, tab, rows, part_type):
        assert tab == "unlisted"
        return iter(copy.deepcopy(self.rows))

    def bulk_update(self, changes, ids, *, tab):
        self.calls.append((changes, ids, tab))
        assert tab == "unlisted"
        assert changes[0]["field"] == "title"
        if not self.discard:
            for item in self.rows:
                if item["listing_id"] in ids:
                    item["title"] = changes[0]["value"]
        return ""


class Queue(unittest.TestCase):
    def test_variant_identifiers_cannot_disappear_during_shortening(self):
        lost = auto_titles.lost_identifiers(
            "Classic Style 5 Lug Wheel LED VIN E 3.5L", "Tail Lamp")
        self.assertEqual(lost, ["Classic", "5 Lug", "LED", "VIN E", "3.5L"])
        self.assertEqual(auto_titles.lost_identifiers("VIN E 3.5L LED", "LED 3.5L VIN E"), [])

    def test_new_listings_precede_retries_and_batch_is_bounded(self):
        rows = [row("L1"), row("L2")]
        chosen = auto_titles.pending(rows, {"L1": part(), "L2": part()},
                                     {"L1": {"attempted": 100}}, "v", 200, 1)
        self.assertEqual([key for key, _ in chosen], ["L2"])

    def test_verified_input_is_skipped_until_source_title_or_renderer_changes(self):
        item, source = row(), part()
        stamp = auto_titles.signature(item, source, "v1")
        state = {"L1": {"signature": stamp, "checked": 100}}
        self.assertEqual(auto_titles.pending([item], {"L1": source}, state,
                                             "v1", 200, 10), [])
        for changed, rev in (({**item, "title": "manual edit"}, "v1"), (item, "v2")):
            self.assertEqual(len(auto_titles.pending([changed], {"L1": source},
                                                     state, rev, 200, 10)), 1)
        source.side = "Right"
        self.assertEqual(len(auto_titles.pending([item], {"L1": source}, state,
                                                 "v1", 200, 10)), 1)

    def test_fitment_is_rechecked_daily_even_without_source_row_change(self):
        item, source = row(), part()
        state = {"L1": {"signature": auto_titles.signature(item, source, "v"),
                        "checked": 100}}
        self.assertEqual(len(auto_titles.pending([item], {"L1": source}, state,
                                                 "v", 90000, 10)), 1)

    def test_new_arrivals_precede_the_untouched_initial_backlog(self):
        rows = [row("L1"), row("L2")]
        state = {"L1": {"seen": 100}}
        chosen = auto_titles.pending(rows, {"L1": part(), "L2": part()},
                                     state, "v", 200, 1)
        self.assertEqual([key for key, _ in chosen], ["L2"])


class Saves(unittest.TestCase):
    def run_batch(self, client, state, apply=True, rows=None, cap=10, batch_size=10):
        return auto_titles.run_batch(
            client, rows or [row()], [part()], STORE, state, batch_size=batch_size,
            cap=cap, apply=apply, attach=lambda parts: None,
            checkpoint=lambda value: None, now=1000, renderer_revision="v")

    def test_dry_run_changes_neither_portal_nor_progress(self):
        client, state = Client([row()]), {}
        result = self.run_batch(client, state, apply=False)
        self.assertEqual(len(result["titles"]), 1)
        self.assertEqual(client.calls, [])
        self.assertEqual(state, {})

    def test_success_is_verified_and_the_next_pass_writes_nothing(self):
        client, state = Client([row()]), {}
        result = self.run_batch(client, state)
        self.assertEqual(result["verified"], 1)
        self.assertEqual(len(client.calls), 1)
        again = self.run_batch(client, state, rows=client.rows)
        self.assertEqual(again["selected"], 0)
        self.assertEqual(len(client.calls), 1)
        self.assertLessEqual(len(client.rows[0]["title"]), 80)

    def test_silent_discard_is_a_failure_and_never_marked_checked(self):
        client, state = Client([row()], discard=True), {}
        result = self.run_batch(client, state)
        self.assertEqual(result["failed"], 1)
        self.assertNotIn("checked", state["L1"])

    def test_listing_that_left_unlisted_cannot_be_written(self):
        client, state = Client([]), {}
        self.run_batch(client, state)
        self.assertEqual(client.calls, [])
        self.assertNotIn("checked", state["L1"])

    def test_concurrent_title_edit_is_preserved(self):
        client, state = Client([row(title="staff edited this")]), {}
        self.run_batch(client, state)
        self.assertEqual(client.calls, [])

    def test_cap_refuses_before_first_write(self):
        client, state = Client([row()]), {}
        with self.assertRaises(workflow.ApplyGuard):
            self.run_batch(client, state, batch_size=11, cap=10)
        self.assertEqual(client.calls, [])
        self.assertEqual(state, {})

    def test_batch_changes_only_selected_ids_and_next_pass_advances(self):
        rows = [row("L1"), row("L2")]
        client, state = Client(rows), {}
        first = self.run_batch(client, state, rows=rows, batch_size=1, cap=1)
        self.assertEqual(first["saved"], 1)
        self.assertEqual(client.calls[0][1], ["L1"])
        second = self.run_batch(client, state, rows=client.rows, batch_size=1, cap=1)
        self.assertEqual(second["saved"], 1)
        self.assertEqual(client.calls[1][1], ["L2"])

    def test_title_collision_with_unchanged_listing_is_held(self):
        target = titles.render_title(part(), STORE)[0]
        rows = [row(), row("OTHER", stock="999", title=target, interchange="different")]
        client, state = Client(rows), {}
        result = self.run_batch(client, state, rows=rows)
        self.assertEqual(client.calls, [])
        self.assertTrue(any("collides" in item["reason"] for item in result["held"]))

    def test_truncated_subject_is_held(self):
        client, state = Client([row()]), {}
        with patch.object(titles, "render_title", return_value=("2014 Ford", ["truncated"])):
            result = self.run_batch(client, state)
        self.assertEqual(client.calls, [])
        self.assertTrue(any("truncated" in item["reason"] for item in result["held"]))
