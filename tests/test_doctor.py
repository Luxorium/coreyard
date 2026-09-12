"""Doctor checks that must speak up, and must stay quiet when nothing is wrong.

`check_unpublished` exists because of a real outage: reconciliation activated the catalogue
before publishing it to a sales channel, leaving 12,900 products ACTIVE, in stock, priced
and returning 404. Every count in every report included them, because status is what the
reports look at and status was fine. The one check that could see it lived behind
`status --deep`, which pages the whole store and so is never run casually.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coreyard import capabilities, doctor
from coreyard.doctor import FAIL, OK, WARN, check_unpublished


class FakeClient:
    """A Shopify client that answers one productsCount query."""

    def __init__(self, count=0, precision="EXACT", raises=None):
        self.count, self.precision, self.raises = count, precision, raises
        self.queries: list[str] = []

    def graphql(self, query, variables=None, tries=5):
        if self.raises:
            raise self.raises
        self.queries.append((variables or {}).get("q", ""))
        return {"productsCount": {"count": self.count, "precision": self.precision}}


class Unpublished(unittest.TestCase):
    def test_it_says_nothing_alarming_when_every_active_product_is_on_a_channel(self):
        (level, name, detail), = check_unpublished(FakeClient(count=0))
        self.assertEqual(level, OK)
        self.assertEqual(name, "unpublished")

    def test_one_unpublished_active_product_is_a_failure(self):
        (level, _, detail), = check_unpublished(FakeClient(count=1))
        self.assertEqual(level, FAIL)
        self.assertIn("1 ACTIVE", detail)
        self.assertIn("404", detail)

    def test_a_capped_count_is_reported_as_a_lower_bound(self):
        (level, _, detail), = check_unpublished(
            FakeClient(count=10000, precision="AT_LEAST"))
        self.assertEqual(level, FAIL)
        self.assertIn("at least 10000", detail)

    def test_it_asks_only_about_active_products_missing_a_channel(self):
        client = FakeClient(count=0)
        check_unpublished(client)
        self.assertEqual(client.queries,
                         ["status:active AND published_status:unpublished"])

    def test_an_unreachable_store_warns_rather_than_failing_the_run(self):
        (level, name, _), = check_unpublished(FakeClient(raises=RuntimeError("boom")))
        self.assertEqual(level, WARN)
        self.assertEqual(name, "unpublished")


def caps_for(**environ):
    """Capabilities for a synthetic installation, never for the maintainer's own."""
    with mock.patch.dict(os.environ, environ, clear=True):
        return capabilities.detect(load=False)


def levels(results):
    return {name: level for level, name, _ in results}


class TabularInstallationIsNotABrokenOne(unittest.TestCase):
    """A file-backed yard used to be diagnosed as three failures and a wrong instruction.

    `doctor` asked for SMB credentials it does not need, `smbclient` it never shells out to
    and a `schema.json` nothing would read, then told the operator to fix an installation
    that was already correct. The first thing a new customer runs must not misdiagnose them.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmp.name) / "parts.csv"
        self.csv.write_text("r_number,part_type,price\n51,TAIL LAMP,89.00\n",
                            encoding="utf-8")
        self.caps = caps_for(COREYARD_SOURCE=f"tabular:{self.csv}")

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_does_not_demand_smbclient(self):
        self.assertNotIn("smbclient", levels(doctor.check_environment(caps=self.caps)))

    def test_it_reports_no_failure_for_a_correct_file_backed_install(self):
        results = doctor.check_capabilities(caps=self.caps)
        self.assertNotIn(FAIL, [level for level, _, _ in results])

    def test_the_capability_section_names_the_source_it_found(self):
        detail = dict((name, text)
                      for _, name, text in doctor.check_capabilities(caps=self.caps))
        self.assertIn(str(self.csv), detail["source"])

    def test_capabilities_that_are_simply_off_are_collapsed_into_one_line(self):
        """Eleven lines listing what a site deliberately does not use is a report people
        stop reading."""
        results = doctor.check_capabilities(caps=self.caps)
        collapsed = [text for _, name, text in results if name == "not in use"]
        self.assertEqual(len(collapsed), 1)
        self.assertIn("schema", collapsed[0])

    def test_liveness_probes_the_configured_source_not_the_named_pipe(self):
        results = doctor.check_liveness(caps=self.caps)
        by_name = levels(results)
        self.assertEqual(by_name.get("source"), OK)
        self.assertNotIn("photo-share", by_name)
        self.assertNotIn("shopify", by_name,
                         "an unconfigured store must not be probed")

    def test_the_resolved_source_is_the_one_probed(self):
        """`source.load(None)` re-reads the environment, so a diagnostic that resolved a
        tabular source and then asked for "the source" was handed the database instead —
        and opened a named pipe the report had just said this installation does not use.
        Under a test runner that meant a unit test talking to a live SQL Server."""
        import coreyard.source as source_module

        seen = []
        real = source_module.load
        with mock.patch.object(source_module, "load",
                               lambda spec=None: seen.append(spec) or real(spec)):
            doctor.check_liveness(caps=self.caps)
        # Every consumer, not just the first: the age check asks the source a second
        # question, and asking *it* with `None` would reach the same wrong server.
        self.assertTrue(seen)
        self.assertEqual(set(seen), {f"tabular:{self.csv}"})

    def test_a_source_file_that_vanished_is_still_a_failure(self):
        self.csv.unlink()
        caps = caps_for(COREYARD_SOURCE=f"tabular:{self.csv}")
        self.assertEqual(levels(doctor.check_capabilities(caps=caps))["source"], FAIL)


class OptionalFeaturesAreNotFailures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmp.name) / "parts.csv"
        self.csv.write_text("r_number,price\n51,9.00\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_unconfigured_order_pipeline_reports_nothing_at_all(self):
        """Order receipt is opt-in. A missing orders.log for a site that never turned it
        on is not a stale poller, and calling it one asks the operator to fix a feature
        they deliberately declined.

        LOG_DIR is redirected because the maintainer's own checkout has an orders.log, and
        the behaviour under test is what a customer's install does.
        """
        caps = caps_for(COREYARD_SOURCE=f"tabular:{self.csv}")
        logs = Path(self.tmp.name) / "empty-logs"
        logs.mkdir()
        original = doctor.LOG_DIR
        try:
            doctor.LOG_DIR = logs
            self.assertEqual(doctor.check_orders(caps=caps), [])
        finally:
            doctor.LOG_DIR = original

    def test_a_configured_order_pipeline_with_no_log_warns_and_says_what_to_start(self):
        caps = caps_for(COREYARD_SOURCE=f"tabular:{self.csv}",
                        SHOPIFY_WEBHOOK_SECRET="s3cret")
        original = doctor.LOG_DIR
        try:
            doctor.LOG_DIR = Path(self.tmp.name) / "logs"
            (level, name, detail), = doctor.check_orders(caps=caps)
        finally:
            doctor.LOG_DIR = original
        self.assertEqual((level, name), (WARN, "orders"))
        self.assertIn("orders poll", detail)

    def test_a_polling_site_with_no_webhook_secret_is_still_watched(self):
        """Polling needs only the Admin API, so it turns the webhook capability on
        nowhere. Gating the staleness check on that capability would have stopped watching
        the transport that is hardest to notice failing: on a quiet day a healthy poller
        and a dead one leave identical state everywhere except the log."""
        import os
        import time

        caps = caps_for(COREYARD_SOURCE=f"tabular:{self.csv}")
        self.assertFalse(caps.enabled("orders"))
        logs = Path(self.tmp.name) / "logs"
        logs.mkdir()
        log = logs / "orders.log"
        log.write_text("0 order(s) in the window.\n", encoding="utf-8")
        stale = time.time() - doctor.ORDERS_STALE.total_seconds() - 600
        os.utime(log, (stale, stale))
        original = doctor.LOG_DIR
        try:
            doctor.LOG_DIR = logs
            (level, name, detail), = doctor.check_orders(caps=caps)
        finally:
            doctor.LOG_DIR = original
        self.assertEqual((level, name), (FAIL, "orders"))
        self.assertIn("not reaching the yard", detail)

    def test_publishing_checks_are_skipped_without_store_credentials(self):
        caps = caps_for(COREYARD_SOURCE=f"tabular:{self.csv}")
        self.assertEqual(doctor.check_publishing(caps=caps), [])

    def test_a_half_configured_store_is_a_failure_rather_than_a_skip(self):
        caps = caps_for(COREYARD_SOURCE=f"tabular:{self.csv}",
                        SHOPIFY_STORE="x.myshopify.com")
        self.assertEqual(levels(doctor.check_liveness(caps=caps))["shopify"], FAIL)


class StaleCursorIsNotExcusedForever(unittest.TestCase):
    """A running full sync explains an ageing cursor — for one cycle, not for five days.

    On a host whose hourly full sync takes most of an hour, a sync is almost always
    running. An unbounded excuse therefore reported a cursor frozen since last week as OK,
    which is the same defect as a check that stays lit, wearing the opposite face.
    """

    def test_the_grace_period_follows_the_installed_schedule(self):
        from datetime import timedelta

        with mock.patch("coreyard.schedule.installed_intervals",
                        return_value={"sync": 3600}):
            self.assertEqual(doctor._suppression_grace(),
                             timedelta(seconds=3600) + doctor.CURSOR_STALE)

    def test_it_falls_back_to_an_hour_when_nothing_is_scheduled(self):
        from datetime import timedelta

        with mock.patch("coreyard.schedule.installed_intervals", return_value={}):
            self.assertEqual(doctor._suppression_grace(),
                             timedelta(hours=1) + doctor.CURSOR_STALE)


class FreshInstall(unittest.TestCase):
    def test_no_sync_state_yet_is_a_warning_with_the_next_command(self):
        """Five minutes after `coreyard init` there is no state file. Telling a new
        customer their installation has FAILED is a wrong first impression and a wrong
        diagnosis."""
        tmp = tempfile.TemporaryDirectory()
        original = doctor.STATE_DB
        try:
            doctor.STATE_DB = Path(tmp.name) / "absent.sqlite3"
            (level, name, detail), = doctor.check_freshness(
                caps=caps_for(COREYARD_SOURCE="database", SMB_HOST="h",
                              SMB_USER="u", SMB_PASSWORD="p"))
        finally:
            doctor.STATE_DB = original
            tmp.cleanup()
        self.assertEqual((level, name), (WARN, "state"))
        self.assertIn("coreyard sync", detail)


if __name__ == "__main__":
    unittest.main()


class AFailedBookingIsNotASilentOne(unittest.TestCase):
    """The gap that let order #1012 wait 43 hours for somebody to look.

    The scheduled watcher only ever asked whether the *poller* was alive. It was — every ten
    minutes, on time, reporting the order in its overlap window — while the sale it could not
    book sat in the queue in `error`. A fresh log and an unbooked sale are indistinguishable
    unless something reads the queue, so `check_orders` and `check_order_queue` are separate
    checks and the health run does both.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "queue.sqlite3"

    def queue(self):
        from coreyard.orders.pipeline import EventQueue

        return EventQueue(self.db)

    def add(self, webhook_id, name, *, booking_error="", age_minutes=0):
        """One order through the real queue API, optionally backdated."""
        from datetime import datetime, timedelta, timezone

        with self.queue() as q:
            q.add(webhook_id, "orders/paid", b'{"name": "%s"}' % name.encode(), name)
            q.finish(webhook_id, name, ["47259"], booking_error=booking_error,
                     work_order="" if booking_error else "2086")
            if age_minutes:
                received = (datetime.now(timezone.utc)
                            - timedelta(minutes=age_minutes)).isoformat()
                q.conn.execute("UPDATE events SET received_at=? WHERE webhook_id=?",
                               (received, webhook_id))
                q.conn.commit()

    def test_a_refused_booking_is_reported_with_the_order_to_chase(self):
        self.add("poll:1", "#1012",
                 booking_error="work order not created: R#47259 is not available",
                 age_minutes=180)
        (level, name, detail), = doctor.check_order_queue(queue_db=self.db)
        self.assertEqual((level, name), (FAIL, "bookings"))
        self.assertIn("#1012", detail)
        self.assertIn("1 failed", detail)
        self.assertIn("orders retry", detail)

    def test_a_booked_order_says_nothing_alarming(self):
        self.add("poll:1", "#1011", age_minutes=180)
        (level, name, _), = doctor.check_order_queue(queue_db=self.db)
        self.assertEqual((level, name), (OK, "bookings"))

    def test_a_failure_younger_than_the_window_is_left_to_the_next_poll(self):
        """A booking that the following tick clears is a retry, not an incident."""
        self.add("poll:1", "#1012", booking_error="transient", age_minutes=1)
        (level, _, _), = doctor.check_order_queue(queue_db=self.db)
        self.assertEqual(level, OK)

    def test_a_host_that_has_never_taken_an_order_is_not_given_a_queue(self):
        """EventQueue creates its database on construction, so a read-only diagnostic that
        opened it unconditionally would leave one behind on every installation that has no
        orders at all — and then report on it forever."""
        absent = Path(self.tmp.name) / "never.sqlite3"
        self.assertEqual(doctor.check_order_queue(queue_db=absent), [])
        self.assertFalse(absent.exists())

    def test_an_unreadable_queue_is_a_warning_rather_than_a_crash(self):
        self.db.write_text("not a database", encoding="utf-8")
        (level, name, detail), = doctor.check_order_queue(queue_db=self.db)
        self.assertEqual((level, name), (WARN, "bookings"))
        self.assertIn("unreadable", detail)

    def test_the_scheduled_health_run_includes_it(self):
        """The check exists to be run without being asked for, so its absence from the
        pipeline set is the whole defect coming back."""
        self.assertIn(doctor.check_order_queue, doctor.PIPELINE)


class NothingIsTakingSoldPartsOffTheStore(unittest.TestCase):
    """The four days that produced order #1012.

    `reconcile` is the only job that archives a product whose part has left the yard. From
    2026-09-02 to 2026-09-06 a wedged delta sync held `.sync.lock` continuously and every
    reconcile tick logged "Another run holds .sync.lock; skipping this one" — so the job
    *ran* on schedule, its log kept growing, and the storefront went on selling parts that
    were no longer there. Freshness of the log or of the newest run row would both have
    reported that as healthy, which is why this asks when reconcile last **succeeded**.
    """

    def setUp(self):
        self.caps = caps_for(COREYARD_SOURCE="tabular:parts.csv",
                             SHOPIFY_STORE="x.myshopify.com",
                             SHOPIFY_ADMIN_TOKEN="shpat_x")
        self.assertTrue(self.caps.enabled("shopify"))

    def finished(self, ago):
        from datetime import datetime, timezone

        stamp = (datetime.now(timezone.utc) - ago).isoformat()
        return mock.patch.object(doctor.ops, "last_ok", return_value=stamp)

    def test_a_recent_reconcile_is_quiet(self):
        from datetime import timedelta

        with self.finished(timedelta(minutes=20)):
            (level, name, _), = doctor.check_delisting(caps=self.caps)
        self.assertEqual((level, name), (OK, "delisting"))

    def test_a_job_that_has_not_succeeded_for_days_is_a_failure(self):
        from datetime import timedelta

        with self.finished(timedelta(days=4)):
            (level, name, detail), = doctor.check_delisting(caps=self.caps)
        self.assertEqual((level, name), (FAIL, "delisting"))
        self.assertIn("sold twice", detail)
        self.assertIn(".sync.lock", detail)

    def test_a_skipped_tick_does_not_pass_for_a_successful_one(self):
        """The whole point. A lock-skip records a run row, so `last` stays fresh while
        nothing is being delisted; only `last_ok` tells the two apart."""
        from datetime import datetime, timedelta, timezone

        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        stale = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        with mock.patch.object(doctor.ops, "last", return_value={"finished": recent}), \
                mock.patch.object(doctor.ops, "last_ok", return_value=stale):
            (level, _, _), = doctor.check_delisting(caps=self.caps)
        self.assertEqual(level, FAIL)

    def test_a_store_that_has_never_reconciled_is_told_what_to_schedule(self):
        with mock.patch.object(doctor.ops, "last_ok", return_value=None):
            (level, name, detail), = doctor.check_delisting(caps=self.caps)
        self.assertEqual((level, name), (WARN, "delisting"))
        self.assertIn("reconcile --apply", detail)

    def test_an_installation_with_no_store_is_not_asked_about_one(self):
        csv_only = caps_for(COREYARD_SOURCE="tabular:parts.csv")
        self.assertEqual(doctor.check_delisting(caps=csv_only), [])

    def test_the_scheduled_health_run_includes_it(self):
        self.assertIn(doctor.check_delisting, doctor.PIPELINE)
