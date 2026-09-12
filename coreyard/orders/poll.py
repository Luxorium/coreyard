"""Pull transport: ask Shopify what is new instead of waiting to be told.

The webhook receiver needs a public HTTPS URL. Plenty of yard machines sit behind NAT with
no inbound port and no appetite for a tunnel, and for a counter that opens at eight in the
morning, learning about an order a few minutes late costs nothing.

So this asks the same question from the other direction. It is a *transport*, not a second
pipeline: every order it finds is written to the same durable queue and handled by the same
worker as a webhook delivery, which means the same optional work-order
booking, the same delisting, and — importantly — the same de-duplication. A site can run
both transports during a migration without booking anything twice, because the queue keys
on the order's identity as well as on the delivery id.

    bin/coreyard orders poll --check          # scopes and connectivity only
    bin/coreyard orders poll                  # fetch new orders
    bin/coreyard orders poll --since 2026-08-01
    bin/coreyard orders poll --write-orders   # also book each sale in the source system

Needs ``read_orders`` on the app. Shopify additionally gates order payloads behind protected
customer data access, which for a custom app is a toggle on the app itself.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from coreyard.config import cli_name
from coreyard.orders.pipeline import (EventQueue, OrderWorker, _order_key,
                                      booking_attempts, drain)

# The window a first poll looks back over when it has no cursor of its own.
DEFAULT_LOOKBACK = timedelta(days=2)
# Re-examine a second either side of the cursor. Orders created in the same second as the
# high-water mark would otherwise fall in the gap between "not greater than" and "already
# seen"; re-examining costs nothing because the queue de-duplicates.
OVERLAP = timedelta(seconds=1)

CURSOR_NAME = "orders_poll"

_ORDERS = """query($q:String!,$cursor:String,$first:Int!){
  orders(first:$first, after:$cursor, query:$q, sortKey:CREATED_AT, reverse:false){
    pageInfo{ hasNextPage endCursor }
    nodes{ id name createdAt displayFinancialStatus }
  }
}"""


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def since_default(cursor: Optional[str]) -> datetime:
    if cursor:
        return _parse(cursor) - OVERLAP
    return datetime.now(timezone.utc) - DEFAULT_LOOKBACK


def find_orders(client, since: datetime, page_size: int = 25) -> Iterator[dict]:
    """Order stubs created after ``since``, oldest first."""
    query = f"created_at:>'{_stamp(since)}'"
    yield from client.paginate(_ORDERS, "orders", {"q": query}, page_size=page_size)


def fetch_payload(client, order_id: str) -> dict:
    """The order in the shape the pipeline parses.

    The GraphQL search above is how new orders are discovered, but it answers in camelCase
    (``lineItems``, ``shippingAddress``) while the work-order writer parses the snake_case
    payload Shopify POSTs to a webhook. Feeding the GraphQL shape to it is not an error, it
    is worse: the writer finds no line items, concludes the order
    contains nothing of ours, and reports success having booked nothing. So each order is
    re-read over REST, which returns exactly the webhook shape.
    """
    numeric = str(order_id).rsplit("/", 1)[-1]
    payload = client.rest_get(f"orders/{numeric}.json").get("order") or {}
    if not payload.get("line_items"):
        raise RuntimeError(f"order {numeric}: the REST payload carried no line items")
    return payload


def poll(client, spool: EventQueue, worker: OrderWorker, since: datetime,
         limit: int = 0, paid_only: bool = True) -> tuple[int, int, Optional[str]]:
    """Queue every new order and drain the queue.

    Returns (newly queued, orders seen, newest createdAt seen).

    Unpaid orders are skipped here as well as in the worker: an unpaid order is not a sale,
    and queueing one would leave a permanent error row for something that was never meant to
    be processed.
    """
    queued = 0
    seen = 0
    latest: Optional[str] = None
    # Once an order in this window could not be taken in, the cursor stops short of it.
    # Orders arrive oldest first, so everything after an unfetchable one is newer, and
    # moving the cursor past any of them would be a promise that the gap was handled.
    held = False
    for stub in find_orders(client, since):
        seen += 1
        created = stub.get("createdAt")
        ingested = True
        if paid_only and str(stub.get("displayFinancialStatus") or "").upper() != "PAID":
            # Accounted for rather than skipped: an unpaid order is not a sale, and the
            # policy that says so is this run's, not a failure to read it.
            pass
        else:
            try:
                payload = fetch_payload(client, stub["id"])
            except RuntimeError as exc:
                # It never reached the durable queue, so `orders retry` does not own it and
                # nothing else will come back for it. The only thing that can is the next
                # poll, and only if the cursor stays behind it.
                print(f"  ! {stub.get('name') or stub['id']}: {exc}")
                ingested = False
            else:
                # A poll and a webhook can both see the same order; the order key is what
                # stops the second one printing it again.
                webhook_id = f"poll:{str(stub['id']).rsplit('/', 1)[-1]}"
                if spool.add(webhook_id, "orders/paid",
                             json.dumps(payload).encode("utf-8"), _order_key(payload)):
                    queued += 1
                    print(f"  queued {payload.get('name') or webhook_id}")
        if not ingested:
            held = True
        elif not held and created and (latest is None or created > latest):
            latest = created
        if limit and queued >= limit:
            break
    if queued:
        drain(spool, worker)
    return queued, seen, latest


def retry_parked(spool: EventQueue, worker: OrderWorker) -> int:
    """Re-attempt bookings that failed on an earlier tick. Returns how many were tried.

    A booking is refused for reasons that live outside CoreYard, and most of them are gone
    by the next tick: the yard database was down, a lock was held too long, the batch timed
    out. Until now every one of those waited for a person to notice `orders status` and run
    `orders retry` by hand, which is the same as not being automatic at all — the sale sat
    unbooked for as long as it took somebody to look.

    Re-attempting is safe to do unasked because the write is idempotent at the source: the
    booking transaction takes ``CustomerPO`` under ``UPDLOCK, HOLDLOCK`` and returns the
    existing work order rather than raising a second one, so a retry of an order that in
    fact succeeded is a no-op that reports the number it already had.

    Bounded by :func:`booking_attempts`, because the other kind of cause — the part is no
    longer on the shelf — never clears by waiting, and a job that retries it every ten
    minutes forever is how a decision somebody has to make stays invisible. Orders past the
    bound are left parked and reported, and `coreyard doctor` fails on them.
    """
    # Without write_orders a "retry" only re-parks the order with "retry skipped", burning
    # an attempt on a stage that was never going to run.
    if not worker.write_orders:
        return 0
    limit = booking_attempts()
    count = spool.requeue_retryable(limit)
    if count:
        print(f"\n  re-attempting {count} order(s) parked by an earlier tick ...")
        drain(spool, worker)
    for webhook_id, name in spool.exhausted(limit):
        print(f"  ! {name or webhook_id}: still unbooked after {limit} attempts, "
              f"left for a person — `coreyard orders retry --id {webhook_id}`",
              file=sys.stderr)
    return count


def check(client) -> int:
    """Report whether this installation can poll at all, and write nothing."""
    scopes = client.access_scopes()
    granted = "read_orders" in scopes
    print(f"read_orders granted: {granted}")
    if not granted:
        print("\n  Add read_orders (and write_orders for fulfillment sync) to the app's\n"
              "  scopes, release a new version, and re-authorize — Shopify grants scopes at\n"
              "  install time, so adding one does not upgrade an existing token. Order\n"
              "  payloads also require protected customer data access, which is a separate\n"
              "  approval on the app itself.")
        return 1
    print(f"connected to {client.shop_name()}; ready to poll")
    return 0


def run(args) -> int:
    from coreyard.sink.shopify_api import ShopifyClient
    from coreyard.state import DEFAULT_STATE_DB, SyncState

    client = ShopifyClient()
    if args.check:
        return check(client)

    with SyncState(DEFAULT_STATE_DB) as state:
        cursor = state.get_cursor(CURSOR_NAME)
        start = _parse(args.since) if args.since else since_default(cursor)
        print(f"Looking for orders created after {_stamp(start)} ...")

        worker = OrderWorker(retire=not args.no_retire, write_orders=args.write_orders)
        with EventQueue() as spool:
            queued, seen, latest = poll(client, spool, worker, start, limit=args.limit,
                                        paid_only=not args.include_unpaid)
            # After the new arrivals, not before: a fresh order is the one with a customer
            # waiting, and it should not queue behind a retry of something already late.
            retry_parked(spool, worker)
        print(f"\n{seen} order(s) in the window, {queued} newly queued and handled.")
        # Safe to advance even if a *stage* failed: that order is already in the durable
        # queue, so `orders retry` still owns it. The cursor only decides what the next poll
        # asks Shopify for, and re-asking is what the overlap window is for. An order that
        # never reached the queue is the other case, and `poll` holds the cursor behind it.
        if latest and not args.since:
            state.set_cursor(CURSOR_NAME, latest)
            print(f"Cursor advanced to {latest}.")
    print(f"Check `{cli_name()} orders status` for any stage that failed.")
    return 0
