"""Webhook signature checking and pull-ticket rendering, offline."""

import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import stat
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.state import SyncState
from coreyard.transform import pull_ticket
from coreyard.webhook import (
    DEFAULT_TOPICS,
    EventQueue,
    Handled,
    OrderWorker,
    drain,
    order_is_paid,
    purge_tickets,
    register,
    verify,
)

SECRET = "shpss_testsecret"
STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="90-day warranty")


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()


ORDER = {
    "name": "#1042",
    "order_number": 1042,
    "created_at": "2026-08-15T14:03:22-05:00",
    "email": "buyer@example.com",
    "financial_status": "paid",
    "note": "Leave at side door",
    "customer": {"first_name": "Dana", "last_name": "Ruiz"},
    "shipping_address": {
        "first_name": "Dana", "last_name": "Ruiz", "address1": "88 Elm St",
        "city": "Austin", "province_code": "TX", "zip": "78701",
        "country_code": "US", "phone": "512-555-0134",
    },
    "shipping_lines": [{"title": "Standard Freight"}],
    "line_items": [
        {"sku": "51", "title": "2008 Ford F150 Driver Side Left Mirror",
         "quantity": 1, "price": "89.00"},
        {"sku": "99999", "title": "Unknown Widget", "quantity": 2, "price": "10.00"},
    ],
}

PART = Part(r_number="51", part_type="Mirror", stock_number="251026", side="Left",
            interchange_number="545-01883", price=Decimal("89.00"), year=2008,
            make="Ford", model="F150", location="A-12-3", grade="B")


class Signature(unittest.TestCase):
    def test_accepts_a_genuine_signature(self):
        body = json.dumps(ORDER).encode()
        self.assertTrue(verify(body, sign(body), SECRET))

    def test_rejects_a_tampered_body(self):
        body = json.dumps(ORDER).encode()
        header = sign(body)
        self.assertFalse(verify(body + b" ", header, SECRET))

    def test_rejects_the_wrong_secret(self):
        body = b'{"a":1}'
        self.assertFalse(verify(body, sign(body, "other-secret"), SECRET))

    def test_rejects_a_missing_header(self):
        """An unsigned POST must never be treated as an order."""
        self.assertFalse(verify(b'{"a":1}', "", SECRET))

    def test_rejects_garbage_header(self):
        self.assertFalse(verify(b'{"a":1}', "not-base64!!", SECRET))


class PaymentGate(unittest.TestCase):
    def test_registration_uses_the_paid_topic(self):
        self.assertEqual(DEFAULT_TOPICS, ["ORDERS_PAID"])

    def test_only_fully_paid_orders_pass(self):
        self.assertTrue(order_is_paid(ORDER))
        for status in ("pending", "authorized", "partially_paid", "refunded", ""):
            with self.subTest(status=status):
                self.assertFalse(order_is_paid({"financial_status": status}))

    def test_non_object_json_does_not_pass(self):
        self.assertFalse(order_is_paid([{"financial_status": "paid"}]))

    def test_registration_uses_the_current_uri_input(self):
        client = Mock()
        client.graphql.side_effect = [
            {"webhookSubscriptions": {"nodes": []}},
            {"webhookSubscriptionCreate": {"userErrors": []}},
        ]
        args = SimpleNamespace(
            url="https://yard.example.com/webhook", topics=["ORDERS_PAID"]
        )
        with patch("coreyard.webhook._client", return_value=client), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(register(args), 0)
        variables = client.graphql.call_args_list[1].args[1]
        self.assertEqual(
            variables,
            {"topic": "ORDERS_PAID", "sub": {"uri": args.url, "format": "JSON"}},
        )


class Queue(unittest.TestCase):
    def _queue(self, path: Path) -> EventQueue:
        event_queue = EventQueue(path)
        self.addCleanup(event_queue.close)
        return event_queue

    def test_duplicate_delivery_is_dropped(self):
        """Shopify delivers at least once; a resend must not reprint the order."""
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            self.assertTrue(q.add("wh-1", "orders/paid", b'{"name":"#1"}'))
            self.assertFalse(q.add("wh-1", "orders/paid", b'{"name":"#1"}'))
            self.assertEqual(len(q.pending()), 1)

    def test_same_order_under_a_new_topic_and_delivery_id_is_dropped(self):
        """Migration can briefly leave both create and paid subscriptions active."""
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            body = json.dumps(ORDER).encode()
            self.assertTrue(q.add("wh-create", "orders/create", body, "order-1042"))
            self.assertFalse(q.add("wh-paid", "orders/paid", body, "order-1042"))
            self.assertEqual(len(q.pending()), 1)

    def test_queue_database_is_owner_only(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "q.sqlite3"
            self._queue(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_payload_is_erased_once_handled(self):
        """An order body is a customer's name, phone and street address."""
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], "/tmp/t.html")
            self.assertEqual(q.pending(), [])
            stored = q.conn.execute("SELECT payload FROM events").fetchone()[0]
            self.assertIsNone(stored)

    def test_the_work_order_number_is_kept_for_reconciliation(self):
        """Matching sales against the yard system is the whole point of booking them."""
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], "/tmp/t.html", work_order="1790")
            self.assertEqual(q.recent()[0][-1], "1790")

    def test_a_failed_booking_is_flagged_without_losing_the_ticket(self):
        """The ticket is what somebody works from when the database write did not land."""
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], "/tmp/t.html",
                     error="work order not created: R#51 is not available")
            received, topic, state, order, r_numbers, ticket, error, wo = q.recent()[0]
            self.assertEqual(state, "error")
            self.assertEqual(ticket, "/tmp/t.html")     # not thrown away
            self.assertEqual(r_numbers, "51")
            self.assertIn("not available", error)
            payload = q.conn.execute("SELECT payload FROM events").fetchone()[0]
            self.assertIsNotNone(payload)  # incomplete work remains retryable

    def test_retry_only_selects_the_failed_stage(self):
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], "/tmp/t.html",
                     booking_error="database unavailable")
            self.assertEqual(q.requeue("wh-1"), 1)
            event = q.pending()[0]
            self.assertEqual(event.retry_stages(), {"book"})

    def test_drain_retries_only_the_failed_stage_then_erases_payload(self):
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], "/tmp/t.html",
                     booking_error="database unavailable")
            q.requeue("wh-1")
            worker = Mock()
            worker.handle.return_value = Handled(
                order_name="#1042", r_numbers=["51"], ticket="/tmp/t.html",
                work_order="1790",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(drain(q, worker), 1)
            worker.handle.assert_called_once_with(ORDER, {"book"})
            row = q.conn.execute("SELECT state, payload FROM events").fetchone()
            self.assertEqual(row, ("done", None))

    def test_retry_preserves_a_work_order_from_an_already_successful_stage(self):
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], "/tmp/t.html", work_order="1790",
                     retire_error="API unavailable")
            q.requeue("wh-1")
            worker = Mock()
            worker.handle.return_value = Handled(
                order_name="#1042", r_numbers=["51"], ticket="/tmp/t.html",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(drain(q, worker), 1)
            self.assertEqual(q.recent()[0][-1], "1790")

    def test_expired_error_payload_is_securely_purged(self):
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], "/tmp/t.html",
                     retire_error="API unavailable")
            q.conn.execute(
                "UPDATE events SET received_at='2000-01-01T00:00:00+00:00'"
            )
            q.conn.commit()
            self.assertEqual(q.purge_error_payloads(7), 1)
            self.assertIsNone(q.conn.execute("SELECT payload FROM events").fetchone()[0])

    def test_an_existing_spool_gains_the_column_without_being_recreated(self):
        """A queue mid-flight when the service restarts must survive the upgrade."""
        with TemporaryDirectory() as d:
            path = Path(d) / "q.sqlite3"
            old = EventQueue(path)
            old.conn.execute("ALTER TABLE events DROP COLUMN work_order")
            old.conn.commit()
            old.add("wh-old", "orders/paid", b'{"name":"#1"}')
            old.conn.close()

            upgraded = self._queue(path)
            self.assertEqual(len(upgraded.pending()), 1)     # the queued order is still there
            upgraded.finish("wh-old", "#1", ["51"], "/tmp/t.html", work_order="1791")
            self.assertEqual(upgraded.recent()[0][-1], "1791")


class Worker(unittest.TestCase):
    def _worker(self, directory: Path, **kwargs) -> OrderWorker:
        # Always a throwaway state file: the retirement memory lives in the sync's
        # database, and a test must never open the installation's real one.
        kwargs.setdefault("state_db", directory / "sync-state.sqlite3")
        with patch("coreyard.webhook.TICKET_DIR", directory), \
                patch("coreyard.webhook.load_store", return_value=STORE):
            return OrderWorker(**kwargs)

    def test_unpaid_order_is_refused_before_lookup(self):
        with TemporaryDirectory() as d:
            worker = self._worker(Path(d), retire=False, write_orders=False)
            unpaid = dict(ORDER, financial_status="pending")
            with patch("coreyard.yms.inventory.fetch_parts_by_r_number") as fetch:
                with self.assertRaisesRegex(RuntimeError, "financial status"):
                    worker.handle(unpaid)
            fetch.assert_not_called()

    def test_ticket_and_directory_are_owner_only(self):
        with TemporaryDirectory() as d:
            directory = Path(d) / "tickets"
            worker = self._worker(directory, retire=False, write_orders=False)
            with patch("coreyard.yms.inventory.fetch_parts_by_r_number",
                       return_value={"51": PART}):
                done = worker.handle(ORDER)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(Path(done.ticket).stat().st_mode), 0o600)

    def test_retirement_failure_is_recorded_for_retry(self):
        with TemporaryDirectory() as d:
            worker = self._worker(Path(d), retire=True, write_orders=False)
            publisher = Mock()
            publisher.retire.side_effect = RuntimeError("API unavailable")
            worker._publisher = publisher
            with patch("coreyard.yms.inventory.fetch_parts_by_r_number",
                       return_value={"51": PART}), \
                    contextlib.redirect_stderr(io.StringIO()):
                done = worker.handle(ORDER)
            self.assertIn("retirement failed", done.retire_error)
            self.assertIn("R#51", done.retire_error)

    def test_retirement_remembers_the_status_it_replaced(self):
        """So a refunded or voided sale can put the part back as what it was."""
        with TemporaryDirectory() as d:
            worker = self._worker(Path(d), retire=True, write_orders=False)

            def retire(r_number, record_prior=None):
                if record_prior:
                    record_prior(r_number, "ACTIVE")
                return "retired"

            worker._publisher = Mock()
            worker._publisher.retire.side_effect = retire
            with patch("coreyard.yms.inventory.fetch_parts_by_r_number",
                       return_value={"51": PART}):
                done = worker.handle(ORDER)

            self.assertEqual(done.retire_error, "")
            with SyncState(Path(d) / "sync-state.sqlite3") as state:
                self.assertEqual(state.retired_statuses().get("51"), "ACTIVE")

    def test_an_unusable_state_file_still_delists_the_part(self):
        """A sold part coming off sale outranks remembering how to put it back."""
        with TemporaryDirectory() as d:
            blocker = Path(d) / "blocker"
            blocker.write_text("not a directory")
            worker = self._worker(Path(d), retire=True, write_orders=False,
                                  state_db=blocker / "state.sqlite3")
            worker._publisher = Mock()
            with patch("coreyard.yms.inventory.fetch_parts_by_r_number",
                       return_value={"51": PART}), \
                    contextlib.redirect_stderr(io.StringIO()):
                done = worker.handle(ORDER)

            self.assertEqual(done.retire_error, "")
            self.assertEqual(
                [c.kwargs["record_prior"] for c in worker._publisher.retire.call_args_list],
                [None, None],
            )

    def test_booking_failure_is_independent_of_retirement(self):
        with TemporaryDirectory() as d:
            worker = self._worker(Path(d), retire=True, write_orders=True)
            worker._publisher = Mock()
            with patch("coreyard.yms.inventory.fetch_parts_by_r_number",
                       return_value={"51": PART}), \
                    patch.object(worker, "_book", return_value=("", "database unavailable")):
                done = worker.handle(ORDER)
            self.assertEqual(done.booking_error, "database unavailable")
            self.assertEqual(done.retire_error, "")
            self.assertEqual(worker._publisher.retire.call_count, 2)

    def test_lookup_failure_keeps_booking_retryable(self):
        with TemporaryDirectory() as d:
            worker = self._worker(Path(d), retire=False, write_orders=True)
            with patch("coreyard.yms.inventory.fetch_parts_by_r_number",
                       side_effect=RuntimeError("database offline")), \
                    contextlib.redirect_stderr(io.StringIO()):
                done = worker.handle(ORDER)
            self.assertIn("part lookup failed", done.booking_error)
            self.assertIn("database offline", done.booking_error)

    def test_print_retry_requires_an_explicit_reprint(self):
        with TemporaryDirectory() as d:
            worker = self._worker(Path(d), retire=False, write_orders=False)
            with patch("coreyard.yms.inventory.fetch_parts_by_r_number",
                       return_value={"51": PART}):
                done = worker.handle(ORDER, {"print"})
            self.assertIn("--reprint", done.print_error)


class Retention(unittest.TestCase):
    def test_old_tickets_are_removed_but_recent_ones_stay(self):
        with TemporaryDirectory() as d:
            directory = Path(d)
            old = directory / "old.html"
            new = directory / "new.html"
            old.write_text("old", encoding="utf-8")
            new.write_text("new", encoding="utf-8")
            os.utime(old, (0, 0))
            self.assertEqual(purge_tickets(directory, 30), 1)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())


class Ticket(unittest.TestCase):
    def setUp(self):
        self.ticket = pull_ticket.from_order(ORDER, {"51": PART}, STORE)
        self.html = pull_ticket.render(self.ticket, STORE)

    def test_carries_both_halves_of_the_job(self):
        """Bin location comes from the yard, the address from Shopify; both must appear."""
        self.assertIn("A-12-3", self.html)      # where to find it
        self.assertIn("88 Elm St", self.html)   # where it goes
        self.assertIn("#1042", self.html)

    def test_unmatched_sku_is_flagged_not_blank(self):
        """A silent empty bin column reads as 'no location', which is a different fact."""
        self.assertIn("NOT FOUND", self.html)
        self.assertIn("had no matching part", self.html)

    def test_shows_the_identifiers_a_puller_needs(self):
        self.assertIn("Stock #251026", self.html)
        self.assertIn("545-01883", self.html)

    def test_escapes_order_content(self):
        """Order fields are attacker-supplied text going into HTML."""
        hostile = dict(ORDER, note="<script>alert(1)</script>")
        html = pull_ticket.render(pull_ticket.from_order(hostile, {}, STORE), STORE)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_renders_without_a_store_profile(self):
        self.assertIn("#1042", pull_ticket.render(self.ticket))

    def test_line_quantities_and_prices_survive(self):
        self.assertEqual([l.quantity for l in self.ticket.lines], [1, 2])
        self.assertEqual(self.ticket.lines[0].sku, "51")


if __name__ == "__main__":
    unittest.main()
