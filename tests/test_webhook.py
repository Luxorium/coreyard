"""Webhook signature checking and the order pipeline, offline."""

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
from coreyard.webhook import (
    DEFAULT_TOPICS,
    EventQueue,
    Handled,
    OrderWorker,
    drain,
    order_is_paid,
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


class Delivery(unittest.TestCase):
    """ORD-01 at the door: what a request has to be before anything is persisted.

    Each guard is one refusal, and the property they share is that nothing reaches the queue
    — an order that is not queued is never printed, never booked, and never takes a part off
    the shelf.
    """

    def setUp(self):
        import queue as queue_mod

        from coreyard.webhook import make_handler

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spool = EventQueue(Path(self.tmp.name) / "queue.sqlite3")
        self.addCleanup(self.spool.close)
        self.wake = queue_mod.Queue()
        self.handler_cls = make_handler(self.spool, SECRET, "/hook", self.wake,
                                        store="mine.myshopify.com")

    def post(self, payload=None, *, body=None, secret=SECRET, path="/hook",
             topic="orders/paid", delivery="d-1", shop="mine.myshopify.com",
             length=None, sign_body=True):
        if body is None:
            body = json.dumps(payload or self.order()).encode()
        headers = {"Content-Length": str(len(body) if length is None else length),
                   "X-Shopify-Topic": topic,
                   "X-Shopify-Webhook-Id": delivery}
        if shop is not None:
            headers["X-Shopify-Shop-Domain"] = shop
        if sign_body:
            headers["X-Shopify-Hmac-Sha256"] = sign(body, secret)
        replies = []
        handler = self.handler_cls.__new__(self.handler_cls)
        handler.path = path
        handler.headers = headers
        handler.rfile = io.BytesIO(body)
        handler._reply = lambda code, message="": replies.append((code, message))
        handler.address_string = lambda: "198.51.100.7"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            handler.do_POST()
        return replies[0], err.getvalue()

    def order(self, **kw):
        payload = {"id": 9001, "name": "#1042", "financial_status": "paid",
                   "line_items": [{"sku": "51", "quantity": 1}]}
        payload.update(kw)
        return payload

    def queued(self) -> int:
        return len(self.spool.pending())

    def test_a_signed_paid_order_from_this_store_is_queued(self):
        (code, message), _ = self.post()
        self.assertEqual((code, message), (200, "queued"))
        self.assertEqual(self.queued(), 1)

    def test_a_wrong_signature_is_refused_and_nothing_is_kept(self):
        (code, _), _ = self.post(secret="shpss_someoneelse")
        self.assertEqual(code, 401)
        self.assertEqual(self.queued(), 0)

    def test_a_missing_signature_is_refused(self):
        (code, _), _ = self.post(sign_body=False)
        self.assertEqual(code, 401)
        self.assertEqual(self.queued(), 0)

    def test_an_oversized_body_is_refused_before_it_is_read(self):
        from coreyard.webhook import MAX_BODY

        (code, _), _ = self.post(length=MAX_BODY + 1)
        self.assertEqual(code, 413)
        self.assertEqual(self.queued(), 0)

    def test_an_empty_or_unparsable_length_is_refused(self):
        self.assertEqual(self.post(length=0)[0][0], 413)
        self.assertEqual(self.post(length="banana")[0][0], 400)
        self.assertEqual(self.queued(), 0)

    def test_malformed_json_is_refused(self):
        (code, _), _ = self.post(body=b"{not json")
        self.assertEqual(code, 400)
        self.assertEqual(self.queued(), 0)

    def test_json_that_is_not_an_order_is_refused(self):
        (code, _), _ = self.post(body=json.dumps([1, 2, 3]).encode())
        self.assertEqual(code, 400)
        self.assertEqual(self.queued(), 0)

    def test_an_unpaid_order_is_acknowledged_and_dropped(self):
        """Acknowledged so Shopify stops retrying, dropped so no PII is persisted."""
        (code, message), _ = self.post(self.order(financial_status="pending"))
        self.assertEqual((code, message), (200, "ignored unpaid"))
        self.assertEqual(self.queued(), 0)

    def test_a_delivery_from_another_store_is_refused(self):
        """The signature proves the sender holds this secret. It says nothing about whose
        sale this is — and booking somebody else's order takes a part off these shelves."""
        (code, message), log = self.post(shop="someone-else.myshopify.com")
        self.assertEqual((code, message), (200, "ignored other store"))
        self.assertEqual(self.queued(), 0)
        self.assertIn("someone-else.myshopify.com", log)

    def test_a_delivery_with_no_shop_header_is_still_accepted(self):
        """Shopify always sends one; a proxy that strips it must not stop the pipeline."""
        (code, message), _ = self.post(shop=None)
        self.assertEqual(message, "queued")

    def test_an_installation_with_no_store_configured_accepts_any(self):
        import queue as queue_mod

        from coreyard.webhook import make_handler

        self.handler_cls = make_handler(self.spool, SECRET, "/hook", queue_mod.Queue())
        self.assertEqual(self.post(shop="anywhere.myshopify.com")[0][1], "queued")

    def test_an_unsupported_topic_is_acknowledged_and_dropped(self):
        (code, message), _ = self.post(topic="orders/cancelled")
        self.assertEqual((code, message), (200, "ignored topic"))
        self.assertEqual(self.queued(), 0)

    def test_another_path_is_not_the_webhook(self):
        (code, _), _ = self.post(path="/")
        self.assertEqual(code, 404)
        self.assertEqual(self.queued(), 0)

    def test_the_same_delivery_twice_is_queued_once(self):
        self.post()
        (code, message), _ = self.post()
        self.assertEqual((code, message), (200, "duplicate"))
        self.assertEqual(self.queued(), 1)

    def test_the_same_order_under_a_new_delivery_id_is_queued_once(self):
        """Shopify re-delivers under a fresh id, and the poller finds the same order
        again. One booking either way."""
        self.post(delivery="d-1")
        (code, message), _ = self.post(delivery="d-2")
        self.assertEqual(message, "duplicate")
        self.assertEqual(self.queued(), 1)

    def test_a_delivery_with_no_id_still_deduplicates(self):
        body = json.dumps(self.order()).encode()
        self.post(body=body, delivery=None)
        self.assertEqual(self.post(body=body, delivery=None)[0][1], "duplicate")
        self.assertEqual(self.queued(), 1)


class Queue(unittest.TestCase):
    def _queue(self, path: Path) -> EventQueue:
        event_queue = EventQueue(path)
        self.addCleanup(event_queue.close)
        return event_queue

    def test_duplicate_delivery_is_dropped(self):
        """Shopify delivers at least once; a resend must not rebook the order."""
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
            q.finish("wh-1", "#1042", ["51"])
            self.assertEqual(q.pending(), [])
            stored = q.conn.execute("SELECT payload FROM events").fetchone()[0]
            self.assertIsNone(stored)

    def test_the_work_order_number_is_kept_for_reconciliation(self):
        """Matching sales against the yard system is the whole point of booking them."""
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], work_order="1790")
            self.assertEqual(q.recent()[0][-1], "1790")

    def test_a_failed_booking_is_flagged_and_stays_retryable(self):
        """A booking that did not land must be visible as work still owed to the counter."""
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"],
                     error="work order not created: R#51 is not available")
            received, topic, state, order, r_numbers, error, wo = q.recent()[0]
            self.assertEqual(state, "error")
            self.assertEqual(order, "#1042")            # not thrown away
            self.assertEqual(r_numbers, "51")
            self.assertIn("not available", error)
            payload = q.conn.execute("SELECT payload FROM events").fetchone()[0]
            self.assertIsNotNone(payload)  # incomplete work remains retryable

    def test_retry_only_selects_the_failed_stage(self):
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], booking_error="database unavailable")
            self.assertEqual(q.requeue("wh-1"), 1)
            event = q.pending()[0]
            self.assertEqual(event.retry_stages(), {"book"})

    def test_drain_retries_only_the_failed_stage_then_erases_payload(self):
        with TemporaryDirectory() as d:
            q = self._queue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/paid", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], booking_error="database unavailable")
            q.requeue("wh-1")
            worker = Mock()
            worker.handle.return_value = Handled(
                order_name="#1042", r_numbers=["51"], work_order="1790",
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
            q.finish("wh-1", "#1042", ["51"], work_order="1790",
                     retire_error="API unavailable")
            q.requeue("wh-1")
            worker = Mock()
            worker.handle.return_value = Handled(order_name="#1042", r_numbers=["51"])
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
        # The worker lives in the order pipeline; the webhook module is only one of the
        # transports that feed it.
        with patch("coreyard.orders.pipeline.load_store", return_value=STORE):
            return OrderWorker(**kwargs)

    def test_unpaid_order_is_refused_before_lookup(self):
        with TemporaryDirectory() as d:
            worker = self._worker(Path(d), retire=False, write_orders=False)
            unpaid = dict(ORDER, financial_status="pending")
            with patch("coreyard.yms.inventory.fetch_parts_by_r_number") as fetch:
                with self.assertRaisesRegex(RuntimeError, "financial status"):
                    worker.handle(unpaid)
            fetch.assert_not_called()

    def test_handling_an_order_writes_no_document_to_disk(self):
        """The work order is the artefact; a second copy of the customer's details is not.

        Guards the reason the ticket renderer was removed: an order payload carries a name,
        phone and street address, and anything this writes outside the owner-only queue is
        PII with its own retention problem.
        """
        with TemporaryDirectory() as d:
            directory = Path(d)
            worker = self._worker(directory, retire=False, write_orders=False)
            with patch("coreyard.yms.inventory.fetch_parts_by_r_number",
                       return_value={"51": PART}):
                done = worker.handle(ORDER)
            self.assertEqual(done.order_name, "#1042")
            self.assertEqual(list(directory.iterdir()), [])

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


if __name__ == "__main__":
    unittest.main()
