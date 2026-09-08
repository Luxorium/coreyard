"""Order polling: the same pipeline, reached by pulling instead of being pushed.

The two things worth pinning are that polling normalizes an order into the shape the work
renderer and the work-order writer actually parse, and that an order which arrives twice —
by two transports, or by two polls with overlapping windows — is handled once.
"""

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from coreyard.orders import poll as poll_mod
from coreyard.orders.pipeline import EventQueue, _order_key

PAID = {"id": "gid://shopify/Order/1001", "name": "#1042",
        "createdAt": "2026-08-20T14:00:00Z", "displayFinancialStatus": "PAID"}
UNPAID = {"id": "gid://shopify/Order/1002", "name": "#1043",
          "createdAt": "2026-08-20T15:00:00Z", "displayFinancialStatus": "PENDING"}


def rest_order(order_id: int = 1001, name: str = "#1042") -> dict:
    """An order in the snake_case shape Shopify POSTs to a webhook and returns over REST."""
    return {"order": {
        "id": order_id,
        "admin_graphql_api_id": f"gid://shopify/Order/{order_id}",
        "name": name, "financial_status": "paid",
        "line_items": [{"sku": "51", "quantity": 1, "price": "120.00", "name": "Alternator"}],
        "shipping_address": {"address1": "1 Main St", "city": "Testville"},
    }}


REST_PAYLOAD = rest_order()


class FakeClient:
    """Answers the two calls polling makes: a GraphQL search and a REST re-read."""

    def __init__(self, stubs, rest=None):
        self.stubs = stubs
        self.rest = rest
        self.rest_calls: list[str] = []

    def paginate(self, query, connection, variables=None, page_size=25, max_pages=None):
        yield from self.stubs

    def rest_get(self, path, tries=5):
        self.rest_calls.append(path)
        if self.rest is not None:
            return self.rest
        order_id = int(path.split("/")[-1].split(".")[0])
        return rest_order(order_id, f"#{order_id}")


class Payloads(unittest.TestCase):
    def test_the_order_is_re_read_in_the_shape_the_pipeline_parses(self):
        """GraphQL answers camelCase; the order writer reads snake_case.

        Feeding the GraphQL shape downstream is worse than an error: the writer finds no
        line items, decides the order holds nothing of ours, and reports success.
        """
        client = FakeClient([PAID])
        payload = poll_mod.fetch_payload(client, PAID["id"])
        self.assertEqual(client.rest_calls, ["orders/1001.json"])
        self.assertIn("line_items", payload)
        self.assertEqual(payload["line_items"][0]["sku"], "51")

    def test_a_payload_with_no_line_items_is_refused(self):
        client = FakeClient([PAID], rest={"order": {"id": 1001, "name": "#1042"}})
        with self.assertRaises(RuntimeError):
            poll_mod.fetch_payload(client, PAID["id"])

    def test_a_graphql_id_is_reduced_to_the_rest_id(self):
        client = FakeClient([PAID])
        poll_mod.fetch_payload(client, "gid://shopify/Order/98765")
        self.assertEqual(client.rest_calls, ["orders/98765.json"])


class Window(unittest.TestCase):
    def test_a_first_poll_looks_back_a_bounded_window(self):
        start = poll_mod.since_default(None)
        self.assertLess(datetime.now(timezone.utc) - start,
                        poll_mod.DEFAULT_LOOKBACK + timedelta(minutes=1))

    def test_the_cursor_is_backed_off_so_a_tie_is_re_examined(self):
        """An order created in the same second as the mark would fall through the gap."""
        start = poll_mod.since_default("2026-08-20T14:00:00Z")
        self.assertEqual(start, datetime(2026, 8, 20, 13, 59, 59, tzinfo=timezone.utc))

    def test_the_query_is_an_unambiguous_utc_timestamp(self):
        client = MagicMock()
        client.paginate.return_value = iter(())
        list(poll_mod.find_orders(client, datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)))
        variables = client.paginate.call_args.args[2]
        self.assertEqual(variables["q"], "created_at:>'2026-08-20T14:00:00Z'")


class Quiet(unittest.TestCase):
    """These paths report progress to a person; a test run is not that person."""

    def setUp(self):
        silence = contextlib.redirect_stdout(io.StringIO())
        silence.__enter__()
        self.addCleanup(silence.__exit__, None, None, None)


class Queueing(Quiet):
    def _queue(self) -> EventQueue:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        spool = EventQueue(Path(directory.name) / "queue.sqlite3")
        self.addCleanup(spool.close)
        return spool

    def _worker(self):
        worker = MagicMock()
        worker.handle.return_value = MagicMock(
            order_name="#1042", r_numbers=["51"],
            work_order="", booking_error="", retire_error="", error="")
        return worker

    def test_a_paid_order_is_queued_and_handled(self):
        spool, worker = self._queue(), self._worker()
        queued, seen, latest = poll_mod.poll(FakeClient([PAID]), spool, worker,
                                             datetime.now(timezone.utc))
        self.assertEqual((queued, seen), (1, 1))
        self.assertEqual(latest, PAID["createdAt"])
        self.assertEqual(worker.handle.call_count, 1)

    def test_an_unpaid_order_is_not_queued(self):
        """An unpaid order is not a sale; queueing one leaves a permanent error row."""
        spool, worker = self._queue(), self._worker()
        queued, seen, _ = poll_mod.poll(FakeClient([UNPAID]), spool, worker,
                                        datetime.now(timezone.utc))
        self.assertEqual((queued, seen), (0, 1))
        worker.handle.assert_not_called()

    def test_polling_twice_handles_an_order_once(self):
        spool, worker = self._queue(), self._worker()
        client = FakeClient([PAID])
        poll_mod.poll(client, spool, worker, datetime.now(timezone.utc))
        queued, _, _ = poll_mod.poll(client, spool, worker, datetime.now(timezone.utc))
        self.assertEqual(queued, 0)
        self.assertEqual(worker.handle.call_count, 1)

    def test_a_webhook_delivery_of_the_same_order_is_not_processed_again(self):
        """The two transports share a queue, so a migration cannot print twice."""
        spool, worker = self._queue(), self._worker()
        order = rest_order(1001, "#1042")["order"]
        self.assertTrue(spool.add("webhook-delivery-1", "orders/paid",
                                  json.dumps(order).encode(), _order_key(order)))
        queued, _, _ = poll_mod.poll(FakeClient([PAID]), spool, worker,
                                     datetime.now(timezone.utc))
        self.assertEqual(queued, 0)

    def test_a_limit_stops_after_that_many(self):
        spool, worker = self._queue(), self._worker()
        stubs = [dict(PAID, id=f"gid://shopify/Order/{1000 + i}", name=f"#{1000 + i}")
                 for i in range(5)]
        queued, _, _ = poll_mod.poll(FakeClient(stubs), spool, worker,
                                     datetime.now(timezone.utc), limit=2)
        self.assertEqual(queued, 2)

    def test_an_unreadable_order_does_not_stop_the_others(self):
        spool, worker = self._queue(), self._worker()

        class Flaky(FakeClient):
            def rest_get(self, path, tries=5):
                if path == "orders/1001.json":
                    return {"order": {"id": 1001}}      # no line items
                return rest_order(1009, "#1044")

        stubs = [PAID, dict(PAID, id="gid://shopify/Order/1009", name="#1044")]
        queued, seen, _ = poll_mod.poll(Flaky(stubs), spool, worker,
                                        datetime.now(timezone.utc))
        self.assertEqual((queued, seen), (1, 2))


class Scopes(Quiet):
    def test_check_reports_a_missing_scope_without_writing_anything(self):
        client = MagicMock()
        client.access_scopes.return_value = {"read_products"}
        self.assertEqual(poll_mod.check(client), 1)
        client.shop_name.assert_not_called()

    def test_check_passes_when_orders_can_be_read(self):
        client = MagicMock()
        client.access_scopes.return_value = {"read_orders"}
        client.shop_name.return_value = "Test Yard"
        self.assertEqual(poll_mod.check(client), 0)


if __name__ == "__main__":
    unittest.main()


class AParkedBookingIsReattemptedWithoutBeingAskedTo(Quiet):
    """The half of "automatic" that was missing.

    Booking a work order already happened on its own; recovering from a booking that failed
    did not. A yard database that was down for one tick left the sale parked in `error`
    until a person read `orders status` and typed `orders retry` — so the pipeline was
    automatic exactly until the first thing went wrong with it.
    """

    def _queue(self) -> EventQueue:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        spool = EventQueue(Path(directory.name) / "queue.sqlite3")
        self.addCleanup(spool.close)
        return spool

    def _park(self, spool, webhook_id="poll:1", name="#1012",
              error="work order not created: yard database unreachable"):
        spool.add(webhook_id, "orders/paid", json.dumps(rest_order()["order"]).encode(), name)
        spool.finish(webhook_id, name, ["51"], booking_error=error)

    def _worker(self, *, writes=True, succeeds=True):
        worker = MagicMock()
        worker.write_orders = writes
        worker.handle.return_value = MagicMock(
            order_name="#1012", r_numbers=["51"],
            work_order="2087" if succeeds else "",
            booking_error="" if succeeds else "work order not created: still down",
            retire_error="", error="" if succeeds else "still down")
        return worker

    def test_a_booking_that_failed_last_tick_is_tried_again_this_one(self):
        spool = self._queue()
        self._park(spool)
        self.assertEqual(spool.error_count(), 1)
        tried = poll_mod.retry_parked(spool, self._worker())
        self.assertEqual(tried, 1)
        self.assertEqual(spool.error_count(), 0)

    def test_it_gives_up_rather_than_retrying_a_gone_part_forever(self):
        """"R#nnnnn is not available" never clears by waiting. A job that re-attempts it
        every ten minutes for a week is how a refund nobody has issued stays invisible."""
        spool = self._queue()
        self._park(spool, error="work order not created: R#47259 is not available")
        worker = self._worker(succeeds=False)
        allowed = poll_mod.booking_attempts()
        for _ in range(10):
            poll_mod.retry_parked(spool, worker)
        # The booking at the time of sale is attempt one, so the pass adds the rest and
        # then stops — ten ticks do not buy ten attempts.
        self.assertEqual(worker.handle.call_count, allowed - 1)
        self.assertEqual([name for _, name in spool.exhausted(allowed)], ["#1012"])

    def test_it_does_not_burn_an_attempt_when_booking_is_switched_off(self):
        """Without --write-orders the retry could only re-park the order with "retry
        skipped", spending one of the few attempts a real outage will need."""
        spool = self._queue()
        self._park(spool)
        worker = self._worker(writes=False)
        self.assertEqual(poll_mod.retry_parked(spool, worker), 0)
        self.assertEqual(worker.handle.call_count, 0)
        self.assertEqual(spool.error_count(), 1)

    def test_an_order_whose_payload_has_been_purged_is_not_resurrected(self):
        """Past its retention window there is nothing left to retry with, and the row must
        not be flipped to pending where `drain` would read a null payload."""
        spool = self._queue()
        self._park(spool)
        spool.purge_error_payloads(0 if False else 1)
        spool.conn.execute("UPDATE events SET payload=NULL")
        spool.conn.commit()
        self.assertEqual(poll_mod.retry_parked(spool, self._worker()), 0)

    def test_a_clean_queue_costs_nothing(self):
        spool = self._queue()
        worker = self._worker()
        self.assertEqual(poll_mod.retry_parked(spool, worker), 0)
        self.assertEqual(worker.handle.call_count, 0)
