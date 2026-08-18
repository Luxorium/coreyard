"""Receive Shopify order webhooks: print a pull ticket, take the part off sale.

    python -m coreyard.webhook register --url https://yard.example.com/webhook
    python -m coreyard.webhook serve
    python -m coreyard.webhook replay <order-file.json>     # re-print without a live order
    python -m coreyard.webhook list | unregister

The paperwork a worker needs is produced as a printable ticket — see
``transform/pull_ticket.py`` for why that join can only happen here.

With ``--write-orders`` (or ``YMS_WRITE_ORDERS=1``) the sale is also booked into the yard
system as a work order, via ``yms/orders.py``. That is the only write CoreYard makes to the
source database and it is off unless an installation asks for it; everything else here reads
Shopify, reads the yard database, and writes back only to Shopify. The ticket prints whether
or not the booking succeeds, because it is the artefact a person can act on when a system is
down.

Two properties drive the design:

* **Shopify wants a 2xx within about five seconds** and retries for two days otherwise. So the
  handler verifies, writes the event to a queue, and answers immediately; the slow work (a
  database lookup, a print, an API call) happens on a worker thread. A ticket that takes eight
  seconds to render must not turn into four duplicate orders.
* **Delivery is at-least-once.** Shopify will resend on any doubt, so the queue de-duplicates
  both the delivery ID and the order ID. A retry or a create-to-paid topic migration cannot
  print or book the same order twice.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import queue
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from coreyard.config import REPO_ROOT, _get, load_env, load_store

QUEUE_DB = REPO_ROOT / "coreyard_webhook_queue.sqlite3"
TICKET_DIR = REPO_ROOT / "out" / "tickets"
MAX_BODY = 2 * 1024 * 1024      # a fat order is ~100 KB; past 2 MB something is wrong
DEFAULT_TOPICS = ["ORDERS_PAID"]
SUPPORTED_TOPICS = {"orders/paid", "orders/create"}  # create retained for migration only
DEFAULT_ERROR_RETENTION_DAYS = 7
DEFAULT_TICKET_RETENTION_DAYS = 30


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


def order_is_paid(payload: object) -> bool:
    """Only a fully paid order may reserve stock or become a source-system order."""
    if not isinstance(payload, dict):
        return False
    return str(payload.get("financial_status") or "").strip().lower() == "paid"


def _order_key(payload: dict) -> str:
    """Stable order identity across topics and different webhook delivery IDs."""
    return str(
        payload.get("admin_graphql_api_id")
        or payload.get("id")
        or payload.get("name")
        or ""
    ).strip()


def _write_private(path: Path, content: str) -> None:
    """Create or replace a PII-bearing file with owner-only permissions."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(content)
    path.chmod(0o600)


def purge_tickets(directory: Path, retention_days: int) -> int:
    """Delete generated pull tickets older than the configured PII retention window."""
    if retention_days < 1 or not directory.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
    removed = 0
    for path in directory.glob("*.html"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:  # another cleanup pass won the race
            continue
    return removed


def _retention_days(key: str, default: int) -> int:
    raw = (_get(key, str(default)) or str(default)).strip()
    try:
        days = int(raw)
    except ValueError:
        raise RuntimeError(f"{key} must be a whole number of days, got {raw!r}") from None
    if days < 1:
        raise RuntimeError(f"{key} must be at least 1 day")
    return days


# ------------------------------------------------------------------------ queue ---
@dataclass(frozen=True)
class QueuedEvent:
    webhook_id: str
    topic: str
    payload: str
    attempts: int = 0
    fatal_error: str = ""
    print_error: str = ""
    booking_error: str = ""
    retire_error: str = ""

    def retry_stages(self) -> set[str] | None:
        """Stages that failed last time; None means this is a fresh/full attempt."""
        if self.attempts == 0 or self.fatal_error:
            return None
        stages = set()
        if self.print_error:
            stages.add("print")
        if self.booking_error:
            stages.add("book")
        if self.retire_error:
            stages.add("retire")
        return stages or None


class EventQueue:
    """Durable, de-duplicating spool of received webhooks.

    Only the fields the ticket needs are kept. An order payload carries a name, email, phone
    and street address, so it is erased immediately after success. A failed event retains its
    payload for a short, configured retry window in this owner-only database.
    """

    def __init__(self, path: Path = QUEUE_DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        # Clearing a successful payload must overwrite its old bytes, not leave customer
        # PII recoverable from free SQLite pages.
        self.conn.execute("PRAGMA secure_delete = ON")
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
            " error TEXT,"
            " work_order TEXT,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " fatal_error TEXT,"
            " print_error TEXT,"
            " booking_error TEXT,"
            " retire_error TEXT,"
            " order_key TEXT)"
        )
        # Added when order write-back arrived. Existing spools upgrade in place rather than
        # being recreated, so a queue mid-flight when the service restarts is not lost.
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(events)")}
        additions = {
            "work_order": "TEXT",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "fatal_error": "TEXT",
            "print_error": "TEXT",
            "booking_error": "TEXT",
            "retire_error": "TEXT",
            "order_key": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS events_order_key"
            " ON events(order_key) WHERE order_key IS NOT NULL AND order_key<>''"
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "EventQueue":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def add(self, webhook_id: str, topic: str, payload: bytes,
            order_key: str = "") -> bool:
        """Record an order once across retries, topics, and delivery IDs."""
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO events(webhook_id, topic, received_at, payload, state,"
                    " order_key) VALUES (?,?,?,?, 'pending', ?)",
                    (webhook_id, topic, datetime.now(timezone.utc).isoformat(),
                     payload.decode("utf-8", "replace"), order_key or None),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False        # at-least-once delivery: Shopify sent it twice

    def pending(self) -> list[QueuedEvent]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT webhook_id, topic, payload, attempts, fatal_error, print_error,"
                " booking_error, retire_error FROM events"
                " WHERE state='pending' AND payload IS NOT NULL ORDER BY received_at"
            )
            return [QueuedEvent(*(value or "" if i >= 4 else value
                                  for i, value in enumerate(row))) for row in rows]

    def finish(self, webhook_id: str, order_name: str, r_numbers: list[str],
               ticket: str, error: str = "", work_order: str = "",
               print_error: str = "", booking_error: str = "",
               retire_error: str = "") -> None:
        problems = [e for e in (error, print_error, booking_error, retire_error) if e]
        combined = "; ".join(problems)
        with self.lock:
            self.conn.execute(
                "UPDATE events SET state=?,"
                " payload=CASE WHEN ? THEN payload ELSE NULL END,"
                " order_name=COALESCE(NULLIF(?, ''), order_name),"
                " r_numbers=COALESCE(NULLIF(?, ''), r_numbers),"
                " ticket=COALESCE(NULLIF(?, ''), ticket), error=?,"
                " work_order=COALESCE(NULLIF(?, ''), work_order),"
                " attempts=attempts+1, fatal_error=?, print_error=?, booking_error=?,"
                " retire_error=? WHERE webhook_id=?",
                ("error" if combined else "done", bool(combined), order_name,
                 ",".join(r_numbers), ticket, combined, work_order, error,
                 print_error, booking_error, retire_error, webhook_id),
            )
            self.conn.commit()

    def recent(self, limit: int = 20) -> list[tuple]:
        with self.lock:
            return list(self.conn.execute(
                "SELECT received_at, topic, state, order_name, r_numbers, ticket, error,"
                " work_order FROM events ORDER BY received_at DESC LIMIT ?", (limit,)))

    def recent_with_ids(self, limit: int = 20) -> list[tuple]:
        with self.lock:
            return list(self.conn.execute(
                "SELECT webhook_id, received_at, topic, state, order_name, r_numbers,"
                " ticket, error, work_order FROM events"
                " ORDER BY received_at DESC LIMIT ?", (limit,)))

    def requeue(self, webhook_id: str | None = None) -> int:
        """Make retryable error rows pending again, preserving their failed stages."""
        sql = "UPDATE events SET state='pending' WHERE state='error' AND payload IS NOT NULL"
        values: tuple = ()
        if webhook_id:
            sql += " AND webhook_id=?"
            values = (webhook_id,)
        with self.lock:
            result = self.conn.execute(sql, values)
            self.conn.commit()
            return result.rowcount

    def error_count(self, webhook_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM events WHERE state='error' AND payload IS NOT NULL"
        values: tuple = ()
        if webhook_id:
            sql += " AND webhook_id=?"
            values = (webhook_id,)
        with self.lock:
            return int(self.conn.execute(sql, values).fetchone()[0])

    def purge_error_payloads(self, retention_days: int) -> int:
        """Erase retry payloads after their bounded retention window expires."""
        if retention_days < 1:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self.lock:
            result = self.conn.execute(
                "UPDATE events SET payload=NULL WHERE state='error' AND payload IS NOT NULL"
                " AND received_at < ?", (cutoff,)
            )
            self.conn.commit()
            return result.rowcount


# ----------------------------------------------------------------------- worker ---
@dataclass
class Handled:
    """What became of one order, including independently retryable stage failures.

    A failure to book the work order must not erase the fact that the ticket printed and
    which parts it was for — that is exactly the information somebody needs in order to
    finish the job by hand.
    """

    order_name: str = ""
    r_numbers: list[str] = field(default_factory=list)
    ticket: str = ""
    work_order: str = ""
    print_error: str = ""
    booking_error: str = ""
    retire_error: str = ""

    @property
    def error(self) -> str:
        return "; ".join(e for e in (
            self.print_error, self.booking_error, self.retire_error
        ) if e)


class OrderWorker:
    """Turns a queued order into a printed ticket, a work order, and an archived product."""

    def __init__(self, retire: bool = True, print_cmd: Optional[str] = None,
                 write_orders: Optional[bool] = None,
                 state_db: Optional[Path] = None) -> None:
        self.retire = retire
        self.print_cmd = print_cmd
        # Which sync-state file holds the retirement memory. Injectable so a test never
        # opens — let alone writes to — the installation's real state database.
        self.state_db = state_db
        # Off unless the installation opts in: everything else CoreYard does to the source
        # database is a SELECT, and an upgrade must never start writing to somebody's
        # system of record on its own.
        if write_orders is None:
            load_env()
            write_orders = (_get("YMS_WRITE_ORDERS", "") or "").strip().lower() in (
                "1", "true", "yes", "on")
        self.write_orders = write_orders
        self.store = load_store()
        self._publisher = None
        self.ticket_dir = TICKET_DIR
        self.ticket_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.ticket_dir.chmod(0o700)

    def publisher(self):
        # Built on first use so `serve` starts (and can answer Shopify) even if the Shopify
        # credentials are wrong — the failure then shows up per-order in the queue, not as a
        # server that refuses to boot and silently drops every delivery.
        if self._publisher is None:
            from coreyard.sink.shopify_write import ShopifyPublisher

            self._publisher = ShopifyPublisher(store=self.store)
        return self._publisher

    @contextmanager
    def _retirement_memory(self):
        """Yield a recorder for each part's pre-archive status, or None if unavailable.

        The sync state file is what lets a refunded or voided sale put a part back on the
        storefront as whatever it was. It is not this worker's own database, though, and a
        paid order still has to be delisted even if that file is missing, locked by a long
        sync, or owned by another user — so every failure here degrades to "don't
        remember" rather than leaving a sold part on sale.
        """
        from coreyard.state import DEFAULT_STATE_DB, SyncState

        state = None
        try:
            state = SyncState(self.state_db or DEFAULT_STATE_DB)
        except Exception as exc:
            print(f"  ! sync state unavailable, statuses not remembered: {exc}",
                  file=sys.stderr)

        def remember(r_number: str, status: str) -> None:
            try:
                state.record_retired(r_number, status)
            except Exception as exc:
                print(f"  ! could not remember R#{r_number} status {status}: {exc}",
                      file=sys.stderr)

        try:
            yield remember if state is not None else None
        finally:
            if state is not None:
                state.close()

    def handle(self, payload: dict, stages: set[str] | None = None) -> Handled:
        """Process one order: print the ticket, book the work order, delist the parts.

        The ticket is produced first and unconditionally. It is the one artefact a person
        can act on without any system being up, so it must not depend on the database write
        succeeding — an oversell, a deadlock or a dropped SMB pipe still leaves a puller
        holding a sheet of paper with a bin location on it.
        """
        from coreyard.transform import pull_ticket
        from coreyard.yms.inventory import fetch_parts_by_r_number

        if not order_is_paid(payload):
            status = str(payload.get("financial_status") or "missing")
            raise RuntimeError(
                f"refusing to process an order whose financial status is {status!r}"
            )

        retrying = stages is not None
        wanted = stages or {"print", "book", "retire"}
        skus = [str(i.get("sku") or "").strip()
                for i in (payload.get("line_items") or []) if i.get("sku")]
        parts = {}
        lookup_error = ""
        if skus:
            try:
                parts = fetch_parts_by_r_number(skus)
            except Exception as exc:
                lookup_error = str(exc)
                # The yard database being unreachable must not lose the order. Print the
                # ticket with the Shopify half filled in and the bin column flagged.
                print(f"  ! part lookup failed ({exc}); printing without yard detail",
                      file=sys.stderr)

        ticket = pull_ticket.from_order(payload, parts, self.store)
        path = self.ticket_dir / f"{_safe_name(ticket.order_name)}.html"
        _write_private(path, pull_ticket.render(ticket, self.store))

        result = Handled(order_name=ticket.order_name, r_numbers=skus, ticket=str(path))
        if "print" in wanted:
            if retrying and not self.print_cmd:
                result.print_error = "print retry skipped; pass --reprint to try again"
            else:
                result.print_error = self._print(path)

        if "book" in wanted:
            if lookup_error and self.write_orders:
                result.booking_error = (
                    f"work order not created: part lookup failed: {lookup_error}"
                )
            elif self.write_orders:
                result.work_order, result.booking_error = self._book(payload, parts)
            elif retrying:
                result.booking_error = (
                    "work-order retry skipped; enable YMS_WRITE_ORDERS or --write-orders"
                )

        if "retire" in wanted and self.retire:
            failed = []
            with self._retirement_memory() as remember:
                for sku in skus:
                    try:
                        self.publisher().retire(sku, record_prior=remember)
                    except Exception as exc:
                        print(f"  ! could not retire R#{sku}: {exc}", file=sys.stderr)
                        failed.append(f"R#{sku}: {exc}")
            if failed:
                result.retire_error = "retirement failed: " + ", ".join(failed)
        elif "retire" in wanted and retrying:
            result.retire_error = "retirement retry skipped; omit --no-retire"
        return result

    def _book(self, payload: dict, parts: dict) -> tuple[str, str]:
        """Create the work order. Returns (order number, error) — never raises.

        A failure here is reported, not thrown: the sale has already happened on Shopify,
        and losing the ticket and the delisting because the yard system was unreachable
        would turn one problem into three. The transaction either committed in full or
        changed nothing, so a retry after the cause is fixed is always safe.
        """
        from coreyard.yms import orders

        try:
            order, skipped = orders.from_shopify(payload, parts)
            if skipped:
                print(f"  . not yard parts, left off the work order: {', '.join(skipped)}")
            if not order.lines:
                return "", ""       # nothing of ours on this order; not an error
            outcome = orders.create(order)
            number = str(outcome.order_number or "")
            print(f"  work order {number}"
                  f"{' already existed (duplicate delivery)' if outcome.duplicate else ''}")
            return number, ""
        except Exception as exc:
            print(f"  ! could not create the work order: {exc}", file=sys.stderr)
            return "", f"work order not created: {exc}"

    def _print(self, path: Path) -> str:
        if not self.print_cmd:
            return ""
        quoted = shlex.quote(str(path))
        cmd = (self.print_cmd.replace("{file}", quoted) if "{file}" in self.print_cmd
               else f"{self.print_cmd} {quoted}")
        try:
            subprocess.run(cmd, shell=True, check=True, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            # A dead printer is not a reason to lose the order; the file is already on disk.
            print(f"  ! print command failed: {exc}", file=sys.stderr)
            return f"print failed: {exc}"
        return ""


def _safe_name(order_name: str) -> str:
    keep = [c for c in order_name if c.isalnum() or c in "-_"]
    return ("order-" + "".join(keep)) if keep else f"order-{int(datetime.now().timestamp())}"


def drain(spool: EventQueue, worker: OrderWorker) -> int:
    """Process everything pending. Returns how many were handled."""
    handled = 0
    for event in spool.pending():
        try:
            done = worker.handle(json.loads(event.payload), event.retry_stages())
            spool.finish(
                event.webhook_id, done.order_name, done.r_numbers, done.ticket,
                work_order=done.work_order, print_error=done.print_error,
                booking_error=done.booking_error, retire_error=done.retire_error,
            )
            wo = f" -> work order {done.work_order}" if done.work_order else ""
            print(f"  {event.topic} {done.order_name}: {len(done.r_numbers)} line(s) "
                  f"-> {done.ticket}{wo}")
            if done.error:
                print(f"  {event.topic} {done.order_name}: {done.error}", file=sys.stderr)
        except Exception as exc:
            spool.finish(event.webhook_id, "", [], "", str(exc))
            print(f"  {event.topic} {event.webhook_id}: FAILED {exc}", file=sys.stderr)
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

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
