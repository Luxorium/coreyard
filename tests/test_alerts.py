"""Alerting: notify once, notify on recovery, and never for a non-problem.

OPS-03 asks for a *tested* way to notify the operator — delivery and recovery exercised,
not merely described in a log. The conditions come from the specification: a full sync
stale for two scheduled intervals, deltas stale for 15 minutes, an order stage failed or
pending for 10 minutes, and storage or credentials preventing progress.

Three failure modes are worth more than the happy path, and each has tests here:

* **Paging every tick.** An alert that repeats every five minutes gets filtered into a
  folder nobody opens, and then everyone believes they are covered.
* **Never saying it recovered.** An operator who only hears about breakage has to go and
  check whether their fix worked.
* **Staying quiet for a real outage.** The subtle one. The delta check is suppressed while
  a full sync holds the lock, which is right — but on a host whose hourly full sync takes
  most of an hour, a sync is almost always running, so unbounded suppression would explain
  a cursor frozen for five days as readily as one frozen for six minutes.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from coreyard import alerts, ops
from coreyard.alerts import FAIL, WARN, Alert
from coreyard.capabilities import MISSING, OFF, ON, Capabilities, Capability


def caps(**states) -> Capabilities:
    items = {name: Capability(name, state, f"{name} is {state}")
             for name, state in states.items()}
    return Capabilities(source_kind="database", source_argument="", items=items)


def ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


class StateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()


class NotifyOnceThenRemind(StateFile):
    def setUp(self):
        super().setUp()
        self.sent = []
        patcher = mock.patch.object(
            alerts, "deliver",
            lambda alert, state="firing": (self.sent.append((alert.key, state)), (True, "ok"))[1])
        patcher.start()
        self.addCleanup(patcher.stop)
        journal = mock.patch.object(alerts, "journal", lambda *a, **k: None)
        journal.start()
        self.addCleanup(journal.stop)

    def test_a_new_condition_is_delivered(self):
        alert = Alert("sync.delta.stale", FAIL, "frozen")
        result = alerts.reconcile([alert], db=self.db)
        self.assertEqual(self.sent, [("sync.delta.stale", "firing")])
        self.assertEqual(result["new"], ["sync.delta.stale"])

    def test_the_same_condition_is_not_delivered_again_on_the_next_tick(self):
        """A five-minute schedule must not mean a five-minute page."""
        alert = Alert("sync.delta.stale", FAIL, "frozen")
        alerts.reconcile([alert], db=self.db)
        self.sent.clear()
        for _ in range(5):
            alerts.reconcile([alert], db=self.db)
        self.assertEqual(self.sent, [])

    def test_a_condition_that_persists_is_repeated_after_the_reminder_interval(self):
        alert = Alert("sync.delta.stale", FAIL, "frozen")
        alerts.reconcile([alert], db=self.db)
        self.sent.clear()
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE alerts SET notified_at = ?", (ago(hours=9),))
        conn.close()
        result = alerts.reconcile([alert], db=self.db, repeat=timedelta(hours=6))
        self.assertEqual(self.sent, [("sync.delta.stale", "firing")])
        self.assertEqual(result["repeated"], ["sync.delta.stale"])

    def test_recovery_is_delivered_when_the_condition_clears(self):
        alerts.reconcile([Alert("orders.stuck", FAIL, "one stuck")], db=self.db)
        self.sent.clear()
        result = alerts.reconcile([], db=self.db)
        self.assertEqual(self.sent, [("orders.stuck", "resolved")])
        self.assertEqual(result["resolved"], ["orders.stuck"])

    def test_a_resolved_condition_is_forgotten_so_it_recovers_only_once(self):
        alerts.reconcile([Alert("orders.stuck", FAIL, "one stuck")], db=self.db)
        alerts.reconcile([], db=self.db)
        self.sent.clear()
        alerts.reconcile([], db=self.db)
        self.assertEqual(self.sent, [])

    def test_a_failed_delivery_is_retried_next_tick(self):
        """Recording a page that never arrived as sent is how an outage goes unheard."""
        with mock.patch.object(alerts, "deliver", lambda a, s="firing": (False, "no route")):
            first = alerts.reconcile([Alert("orders.stuck", FAIL, "x")], db=self.db)
        self.assertEqual(first["failed"], ["orders.stuck: no route"])
        second = alerts.reconcile([Alert("orders.stuck", FAIL, "x")], db=self.db)
        self.assertEqual(self.sent, [("orders.stuck", "firing")])
        self.assertEqual(second["repeated"], ["orders.stuck"])

    def test_a_breakage_nobody_was_told_about_sends_no_recovery(self):
        """A recovery notice for something never announced reads as a fresh incident."""
        with mock.patch.object(alerts, "deliver", lambda a, s="firing": (False, "no route")):
            alerts.reconcile([Alert("orders.stuck", FAIL, "x")], db=self.db)
        self.sent.clear()
        alerts.reconcile([], db=self.db)
        self.assertEqual(self.sent, [])


class DeltaSuppressionIsBounded(unittest.TestCase):
    """The mirror of a check that stays lit: a check that stays quiet."""

    def cursor(self, iso):
        state = mock.MagicMock()
        state.__enter__ = lambda s: s
        state.__exit__ = lambda *a: None
        state.get_cursor.return_value = iso
        return mock.patch("coreyard.state.SyncState", return_value=state)

    def test_a_fresh_cursor_says_nothing(self):
        with self.cursor(ago(minutes=6)):
            self.assertEqual(alerts.check_delta(caps(delta=ON), {"sync": 3600}), [])

    def test_a_running_full_sync_explains_a_recently_aged_cursor(self):
        with self.cursor(ago(minutes=40)), \
                mock.patch.object(ops, "running", return_value=True):
            self.assertEqual(alerts.check_delta(caps(delta=ON), {"sync": 3600}), [])

    def test_a_running_full_sync_does_not_explain_five_days(self):
        with self.cursor(ago(days=5)), \
                mock.patch.object(ops, "running", return_value=True):
            found = alerts.check_delta(caps(delta=ON), {"sync": 3600})
        self.assertEqual([a.key for a in found], ["sync.delta.stale"])
        self.assertEqual(found[0].level, FAIL)

    def test_a_stale_cursor_with_no_sync_running_alerts(self):
        with self.cursor(ago(hours=2)), \
                mock.patch.object(ops, "running", return_value=False):
            found = alerts.check_delta(caps(delta=ON), {"sync": 3600})
        self.assertEqual([a.key for a in found], ["sync.delta.stale"])

    def test_an_installation_without_deltas_is_never_behind_on_them(self):
        with self.cursor(ago(days=30)):
            self.assertEqual(alerts.check_delta(caps(delta=OFF), {"sync": 3600}), [])


class NothingScheduledIsNotLate(unittest.TestCase):
    def test_no_sync_schedule_means_no_staleness_alert(self):
        """Alerting on a job the operator deliberately paused teaches them to ignore this."""
        with mock.patch.object(ops, "last", return_value={
                "finished": ago(days=9), "started": ago(days=9), "ok": True, "counts": {}}):
            self.assertEqual(alerts.check_sync({}), [])

    def test_a_sync_late_by_two_of_its_own_intervals_alerts(self):
        with mock.patch.object(ops, "last", return_value={
                "finished": ago(hours=3), "started": ago(hours=3), "ok": True,
                "counts": {}}), \
                mock.patch.object(ops, "running", return_value=False):
            found = alerts.check_sync({"sync": 3600})
        self.assertEqual([a.key for a in found], ["sync.full.stale"])

    def test_a_sync_inside_two_intervals_says_nothing(self):
        with mock.patch.object(ops, "last", return_value={
                "finished": ago(minutes=90), "started": ago(minutes=90), "ok": True,
                "counts": {}}), \
                mock.patch.object(ops, "running", return_value=False):
            self.assertEqual(alerts.check_sync({"sync": 3600}), [])

    def test_a_sync_that_has_never_run_is_a_setup_state_not_an_outage(self):
        with mock.patch.object(ops, "last", return_value=None):
            self.assertEqual(alerts.check_sync({"sync": 3600}), [])


class RunsThatEndBadly(unittest.TestCase):
    """The failure a staleness check cannot see, because the job *is* running."""

    def test_a_timing_out_sync_is_reported(self):
        with mock.patch.object(ops, "last", return_value={
                "ok": True, "counts": {"timed_out": True}, "exit_code": 124}):
            found = alerts.check_sync_outcome()
        self.assertEqual([a.key for a in found], ["sync.full.timeout"])
        self.assertEqual(found[0].level, WARN)

    def test_a_failing_sync_is_reported_with_its_exit_code(self):
        with mock.patch.object(ops, "last", return_value={
                "ok": False, "counts": {}, "exit_code": 2}):
            found = alerts.check_sync_outcome()
        self.assertEqual([a.key for a in found], ["sync.full.failed"])
        self.assertIn("exit 2", found[0].summary)

    def test_a_clean_sync_says_nothing(self):
        with mock.patch.object(ops, "last", return_value={
                "ok": True, "counts": {"created": 3}, "exit_code": 0}):
            self.assertEqual(alerts.check_sync_outcome(), [])


class ConfigurationAndStorage(unittest.TestCase):
    def test_only_a_missing_capability_alerts_never_one_that_is_merely_off(self):
        self.assertEqual(alerts.check_credentials(caps(portal=OFF, orders=OFF)), [])
        found = alerts.check_credentials(caps(portal=OFF, shopify=MISSING))
        self.assertEqual([a.key for a in found], ["config.missing"])
        self.assertIn("shopify", found[0].summary)

    def test_an_unwritable_workspace_is_reported(self):
        with tempfile.TemporaryDirectory() as base:
            blocked = Path(base) / "out"
            blocked.mkdir()
            os.chmod(blocked, 0o555)
            try:
                with mock.patch.object(alerts, "out_dir", lambda: blocked):
                    found = alerts.check_storage()
            finally:
                os.chmod(blocked, 0o755)
        self.assertIn("storage.unwritable", [a.key for a in found])


class DeliveryIsRealAndSafe(unittest.TestCase):
    def test_an_alert_reaches_the_configured_command(self):
        """OPS-03: delivery must be exercised, not described."""
        with tempfile.TemporaryDirectory() as base:
            sink = Path(base) / "paged.txt"
            with mock.patch.dict(os.environ,
                                 {"COREYARD_ALERT_COMMAND": f"cat > {sink}"}, clear=False):
                delivered, note = alerts.deliver(
                    Alert("orders.stuck", FAIL, "one stuck", "detail here"))
            self.assertTrue(delivered, note)
            body = sink.read_text(encoding="utf-8")
        self.assertIn("[FAIL] coreyard orders.stuck", body)
        self.assertIn("one stuck", body)

    def test_the_notifier_is_told_which_alert_and_whether_it_recovered(self):
        with tempfile.TemporaryDirectory() as base:
            sink = Path(base) / "env.txt"
            command = ("printf '%s %s %s' \"$COREYARD_ALERT_KEY\" "
                       f"\"$COREYARD_ALERT_LEVEL\" \"$COREYARD_ALERT_STATE\" > {sink}")
            with mock.patch.dict(os.environ,
                                 {"COREYARD_ALERT_COMMAND": command}, clear=False):
                alerts.deliver(Alert("sync.delta.stale", FAIL, "x"), "resolved")
            self.assertEqual(sink.read_text().strip(), "sync.delta.stale FAIL resolved")

    def test_no_notifier_configured_is_reported_rather_than_silently_succeeding(self):
        environ = {k: v for k, v in os.environ.items() if k != "COREYARD_ALERT_COMMAND"}
        with mock.patch.dict(os.environ, environ, clear=True):
            delivered, note = alerts.deliver(Alert("x", WARN, "y"))
        self.assertFalse(delivered)
        self.assertIn("COREYARD_ALERT_COMMAND", note)

    def test_a_notifier_that_fails_is_not_counted_as_delivered(self):
        with mock.patch.dict(os.environ, {"COREYARD_ALERT_COMMAND": "exit 3"}, clear=False):
            delivered, note = alerts.deliver(Alert("x", WARN, "y"))
        self.assertFalse(delivered)
        self.assertIn("exited 3", note)

    def test_the_notifier_stdout_is_never_repeated_back(self):
        """It may echo a URL carrying a token."""
        with mock.patch.dict(
                os.environ,
                {"COREYARD_ALERT_COMMAND": "echo https://hooks.example/T0K3N; exit 1"},
                clear=False):
            _, note = alerts.deliver(Alert("x", WARN, "y"))
        self.assertNotIn("T0K3N", note)

    def test_a_wedged_notifier_cannot_hold_the_tick_open(self):
        with mock.patch.object(alerts, "DELIVERY_TIMEOUT", 1), \
                mock.patch.dict(os.environ,
                                {"COREYARD_ALERT_COMMAND": "sleep 30"}, clear=False):
            delivered, note = alerts.deliver(Alert("x", WARN, "y"))
        self.assertFalse(delivered)
        self.assertIn("did not return", note)


class AlertsCarryNoCustomerData(unittest.TestCase):
    def test_a_stuck_order_alert_names_the_order_but_carries_no_payload(self):
        from coreyard.orders.pipeline import EventQueue

        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "queue.sqlite3"
            with EventQueue(path) as queue:
                queue.add("d1", "orders/paid",
                          json.dumps({"name": "#1012", "email": "buyer@example.com",
                                      "shipping_address": {"address1": "9 Elm St"}}
                                     ).encode(), order_key="1012")
                conn = queue.conn
                with conn:
                    conn.execute("UPDATE events SET received_at = ?, order_name = '#1012'",
                                 (ago(hours=2),))
                stuck = queue.stuck(datetime.now(timezone.utc) - timedelta(minutes=10))
            self.assertEqual(len(stuck), 1)
            blob = json.dumps(stuck)
            self.assertIn("#1012", blob)
            for private in ("buyer@example.com", "Elm St"):
                self.assertNotIn(private, blob)

            with mock.patch("coreyard.orders.pipeline.QUEUE_DB", path):
                found = alerts.check_orders(caps(orders=ON))
        self.assertEqual([a.key for a in found], ["orders.stuck"])
        rendered = found[0].message()
        for private in ("buyer@example.com", "Elm St"):
            self.assertNotIn(private, rendered)

    def test_no_queue_means_no_order_alerts(self):
        with mock.patch("coreyard.orders.pipeline.QUEUE_DB",
                        Path("/nonexistent/queue.sqlite3")):
            self.assertEqual(alerts.check_orders(caps(orders=OFF)), [])


class EvaluationNeverRaises(unittest.TestCase):
    def test_one_broken_probe_does_not_silence_the_others(self):
        """Going quiet for every condition because one probe failed is the worst
        possible moment to go quiet."""
        with mock.patch.object(alerts, "check_orders", side_effect=RuntimeError("boom")), \
                mock.patch.object(alerts, "check_storage", return_value=[]), \
                mock.patch.object(alerts, "check_credentials", return_value=[]), \
                mock.patch.object(alerts, "check_sync", return_value=[]), \
                mock.patch.object(alerts, "check_sync_outcome",
                                  return_value=[Alert("sync.full.failed", FAIL, "x")]), \
                mock.patch.object(alerts, "check_delta", return_value=[]):
            found = alerts.evaluate(caps=caps(), intervals={})
        keys = [a.key for a in found]
        self.assertIn("alerts.check_failed", keys)
        self.assertIn("sync.full.failed", keys)


if __name__ == "__main__":
    unittest.main()
