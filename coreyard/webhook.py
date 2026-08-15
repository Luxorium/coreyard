"""Receive Shopify order webhooks: print a pull ticket, take the part off sale.

    python -m coreyard.webhook register --url https://yard.example.com/webhook
    python -m coreyard.webhook serve
    python -m coreyard.webhook replay <order-file.json>     # re-print without a live order
    python -m coreyard.webhook list | unregister

Nothing here writes to the source database. The order is read from Shopify, the part is read
from the yard database, and the only write goes back to Shopify (archive the sold product).
The paperwork a worker needs is produced as a printable ticket instead — see
``transform/pull_ticket.py`` for why that join can only happen here.

Two properties drive the design:

* **Shopify wants a 2xx within about five seconds** and retries for two days otherwise. So the
  handler verifies, writes the event to a queue, and answers immediately; the slow work (a
  database lookup, a print, an API call) happens on a worker thread. A ticket that takes eight
  seconds to render must not turn into four duplicate orders.
* **Delivery is at-least-once.** Shopify will resend on any doubt, so the queue is keyed on
  ``X-Shopify-Webhook-Id`` and a repeat is dropped rather than reprinted.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from coreyard.config import REPO_ROOT, _get, load_env, load_store

QUEUE_DB = REPO_ROOT / "coreyard_webhook_queue.sqlite3"
TICKET_DIR = REPO_ROOT / "out" / "tickets"
MAX_BODY = 2 * 1024 * 1024      # a fat order is ~100 KB; past 2 MB something is wrong
DEFAULT_TOPICS = ["ORDERS_CREATE"]


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


# ------------------------------------------------------------------------ queue ---
class EventQueue:
    """Durable, de-duplicating spool of received webhooks.

    Only the fields the ticket needs are kept. An order payload carries a name, email, phone
    and street address, so it is stored no longer than it takes to print — the queue holds the
    rendered ticket path and the R#s, and the raw payload is deleted once handled.
    """

    def __init__(self, path: Path = QUEUE_DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " webhook_id TEXT PRIMARY KEY,"
            " topic TEXT NOT NULL,"
            " received_at TEXT NOT NULL,"
            " payload TEXT,"          # cleared once handled: it holds customer PII
            " state TEXT NOT NULL,"   # pending | done | error
            " order_name TEXT,"
            " r_numbers TEXT,"
            " ticket TEXT,"
            " error TEXT)"
        )
        self.conn.commit()

    def add(self, webhook_id: str, topic: str, payload: bytes) -> bool:
        """Record a new event. Returns False if this delivery was already seen."""
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO events(webhook_id, topic, received_at, payload, state)"
                    " VALUES (?,?,?,?, 'pending')",
                    (webhook_id, topic, datetime.now(timezone.utc).isoformat(),
                     payload.decode("utf-8", "replace")),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False        # at-least-once delivery: Shopify sent it twice

    def pending(self) -> list[tuple[str, str, str]]:
        with self.lock:
            return list(self.conn.execute(
                "SELECT webhook_id, topic, payload FROM events WHERE state='pending'"
                " ORDER BY received_at"))

    def finish(self, webhook_id: str, order_name: str, r_numbers: list[str],
               ticket: str, error: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE events SET state=?, payload=NULL, order_name=?, r_numbers=?,"
                " ticket=?, error=? WHERE webhook_id=?",
                ("error" if error else "done", order_name, ",".join(r_numbers),
                 ticket, error, webhook_id),
            )
            self.conn.commit()

    def recent(self, limit: int = 20) -> list[tuple]:
        with self.lock:
            return list(self.conn.execute(
                "SELECT received_at, topic, state, order_name, r_numbers, ticket, error"
                " FROM events ORDER BY received_at DESC LIMIT ?", (limit,)))


# ----------------------------------------------------------------------- worker ---
class OrderWorker:
    """Turns a queued order into a printed ticket and an archived product."""

    def __init__(self, retire: bool = True, print_cmd: Optional[str] = None) -> None:
        self.retire = retire
        self.print_cmd = print_cmd
        self.store = load_store()
        self._publisher = None
        TICKET_DIR.mkdir(parents=True, exist_ok=True)

    def publisher(self):
        # Built on first use so `serve` starts (and can answer Shopify) even if the Shopify
        # credentials are wrong — the failure then shows up per-order in the queue, not as a
        # server that refuses to boot and silently drops every delivery.
        if self._publisher is None:
            from coreyard.sink.shopify_write import ShopifyPublisher

            self._publisher = ShopifyPublisher(store=self.store)
        return self._publisher

    def handle(self, payload: dict) -> tuple[str, list[str], str]:
        """Process one order. Returns (order name, R#s, ticket path)."""
        from coreyard.transform import pull_ticket
        from coreyard.yms.inventory import fetch_parts_by_r_number

        skus = [str(i.get("sku") or "").strip()
                for i in (payload.get("line_items") or []) if i.get("sku")]
        parts = {}
        if skus:
            try:
                parts = fetch_parts_by_r_number(skus)
            except Exception as exc:
                # The yard database being unreachable must not lose the order. Print the
                # ticket with the Shopify half filled in and the bin column flagged.
                print(f"  ! part lookup failed ({exc}); printing without yard detail",
                      file=sys.stderr)

        ticket = pull_ticket.from_order(payload, parts, self.store)
        path = TICKET_DIR / f"{_safe_name(ticket.order_name)}.html"
        path.write_text(pull_ticket.render(ticket, self.store), encoding="utf-8")
        self._print(path)

        if self.retire:
            for sku in skus:
                try:
                    self.publisher().retire(sku)
                except Exception as exc:
                    print(f"  ! could not retire R#{sku}: {exc}", file=sys.stderr)
        return ticket.order_name, skus, str(path)

    def _print(self, path: Path) -> None:
        if not self.print_cmd:
            return
        cmd = (self.print_cmd.replace("{file}", str(path)) if "{file}" in self.print_cmd
               else f"{self.print_cmd} {path}")
        try:
            subprocess.run(cmd, shell=True, check=True, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            # A dead printer is not a reason to lose the order; the file is already on disk.
            print(f"  ! print command failed: {exc}", file=sys.stderr)


def _safe_name(order_name: str) -> str:
    keep = [c for c in order_name if c.isalnum() or c in "-_"]
    return ("order-" + "".join(keep)) if keep else f"order-{int(datetime.now().timestamp())}"


def drain(spool: EventQueue, worker: OrderWorker) -> int:
    """Process everything pending. Returns how many were handled."""
    handled = 0
    for webhook_id, topic, raw in spool.pending():
        try:
            order_name, r_numbers, ticket = worker.handle(json.loads(raw))
            spool.finish(webhook_id, order_name, r_numbers, ticket)
            print(f"  {topic} {order_name}: {len(r_numbers)} line(s) -> {ticket}")
        except Exception as exc:
            spool.finish(webhook_id, "", [], "", str(exc))
            print(f"  {topic} {webhook_id}: FAILED {exc}", file=sys.stderr)
        handled += 1
    return handled


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

            topic = self.headers.get("X-Shopify-Topic", "unknown")
            # Fall back to a content hash so a delivery with no id still de-duplicates.
            webhook_id = (self.headers.get("X-Shopify-Webhook-Id")
                          or hashlib.sha256(body).hexdigest())
            fresh = spool.add(webhook_id, topic, body)
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
    secret = webhook_secret()
    spool = EventQueue()
    worker = OrderWorker(retire=not args.no_retire, print_cmd=args.print_cmd)
    wake: queue.Queue = queue.Queue()

    def loop():
        while True:
            wake.get()
            try:
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
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  NOTE: bound to a public interface. Terminate TLS in front of this;\n"
              "        Shopify sends the signature over the wire and it is worth protecting.")
    # Anything queued while the server was down is handled before the first new delivery.
    wake.put("startup")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    return 0


# ------------------------------------------------------------- registration CLI ---
_CREATE = """mutation($topic:WebhookSubscriptionTopic!, $sub:WebhookSubscriptionInput!){
  webhookSubscriptionCreate(topic:$topic, webhookSubscription:$sub){
    webhookSubscription{ id topic } userErrors{ field message } } }"""

_LIST = """{ webhookSubscriptions(first:50){ nodes{ id topic
  endpoint{ ... on WebhookHttpEndpoint { callbackUrl } } } } }"""

_DELETE = """mutation($id:ID!){ webhookSubscriptionDelete(id:$id){
  deletedWebhookSubscriptionId userErrors{ field message } } }"""


def _client():
    from coreyard.sink.shopify_api import ShopifyClient

    return ShopifyClient()


def register(args) -> int:
    if not args.url.startswith("https://"):
        raise RuntimeError("Shopify only delivers to https:// endpoints.")
    client = _client()
    existing = {(n["topic"], (n["endpoint"] or {}).get("callbackUrl"))
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
        url = (node["endpoint"] or {}).get("callbackUrl", "?")
        print(f"  {node['topic']:<22} {url}   [{node['id'].split('/')[-1]}]")
    return 0


def unregister(args) -> int:
    client = _client()
    for node in client.graphql(_LIST)["webhookSubscriptions"]["nodes"]:
        url = (node["endpoint"] or {}).get("callbackUrl", "")
        if args.url and url != args.url:
            continue
        res = client.graphql(_DELETE, {"id": node["id"]})["webhookSubscriptionDelete"]
        print(f"  removed {node['topic']} -> {url}"
              if not res["userErrors"] else f"  FAILED {res['userErrors']}")
    return 0


def replay(args) -> int:
    """Render a ticket from a saved order payload — no store, no signature, no network."""
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    worker = OrderWorker(retire=args.retire, print_cmd=args.print_cmd)
    order_name, r_numbers, ticket = worker.handle(payload)
    print(f"{order_name}: {len(r_numbers)} line(s) -> {ticket}")
    return 0


def show(args) -> int:
    rows = EventQueue().recent(args.limit)
    if not rows:
        print("No webhook deliveries recorded yet.")
        return 0
    for received, topic, state, order, r_numbers, ticket, error in rows:
        print(f"  {received[:19]}  {state:<7} {topic:<16} {order or '-':<8} "
              f"{(error or ticket or '')[:60]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard.webhook",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="action", required=True)

    s = sub.add_parser("serve", help="run the receiver")
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1 — put a TLS proxy in front)")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--path", default="/webhook")
    s.add_argument("--print-cmd", default=os.environ.get("COREYARD_PRINT_CMD") or None,
                   help="shell command to print a ticket, e.g. 'lp -d yardprinter' "
                        "or 'lp -d yardprinter {file}'")
    s.add_argument("--no-retire", action="store_true",
                   help="print tickets but leave sold products on sale")
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
    p.set_defaults(func=replay)

    q = sub.add_parser("status", help="recent deliveries")
    q.add_argument("--limit", type=int, default=20)
    q.set_defaults(func=show)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
