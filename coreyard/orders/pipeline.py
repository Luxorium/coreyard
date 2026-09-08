"""The order pipeline: what happens to a paid storefront order, whatever brought it in.

A paid order has to become two things — optionally a work order in the source system, and an
archived product so the part cannot be sold twice — and each has to survive the other
failing. The work order is the deliverable: the source system already renders one in a
printable form, so CoreYard does not produce a document of its own. That work is here
rather than in the webhook
receiver because the receiver is only one way an order arrives. An installation behind NAT
with no inbound port polls instead (:mod:`coreyard.orders.poll`), and a person replaying a
saved payload is a third. All three feed the same queue and the same worker, so an order is
processed identically and de-duplicated across every route.

Two properties drive the design:

* **Shopify wants a 2xx within about five seconds** and retries for two days otherwise. So a
  delivery is verified, written to the queue and acknowledged immediately; the slow work — a
  database lookup, a source-system transaction, an API call — happens on a worker. A booking
  that takes eight seconds must not turn into four duplicate orders.
* **Delivery is at-least-once.** Shopify resends on any doubt, so the queue de-duplicates on
  both the delivery ID and the order ID. A retry, a topic migration, or a poll that overlaps
  a webhook cannot book the same order twice.

Order payloads carry a customer's name, phone and address. Nothing here logs a body, and no
copy is written to disk; successful payloads are erased immediately and failed ones live only
in the owner-only queue for a bounded retry window.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from coreyard.config import DATA_ROOT, _get, load_env, load_store

QUEUE_DB = DATA_ROOT / "coreyard_webhook_queue.sqlite3"
DEFAULT_ERROR_RETENTION_DAYS = 7

# How many booking attempts an order gets in total, the first being the live one at the time
# of sale and the rest automatic re-attempts on later ticks. Bounded on purpose: the causes divide into two kinds and only one of them clears by
# waiting. A yard database that was down, a lock held too long, a timeout — those are gone by
# the next tick. "R#nnnnn is not available in the quantity ordered" is not: the part has left
# the shelf, and no number of retries puts it back. Retrying the first kind forever is how a
# transient blip needs a human; retrying the second kind forever is how a real decision hides
# behind a job that looks busy. Five ticks is under an hour at the installed schedule.
DEFAULT_BOOKING_ATTEMPTS = 5


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


def _order_name(payload: dict) -> str:
    """The order's human name, e.g. "#1042".

    Kept identical to the reference ``yms.orders`` writes into the work order, because that
    string is the cross-reference a person uses to tie a source-system order back to the
    storefront one — and the duplicate guard keys on it.
    """
    return payload.get("name") or f"#{payload.get('order_number', '')}"


def booking_attempts() -> int:
    """Total booking attempts allowed per order, counting the original.

    So the default of 5 is one live booking plus four automatic re-attempts. Below 2 there
    is no retry pass at all, which is the way to turn the behaviour off.
    """
    raw = (_get("COREYARD_ORDER_BOOKING_ATTEMPTS",
                str(DEFAULT_BOOKING_ATTEMPTS)) or "").strip()
    try:
        attempts = int(raw or DEFAULT_BOOKING_ATTEMPTS)
    except ValueError:
        raise RuntimeError("COREYARD_ORDER_BOOKING_ATTEMPTS must be a whole number, "
                           f"got {raw!r}") from None
    return max(0, attempts)


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
    booking_error: str = ""
    retire_error: str = ""

    def retry_stages(self) -> set[str] | None:
        """Stages that failed last time; None means this is a fresh/full attempt."""
        if self.attempts == 0 or self.fatal_error:
            return None
        stages = set()
        if self.booking_error:
            stages.add("book")
        if self.retire_error:
            stages.add("retire")
        return stages or None


class EventQueue:
    """Durable, de-duplicating spool of received webhooks.

    Only the fields the work order needs are kept. An order payload carries a name, email,
    phone and street address, so it is erased immediately after success. A failed event retains its
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
            " error TEXT,"
            " work_order TEXT,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " fatal_error TEXT,"
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
                "SELECT webhook_id, topic, payload, attempts, fatal_error,"
                " booking_error, retire_error FROM events"
                " WHERE state='pending' AND payload IS NOT NULL ORDER BY received_at"
            )
            return [QueuedEvent(*(value or "" if i >= 4 else value
                                  for i, value in enumerate(row))) for row in rows]

    def finish(self, webhook_id: str, order_name: str, r_numbers: list[str],
               error: str = "", work_order: str = "", booking_error: str = "",
               retire_error: str = "") -> None:
        problems = [e for e in (error, booking_error, retire_error) if e]
        combined = "; ".join(problems)
        with self.lock:
            self.conn.execute(
                "UPDATE events SET state=?,"
                " payload=CASE WHEN ? THEN payload ELSE NULL END,"
                " order_name=COALESCE(NULLIF(?, ''), order_name),"
                " r_numbers=COALESCE(NULLIF(?, ''), r_numbers), error=?,"
                " work_order=COALESCE(NULLIF(?, ''), work_order),"
                " attempts=attempts+1, fatal_error=?, booking_error=?,"
                " retire_error=? WHERE webhook_id=?",
                ("error" if combined else "done", bool(combined), order_name,
                 ",".join(r_numbers), combined, work_order, error,
                 booking_error, retire_error, webhook_id),
            )
            self.conn.commit()

    def stuck(self, older_than: "datetime") -> list[tuple[str, str, str, str]]:
        """``(webhook_id, state, received_at, order_name)`` for events that have not
        finished and were received before ``older_than``.

        Deliberately returns no payload. This feeds an alert, an alert is delivered to
        somewhere outside this host, and the payload is the one field in this table that
        carries customer PII. An order *name* is a reference the operator needs in order to
        act; a customer's address is never needed to say that a booking is stuck.
        """
        cutoff = older_than.isoformat()
        with self.lock:
            rows = self.conn.execute(
                "SELECT webhook_id, state, received_at, COALESCE(order_name, '')"
                " FROM events WHERE state IN ('pending','error') AND received_at < ?"
                " ORDER BY received_at", (cutoff,)).fetchall()
        return [tuple(row) for row in rows]

    def recent(self, limit: int = 20) -> list[tuple]:
        with self.lock:
            return list(self.conn.execute(
                "SELECT received_at, topic, state, order_name, r_numbers, error,"
                " work_order FROM events ORDER BY received_at DESC LIMIT ?", (limit,)))

    def recent_with_ids(self, limit: int = 20) -> list[tuple]:
        with self.lock:
            return list(self.conn.execute(
                "SELECT webhook_id, received_at, topic, state, order_name, r_numbers,"
                " error, work_order FROM events"
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

    def requeue_retryable(self, max_attempts: int) -> int:
        """Make parked error rows pending again — but only while they have attempts left.

        The unbounded :meth:`requeue` above is what ``orders retry`` calls, because a person
        asking for a retry has looked at the failure and decided it is worth another go.
        Nothing has looked at this one, so it needs a stop: an order refused because its part
        is gone would otherwise be re-attempted every ten minutes until somebody noticed the
        log, which is the state this was meant to end.
        """
        if max_attempts < 1:
            return 0
        with self.lock:
            result = self.conn.execute(
                "UPDATE events SET state='pending' WHERE state='error'"
                " AND payload IS NOT NULL AND attempts < ?", (max_attempts,))
            self.conn.commit()
            return result.rowcount

    def exhausted(self, max_attempts: int) -> list[tuple[str, str]]:
        """``(webhook_id, order_name)`` for parked orders that have run out of attempts."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT webhook_id, COALESCE(order_name, '') FROM events"
                " WHERE state='error' AND payload IS NOT NULL AND attempts >= ?"
                " ORDER BY received_at", (max_attempts,)).fetchall()
        return [tuple(row) for row in rows]

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

    A failure to book the work order must not erase which parts it was for — that is exactly
    the information somebody needs in order to finish the job by hand.
    """

    order_name: str = ""
    r_numbers: list[str] = field(default_factory=list)
    work_order: str = ""
    booking_error: str = ""
    retire_error: str = ""

    @property
    def error(self) -> str:
        return "; ".join(e for e in (self.booking_error, self.retire_error) if e)


class OrderWorker:
    """Turns a queued order into a source-system work order and an archived product."""

    def __init__(self, retire: bool = True,
                 write_orders: Optional[bool] = None,
                 state_db: Optional[Path] = None) -> None:
        self.retire = retire
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
        """Process one order: book the work order, delist the parts.

        The work order is the deliverable. The source system renders it in a printable form
        of its own, so CoreYard produces no document here — booking the sale is what puts a
        pullable order in front of the counter, and the two stages fail independently.
        """
        from coreyard.yms.inventory import fetch_parts_by_r_number

        if not order_is_paid(payload):
            status = str(payload.get("financial_status") or "missing")
            raise RuntimeError(
                f"refusing to process an order whose financial status is {status!r}"
            )

        retrying = stages is not None
        wanted = stages or {"book", "retire"}
        skus = [str(i.get("sku") or "").strip()
                for i in (payload.get("line_items") or []) if i.get("sku")]
        parts = {}
        lookup_error = ""
        if skus:
            try:
                parts = fetch_parts_by_r_number(skus)
            except Exception as exc:
                lookup_error = str(exc)
                # The yard database being unreachable must not lose the order: the delisting
                # below still has to happen, and the booking is reported as a retryable
                # failure rather than throwing the delivery away.
                print(f"  ! part lookup failed ({exc}); work order will be skipped",
                      file=sys.stderr)

        result = Handled(order_name=_order_name(payload), r_numbers=skus)
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
        and losing the delisting too because the yard system was unreachable would turn one
        problem into two. The transaction either committed in full or
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


def drain(spool: EventQueue, worker: OrderWorker) -> int:
    """Process everything pending. Returns how many were handled."""
    handled = 0
    for event in spool.pending():
        try:
            done = worker.handle(json.loads(event.payload), event.retry_stages())
            spool.finish(
                event.webhook_id, done.order_name, done.r_numbers,
                work_order=done.work_order,
                booking_error=done.booking_error, retire_error=done.retire_error,
            )
            wo = f" -> work order {done.work_order}" if done.work_order else " -> not booked"
            print(f"  {event.topic} {done.order_name}: {len(done.r_numbers)} line(s){wo}")
            if done.error:
                print(f"  {event.topic} {done.order_name}: {done.error}", file=sys.stderr)
        except Exception as exc:
            spool.finish(event.webhook_id, "", [], str(exc))
            print(f"  {event.topic} {event.webhook_id}: FAILED {exc}", file=sys.stderr)
        handled += 1
    return handled
