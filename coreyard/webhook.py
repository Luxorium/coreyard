"""Receive Shopify order webhooks: print a pull ticket, take the part off sale.

    python -m coreyard.webhook register --url https://yard.example.com/webhook
    python -m coreyard.webhook serve
    python -m coreyard.webhook poll                         # no inbound port needed
    python -m coreyard.webhook replay <order-file.json>     # re-print without a live order
    python -m coreyard.webhook list | unregister

This module is the *webhook transport*: signature verification, the HTTP receiver, and
subscription management. What actually happens to an order — the ticket, the optional work
order, the delisting, the durable de-duplicating queue — lives in
:mod:`coreyard.orders.pipeline`, which polling and replay drive too, so an order is handled
identically however it arrived.

The paperwork a worker needs is produced as a printable ticket — see
``transform/pull_ticket.py`` for why that join can only happen here.

With ``--write-orders`` (or ``YMS_WRITE_ORDERS=1``) the sale is also booked into the yard
system as a work order, via ``yms/orders.py``. That is the only write CoreYard makes to the
source database and it is off unless an installation asks for it; everything else here reads
Shopify, reads the yard database, and writes back only to Shopify. The ticket prints whether
or not the booking succeeds, because it is the artefact a person can act on when a system is
down.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from coreyard.config import _get, load_env
# Re-exported rather than re-implemented: the pipeline is shared with the polling and
# replay transports, and callers (including tests) have always reached these through here.
from coreyard.orders.pipeline import (
    DEFAULT_ERROR_RETENTION_DAYS,
    DEFAULT_TICKET_RETENTION_DAYS,
    TICKET_DIR,
    EventQueue,
    Handled,
    OrderWorker,
    QueuedEvent,
    _order_key,
    _retention_days,
    drain,
    order_is_paid,
    purge_tickets,
)

MAX_BODY = 2 * 1024 * 1024      # a fat order is ~100 KB; past 2 MB something is wrong
DEFAULT_TOPICS = ["ORDERS_PAID"]
SUPPORTED_TOPICS = {"orders/paid", "orders/create"}  # create retained for migration only

__all__ = [
    "DEFAULT_TOPICS",
    "EventQueue",
    "Handled",
    "OrderWorker",
    "QueuedEvent",
    "drain",
    "main",
    "order_is_paid",
    "purge_tickets",
    "verify",
    "webhook_secret",
]


# --------------------------------------------------------------------- security ---
def webhook_secret() -> str:
    """The key Shopify signs with: the app's client secret, unless overridden.

    Webhooks created through the Admin API are signed with the client secret of the app that
    created them. A separate ``SHOPIFY_WEBHOOK_SECRET`` is honoured for webhooks created in
    the Admin UI, which get their own key.
    """
    load_env()
    secret = (_get("SHOPIFY_WEBHOOK_SECRET", "") or _get("SHOPIFY_CLIENT_SECRET", "") or "").strip()
    if not secret:
        raise RuntimeError(
            "No webhook signing secret. Set SHOPIFY_CLIENT_SECRET (or SHOPIFY_WEBHOOK_SECRET) "
            "in .env — without it any host on the network could post fake orders."
        )
    return secret


def verify(body: bytes, header: str, secret: str) -> bool:
    """Constant-time check of Shopify's base64 HMAC-SHA256 over the raw request body.

    Must be given the bytes exactly as received: re-serialising the JSON first changes the
    whitespace and the signature stops matching.
    """
    if not header:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest), header.strip().encode("utf-8"))


# ------------------------------------------------------------------------ server ---
def make_handler(spool: EventQueue, secret: str, path: str, wake: queue.Queue):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CoreYard"
        sys_version = ""

        def _reply(self, code: int, message: str = "") -> None:
            body = message.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # A liveness probe for whatever proxy or tunnel is in front. Deliberately says
            # nothing about the store, the queue, or the configuration.
            self._reply(200, "ok") if self.path == "/healthz" else self._reply(404, "not found")

        def do_POST(self):
            if self.path.split("?")[0] != path:
                return self._reply(404, "not found")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._reply(400, "bad length")
            if length <= 0 or length > MAX_BODY:
                return self._reply(413, "payload size")

            body = self.rfile.read(length)
            if not verify(body, self.headers.get("X-Shopify-Hmac-Sha256", ""), secret):
                # No detail: an attacker probing the endpoint learns only that it said no.
                return self._reply(401, "unauthorized")

            topic = self.headers.get("X-Shopify-Topic", "unknown").strip().lower()
            if topic not in SUPPORTED_TOPICS:
                return self._reply(200, "ignored topic")
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._reply(400, "invalid json")
            if not isinstance(payload, dict):
                return self._reply(400, "invalid order")
            if not order_is_paid(payload):
                # A legacy orders/create subscription can still arrive during migration.
                # Acknowledge it without persisting PII or touching inventory.
                return self._reply(200, "ignored unpaid")
            # Fall back to a content hash so a delivery with no id still de-duplicates.
            webhook_id = (self.headers.get("X-Shopify-Webhook-Id")
                          or hashlib.sha256(body).hexdigest())
            fresh = spool.add(webhook_id, topic, body, _order_key(payload))
            # 200 either way: a duplicate is success from Shopify's point of view, and any
            # other answer earns two days of retries for an order already handled.
            self._reply(200, "queued" if fresh else "duplicate")
            if fresh:
                wake.put(webhook_id)

        def log_message(self, fmt, *a):
            # Default logging prints the request line; keep it, drop everything else, and
            # never log headers or bodies — the body is a customer's name and address.
            print(f"  {self.address_string()} {fmt % a}", file=sys.stderr)

    return Handler


def serve(args) -> int:
    error_retention_days = (
        args.error_retention_days
        if args.error_retention_days is not None
        else _retention_days("COREYARD_WEBHOOK_ERROR_RETENTION_DAYS",
                             DEFAULT_ERROR_RETENTION_DAYS)
    )
    ticket_retention_days = (
        args.ticket_retention_days
        if args.ticket_retention_days is not None
        else _retention_days("COREYARD_TICKET_RETENTION_DAYS",
                             DEFAULT_TICKET_RETENTION_DAYS)
    )
    if error_retention_days < 1 or ticket_retention_days < 1:
        raise RuntimeError("webhook retention windows must be at least 1 day")
    secret = webhook_secret()
    spool = EventQueue()
    purged_payloads = spool.purge_error_payloads(error_retention_days)
    purged_tickets = purge_tickets(TICKET_DIR, ticket_retention_days)
    worker = OrderWorker(retire=not args.no_retire, print_cmd=args.print_cmd,
                         write_orders=args.write_orders)
    wake: queue.Queue = queue.Queue()

    def loop():
        while True:
            try:
                # Enforce PII retention even if the service stays up for months and no
                # restart happens to run the startup sweep.
                wake.get(timeout=3600)
            except queue.Empty:
                pass
            try:
                old_payloads = spool.purge_error_payloads(error_retention_days)
                old_tickets = purge_tickets(TICKET_DIR, ticket_retention_days)
                if old_payloads or old_tickets:
                    print(f"  retention sweep: purged {old_payloads} payload(s), "
                          f"{old_tickets} ticket(s)")
                drain(spool, worker)
            except Exception as exc:                      # keep the thread alive
                print(f"  worker error: {exc}", file=sys.stderr)

    threading.Thread(target=loop, daemon=True).start()

    handler = make_handler(spool, secret, args.path, wake)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"CoreYard webhook listening on http://{args.host}:{args.port}{args.path}")
    print(f"  tickets -> {TICKET_DIR}")
    print(f"  printing: {args.print_cmd or 'disabled (--print-cmd to enable)'}")
    print(f"  retire sold parts: {'no' if args.no_retire else 'yes'}")
    print(f"  PII retention: failed payloads {error_retention_days}d, "
          f"tickets {ticket_retention_days}d"
          f" (purged {purged_payloads} payload(s), {purged_tickets} ticket(s))")
    if worker.write_orders:
        # Say the tax stance out loud at startup. It is the one setting whose being wrong
        # costs money quietly, and the only moment anybody reads this line is now.
        try:
            from coreyard.yms import schema as _schema

            write = _schema.load().order_write
            if write is None:
                print("  work orders: ENABLED but schema.json has no 'order_write' section",
                      file=sys.stderr)
            else:
                print(f"  work orders: yes — customer {write.defaults.customer}, line items "
                      f"{'TAXABLE' if write.line_items_taxable else 'not taxable'}")
        except Exception as exc:                      # a bad mapping must not start silently
            print(f"  work orders: ENABLED but the schema will not load: {exc}",
                  file=sys.stderr)
    else:
        print("  work orders: no (read-only; --write-orders or YMS_WRITE_ORDERS=1)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  NOTE: bound to a public interface. Terminate TLS in front of this;\n"
              "        Shopify sends the signature over the wire and it is worth protecting.")
    # Anything queued while the server was down is handled before the first new delivery.
    wake.put("startup")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
        # The daemon worker may be finishing a delivery. Do not close its connection from
        # this thread; process exit will close SQLite after the worker has stopped.
    return 0


# ------------------------------------------------------------- registration CLI ---
_CREATE = """mutation($topic:WebhookSubscriptionTopic!, $sub:WebhookSubscriptionInput!){
  webhookSubscriptionCreate(topic:$topic, webhookSubscription:$sub){
    webhookSubscription{ id topic } userErrors{ field message } } }"""

_LIST = """{ webhookSubscriptions(first:50){ nodes{ id topic uri } } }"""

_DELETE = """mutation($id:ID!){ webhookSubscriptionDelete(id:$id){
  deletedWebhookSubscriptionId userErrors{ field message } } }"""


def _client():
    from coreyard.sink.shopify_api import ShopifyClient

    return ShopifyClient()


def register(args) -> int:
    if not args.url.startswith("https://"):
        raise RuntimeError("Shopify only delivers to https:// endpoints.")
    client = _client()
    existing = {(n["topic"], n.get("uri"))
                for n in client.graphql(_LIST)["webhookSubscriptions"]["nodes"]}
    for topic in args.topics:
        if (topic, args.url) in existing:
            print(f"  {topic}: already registered")
            continue
        res = client.graphql(_CREATE, {"topic": topic,
                                       "sub": {"uri": args.url, "format": "JSON"}})
        errs = res["webhookSubscriptionCreate"]["userErrors"]
        if errs:
            print(f"  {topic}: FAILED {errs}", file=sys.stderr)
        else:
            print(f"  {topic}: registered -> {args.url}")
    return 0


def list_subscriptions(args) -> int:
    for node in _client().graphql(_LIST)["webhookSubscriptions"]["nodes"]:
        url = node.get("uri") or "?"
        print(f"  {node['topic']:<22} {url}   [{node['id'].split('/')[-1]}]")
    return 0


def unregister(args) -> int:
    client = _client()
    for node in client.graphql(_LIST)["webhookSubscriptions"]["nodes"]:
        url = node.get("uri") or ""
        if args.url and url != args.url:
            continue
        res = client.graphql(_DELETE, {"id": node["id"]})["webhookSubscriptionDelete"]
        print(f"  removed {node['topic']} -> {url}"
              if not res["userErrors"] else f"  FAILED {res['userErrors']}")
    return 0


def replay(args) -> int:
    """Render a ticket from a saved order payload — no store, no signature, no network."""
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    worker = OrderWorker(retire=args.retire, print_cmd=args.print_cmd,
                         write_orders=args.write_order)
    done = worker.handle(payload)
    wo = f" -> work order {done.work_order}" if done.work_order else ""
    print(f"{done.order_name}: {len(done.r_numbers)} line(s) -> {done.ticket}{wo}")
    if done.error:
        print(done.error, file=sys.stderr)
        return 1
    return 0


def retry(args) -> int:
    """Retry only the stages that failed for one or all retained deliveries."""
    with EventQueue() as spool:
        webhook_id = None if args.all else args.webhook_id
        count = spool.requeue(webhook_id)
        if not count:
            target = ("retryable deliveries" if args.all
                      else f"retryable delivery {webhook_id}")
            print(f"No {target} found. Its payload may have passed the retention window.")
            return 1
        print_cmd = (_get("COREYARD_PRINT_CMD", "") or "") if args.reprint else None
        worker = OrderWorker(retire=not args.no_retire, print_cmd=print_cmd or None,
                             write_orders=args.write_orders)
        drain(spool, worker)
        remaining = spool.error_count(webhook_id)
        if remaining:
            print(f"{remaining} delivery retry still has an error; "
                  "see `bin/coreyard orders status`.", file=sys.stderr)
            return 1
        return 0


def show(args) -> int:
    with EventQueue() as spool:
        rows = spool.recent_with_ids(args.limit)
    if not rows:
        print("No webhook deliveries recorded yet.")
        return 0
    for webhook_id, received, topic, state, order, r_numbers, ticket, error, work_order in rows:
        # The work order number is the point of reconciliation: it is what somebody
        # compares against the yard system when checking that no sale was missed.
        print(f"  {received[:19]}  {state:<7} {topic:<16} {order or '-':<8} "
              f"WO {work_order or '-':<6} {(error or ticket or '')[:48]}\n"
              f"      webhook id: {webhook_id}")
    return 0


def _poll(args) -> int:
    from coreyard.orders.poll import run

    return run(args)


def _sync_status(args) -> int:
    from coreyard.orders.lifecycle import run

    return run(args)


def main(argv: list[str] | None = None) -> int:
    # Before the parser is built, not after: argparse evaluates its defaults at construction
    # time, so a COREYARD_PRINT_CMD living in .env would otherwise never be seen and tickets
    # would silently not print.
    load_env()
    ap = argparse.ArgumentParser(prog="coreyard.webhook",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="action", required=True)

    s = sub.add_parser("serve", help="run the receiver")
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1 — put a TLS proxy in front)")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--path", default="/webhook")
    s.add_argument("--print-cmd", default=_get("COREYARD_PRINT_CMD", "") or None,
                   help="shell command to print a ticket, e.g. 'lp -d yardprinter' "
                        "or 'lp -d yardprinter {file}'")
    s.add_argument("--no-retire", action="store_true",
                   help="print tickets but leave sold products on sale")
    group = s.add_mutually_exclusive_group()
    group.add_argument("--write-orders", dest="write_orders", action="store_true",
                       default=None,
                       help="create a work order in the yard system for each sale "
                            "(default: YMS_WRITE_ORDERS in .env)")
    group.add_argument("--no-write-orders", dest="write_orders", action="store_false",
                       help="never write to the yard database")
    s.add_argument(
        "--error-retention-days", type=int,
        default=None,
        help=f"retain failed payloads for retry (default {DEFAULT_ERROR_RETENTION_DAYS} days)",
    )
    s.add_argument(
        "--ticket-retention-days", type=int,
        default=None,
        help=f"retain rendered tickets (default {DEFAULT_TICKET_RETENTION_DAYS} days)",
    )
    s.set_defaults(func=serve)

    r = sub.add_parser("register", help="subscribe the store to order webhooks")
    r.add_argument("--url", required=True, help="public https URL of your /webhook endpoint")
    r.add_argument("--topics", nargs="+", default=DEFAULT_TOPICS)
    r.set_defaults(func=register)

    sub.add_parser("list", help="show the store's webhook subscriptions"
                   ).set_defaults(func=list_subscriptions)

    u = sub.add_parser("unregister", help="delete subscriptions")
    u.add_argument("--url", default="", help="only those pointing here (default: all)")
    u.set_defaults(func=unregister)

    p = sub.add_parser("replay", help="render a ticket from a saved order JSON file")
    p.add_argument("file")
    p.add_argument("--print-cmd", default=None)
    p.add_argument("--retire", action="store_true", help="also archive the products")
    p.add_argument("--write-order", action="store_true",
                   help="also create the work order (this WRITES to the yard database)")
    p.set_defaults(func=replay)

    x = sub.add_parser("retry", help="retry failed stages from retained delivery data")
    selection = x.add_mutually_exclusive_group(required=True)
    selection.add_argument("--id", dest="webhook_id", help="one webhook id from status")
    selection.add_argument("--all", action="store_true", help="all retryable errors")
    x.add_argument("--reprint", action="store_true",
                   help="retry printing when that stage failed (can produce paper)")
    x.add_argument("--no-retire", action="store_true",
                   help="do not retry Shopify retirement")
    writes = x.add_mutually_exclusive_group()
    writes.add_argument("--write-orders", dest="write_orders", action="store_true",
                        default=None, help="retry source-system work-order creation")
    writes.add_argument("--no-write-orders", dest="write_orders", action="store_false",
                        help="do not retry work-order creation")
    x.set_defaults(func=retry)

    q = sub.add_parser("status", help="recent deliveries")
    q.add_argument("--limit", type=int, default=20)
    q.set_defaults(func=show)

    # Polling is a second transport onto the same pipeline, for installations that cannot
    # accept an inbound connection. It lives beside `serve` because they are alternatives.
    o = sub.add_parser("poll", help="pull new orders instead of receiving webhooks")
    o.add_argument("--check", action="store_true",
                   help="report scopes and connectivity, then stop")
    o.add_argument("--since", default=None,
                   help="ISO timestamp to start from (default: the stored cursor, else "
                        "two days ago). Passing it does not move the cursor.")
    o.add_argument("--limit", type=int, default=0, help="handle at most this many orders")
    o.add_argument("--include-unpaid", action="store_true",
                   help="queue unpaid orders too (the worker still refuses them)")
    o.add_argument("--print-cmd", default=_get("COREYARD_PRINT_CMD", "") or None)
    o.add_argument("--no-retire", action="store_true",
                   help="print tickets but leave sold products on sale")
    writes_poll = o.add_mutually_exclusive_group()
    writes_poll.add_argument("--write-orders", dest="write_orders", action="store_true",
                             default=None,
                             help="also book each sale into the yard system")
    writes_poll.add_argument("--no-write-orders", dest="write_orders",
                             action="store_false", help="never write to the yard database")
    o.set_defaults(func=_poll)

    y = sub.add_parser("sync-status",
                       help="mark Shopify orders the source system has progressed")
    y.add_argument("--apply", action="store_true",
                   help="write the tags, note and any fulfillment (default: plan)")
    y.set_defaults(func=_sync_status)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
