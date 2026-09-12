"""``coreyard alert`` — tell the operator when the pipeline has stopped, and when it starts.

Every check CoreYard already has answers "is this broken *right now, while you are looking*".
Nobody looks. The failures that cost money here are the quiet ones: a delta cursor that
stopped advancing five days ago, an order stuck in `error` since yesterday evening, a full
sync that has been silently hitting its deadline every hour. Each of those was visible to
``coreyard doctor`` the whole time and was found by a person running it by hand.

So this is the piece that pushes rather than waits. It is deliberately small:

* **It notifies once, not every tick.** A condition is recorded when it starts and
  re-notified only after ``COREYARD_ALERT_REPEAT``. An alerting system that pages every five
  minutes is one people filter into a folder they stop opening, at which point it is worse
  than nothing because everyone believes they are covered.
* **It notifies on recovery too.** "Deltas are flowing again" is what closes the loop; an
  operator who only ever hears about breakage has to go and check whether their fix worked,
  which means the alert did not save them the trip.
* **A condition nobody scheduled is not late.** Staleness is measured against the job's own
  schedule, read from the host (:func:`coreyard.schedule.installed_intervals`). An
  installation that runs no delta job is not behind on deltas, and saying otherwise is the
  fastest way to teach someone to ignore this.
* **It sends no payload and no secret.** Alerts leave the host. Order *names* travel because
  the operator needs them to act; customer data and credentials never do.

Delivery is a command, not a built-in mail or chat client: ``COREYARD_ALERT_COMMAND``
receives the alert on stdin with ``COREYARD_ALERT_*`` in its environment. That keeps CoreYard
free of a transport dependency and a vendor, and every operator already has something that
works — ``mail``, ``curl`` to a webhook, ``ntfy``, a paging CLI. Without it, alerts are
evaluated and printed but nothing is delivered, and ``doctor`` says so, because an alerting
feature nobody configured is not alerting.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coreyard import capabilities, ops
from coreyard.config import DATA_ROOT, _get, out_dir
from coreyard.state import DEFAULT_STATE_DB

WARN, FAIL = "WARN", "FAIL"

# How long a condition must hold before it is worth waking somebody for, per OPS-03. The
# full-sync one is a multiplier on that job's own schedule rather than a duration, because
# "late" for an hourly pass and for a five-minute catch-up are different numbers.
SYNC_STALE_INTERVALS = 2
DELTA_STALE = timedelta(minutes=15)
ORDERS_STUCK = timedelta(minutes=10)
# Below this, the next sync will fail on a write rather than on anything diagnosable.
DISK_FLOOR = 1024 * 1024 * 1024
# A condition that is still true is repeated at most this often.
REPEAT_AFTER = timedelta(hours=6)
# Long enough for a paging CLI to do a round trip, short enough that a wedged notifier
# cannot hold the scheduled tick open until the next one starts.
DELIVERY_TIMEOUT = 30

_DDL = """CREATE TABLE IF NOT EXISTS alerts (
  key TEXT PRIMARY KEY,
  level TEXT NOT NULL,
  summary TEXT NOT NULL,
  since TEXT NOT NULL,
  notified_at TEXT)"""


@dataclass(frozen=True)
class Alert:
    """One condition worth telling the operator about.

    ``key`` is stable across runs and is what matches a recovery to the breakage it
    resolves, so it must name the *condition* rather than the moment — ``sync.delta.stale``,
    never ``delta 4271 minutes behind``.
    """

    key: str
    level: str
    summary: str
    detail: str = ""

    def message(self, state: str = "firing") -> str:
        head = "RESOLVED" if state == "resolved" else self.level
        body = f"[{head}] coreyard {self.key}\n{self.summary}"
        return f"{body}\n\n{self.detail}" if self.detail and state != "resolved" else body


def _duration(key: str, default: timedelta) -> timedelta:
    """A threshold in seconds from configuration, or the default."""
    raw = (_get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return timedelta(seconds=int(raw))
    except ValueError:
        return default


def _age(iso: str | None) -> timedelta | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    now = datetime.now(timezone.utc) if then.tzinfo else datetime.now()
    return now - then


def _minutes(span: timedelta) -> str:
    total = int(span.total_seconds() // 60)
    return f"{total} min" if total < 120 else f"{total // 60}h {total % 60}m"


# ------------------------------------------------------------------ conditions --
def check_sync(intervals: dict[str, int]) -> list[Alert]:
    """A full sync that has not finished within two of its own scheduled intervals.

    Suppressed while one is running: a long full run is not a missing one, and this
    installation's runs legitimately take most of an hour.
    """
    period = intervals.get("sync")
    if not period:
        return []                       # nothing scheduled, so nothing is late
    last = ops.last("sync", "")
    if last is None:
        return []                       # never run: that is a setup state, not an outage
    if ops.running("sync", ""):
        return []
    age = _age(last["finished"] or last["started"])
    limit = timedelta(seconds=period * SYNC_STALE_INTERVALS)
    if age is None or age <= limit:
        return []
    return [Alert("sync.full.stale", FAIL,
                  f"No full sync has finished for {_minutes(age)}; it is scheduled every "
                  f"{_minutes(timedelta(seconds=period))}.",
                  "The storefront is drifting from the yard. Check out/sync.log and "
                  "`coreyard status`.")]


def check_source_age(caps) -> list[Alert]:
    """The source answers, and answers with last week's yard.

    Only a source whose clock dates its data can be stale — a live database read is current
    because it happened. For an export it is the failure nothing else can see: every probe
    passes, the rows parse, and availability is published for stock that was sold days ago.
    CoreYard refuses to write from it, which is the safe outcome and also a silent one
    unless somebody is told.
    """
    from coreyard import source as source_mod

    state = source_mod.freshness(spec=caps.source_spec)
    if not state.stale:
        return []
    return [Alert("source.stale", FAIL,
                  f"The source has not been refreshed: {state.detail}.",
                  "Publishing is refused until it is, so the storefront is frozen where it "
                  "was — parts already listed stay on sale. Refresh the export, or raise "
                  "SOURCE_MAX_AGE_HOURS if this age is normal here.")]


def check_sync_outcome() -> list[Alert]:
    """The last full sync ended, but not well.

    A run that fails or hits its deadline every time is the failure this whole module
    exists for: the job is scheduled, the log grows, the process exits, and nothing is
    published. It is invisible to a staleness check precisely because the job *is* running.
    """
    last = ops.last("sync", "")
    if last is None or last["ok"] and not last["counts"].get("timed_out"):
        return []
    if last["counts"].get("timed_out"):
        return [Alert("sync.full.timeout", WARN,
                      "The last full sync stopped at its deadline before finishing.",
                      "It keeps what it banked, but a run that times out every time never "
                      "reaches the end of its work. Raise --timeout, or reduce the backlog.")]
    code = last.get("exit_code")
    return [Alert("sync.full.failed", FAIL,
                  f"The last full sync failed"
                  f"{f' (exit {code})' if code not in (None, 0) else ''}.",
                  "See out/sync.log from the last run header.")]


def check_delta(caps, intervals: dict[str, int]) -> list[Alert]:
    """The delta cursor has stopped advancing.

    Only a full run writes this cursor, so a frozen cursor does not mean the catch-up job
    is dead — it can equally mean every full run is being cut short before it gets there.
    Both are outages of the same thing, which is why the condition is the cursor's age
    rather than the job's.
    """
    if not caps.enabled("delta"):
        return []
    try:
        from coreyard.state import SyncState

        with SyncState(DEFAULT_STATE_DB) as state:
            cursor = state.get_cursor("delta_modified_at")
    except Exception:
        return []                       # storage is checked separately and says it better
    age = _age(cursor)
    limit = _duration("COREYARD_ALERT_DELTA_STALE", DELTA_STALE)
    if age is None or age <= limit:
        return []
    if ops.running("sync", ""):
        # The catch-up shares the full sync's lock on purpose, so during a full run the
        # cursor is *expected* to age and `doctor` rightly says so. But that suppression
        # has to be bounded, or it inverts: on a host whose hourly full sync takes most of
        # an hour, a sync is almost always running, and "a sync is running" would then
        # explain a cursor frozen for five days as easily as one frozen for six minutes.
        # Past one full cycle plus the threshold, a run in flight is no longer the reason.
        grace = timedelta(seconds=intervals.get("sync", 3600)) + limit
        if age <= grace:
            return []
    return [Alert("sync.delta.stale", FAIL,
                  f"The delta cursor has not advanced for {_minutes(age)}.",
                  "Only a full run advances it, so either the catch-up job has stopped or "
                  "no full run is reaching the end of its work. Check whether the last full "
                  "sync timed out.")]


def check_orders(caps) -> list[Alert]:
    """An order stage that has been pending or failed for too long.

    Separated from everything else because its cost is different in kind. A stale catalogue
    is embarrassing; a paid storefront sale that never reaches the yard is a part still on
    the shelf for the counter to sell a second time.
    """
    from coreyard.orders.pipeline import QUEUE_DB, EventQueue

    if not QUEUE_DB.exists():
        return []
    limit = _duration("COREYARD_ALERT_ORDERS_STUCK", ORDERS_STUCK)
    try:
        with EventQueue(QUEUE_DB) as queue:
            stuck = queue.stuck(datetime.now(timezone.utc) - limit)
    except Exception as exc:
        return [Alert("orders.queue.unreadable", FAIL,
                      f"The order queue cannot be read: {type(exc).__name__}.")]
    if not stuck:
        return []
    failed = [row for row in stuck if row[1] == "error"]
    oldest = _age(stuck[0][2])
    names = ", ".join(sorted({row[3] for row in stuck if row[3]})[:5]) or "unnamed"
    return [Alert("orders.stuck", FAIL,
                  f"{len(stuck)} order event(s) unfinished for over {_minutes(limit)} "
                  f"({len(failed)} failed); oldest {_minutes(oldest) if oldest else '?'}.",
                  f"Orders: {names}. Run `coreyard orders status`, then "
                  f"`coreyard orders retry --id <webhook-id>`.")]


def check_storage() -> list[Alert]:
    """Storage that will stop the next run, reported before it does."""
    found: list[Alert] = []
    workspace = out_dir()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        probe = workspace / ".alert-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        found.append(Alert("storage.unwritable", FAIL,
                           f"{workspace} cannot be written: {exc.strerror or exc}."))
    try:
        free = shutil.disk_usage(DATA_ROOT).free
        if free < DISK_FLOOR:
            found.append(Alert("storage.full", FAIL,
                               f"Only {free // (1024 * 1024)} MiB free on {DATA_ROOT}.",
                               "The next sync will fail on a write rather than on "
                               "anything diagnosable."))
    except OSError:
        pass
    return found


def check_credentials(caps) -> list[Alert]:
    """A capability something enabled here depends on, that is not configured.

    Only `missing`, never `off`: a feature this installation does not use is not an
    incident, and paging somebody about one is how alerting dies.
    """
    from coreyard.capabilities import MISSING

    broken = [(name, detail) for name, state, detail in caps.summary() if state == MISSING]
    if not broken:
        return []
    return [Alert("config.missing", FAIL,
                  f"{len(broken)} required capability(ies) not configured: "
                  f"{', '.join(name for name, _ in broken)}.",
                  "\n".join(f"  {name}: {detail}" for name, detail in broken))]


def evaluate(caps=None, intervals: dict[str, int] | None = None) -> list[Alert]:
    """Everything currently wrong, in the order an operator should read it.

    Each condition is guarded on its own: an alerting run that raises because one probe
    failed would go silent for every other condition at exactly the wrong moment.
    """
    from coreyard import schedule

    caps = caps or capabilities.detect()
    if intervals is None:
        try:
            intervals = schedule.installed_intervals()
            # `installed_intervals` reports the *shortest* schedule per task, which is right
            # for "has this stopped?" — the most frequent job is the one whose silence is
            # evidence. Both sync conditions below ask the opposite question: how long may a
            # full run legitimately take? A task is keyed by its lock, and here the hourly
            # sync, the reconcile and the five-minute delta all take .sync.lock, so the
            # shortest reading made the delta grace 35 minutes instead of 90 and fired while
            # a full sync was working through a backlog. Corrected once, here, rather than in
            # each check — a check that reads the host's crontab itself cannot be tested
            # against a schedule the host does not have.
            if "sync" in intervals:
                intervals["sync"] = schedule.full_cycle_seconds("sync", intervals["sync"])
        except Exception:
            intervals = {}
    found: list[Alert] = []
    for condition in (lambda: check_storage(),
                      lambda: check_credentials(caps),
                      lambda: check_orders(caps),
                      lambda: check_source_age(caps),
                      lambda: check_sync(intervals),
                      lambda: check_sync_outcome(),
                      lambda: check_delta(caps, intervals)):
        try:
            found.extend(condition())
        except Exception as exc:                          # pragma: no cover - a bug
            found.append(Alert("alerts.check_failed", WARN,
                               f"An alert check itself failed: {type(exc).__name__}."))
    return found


# --------------------------------------------------------------------- state --
def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=30)
    conn.execute(_DDL)
    conn.commit()
    return conn


def _open(db: Path) -> dict[str, dict]:
    try:
        conn = _connect(db)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT key, level, summary, since, notified_at FROM alerts").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {row[0]: {"level": row[1], "summary": row[2], "since": row[3],
                     "notified_at": row[4]} for row in rows}


# ------------------------------------------------------------------ delivery --
def deliver(alert: Alert, state: str = "firing") -> tuple[bool, str]:
    """Hand one alert to the configured notifier. Returns ``(delivered, note)``."""
    command = (_get("COREYARD_ALERT_COMMAND", "") or "").strip()
    if not command:
        return False, "no COREYARD_ALERT_COMMAND configured"
    environment = dict(os.environ)
    environment.update({"COREYARD_ALERT_KEY": alert.key,
                        "COREYARD_ALERT_LEVEL": alert.level,
                        "COREYARD_ALERT_STATE": state,
                        "COREYARD_ALERT_SUMMARY": alert.summary})
    try:
        result = subprocess.run(command, shell=True, input=alert.message(state),
                                text=True, capture_output=True, env=environment,
                                timeout=DELIVERY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"notifier did not return within {DELIVERY_TIMEOUT}s"
    except OSError as exc:
        return False, f"notifier could not be run: {exc.strerror or exc}"
    if result.returncode:
        # Never the notifier's stdout: it may echo a URL carrying a token.
        return False, f"notifier exited {result.returncode}"
    return True, "delivered"


def journal(alert: Alert, state: str, note: str) -> None:
    """Append what was decided to ``out/alerts.jsonl``.

    Not the delivery mechanism — OPS-03 is explicit that an alert written only to a log has
    not been delivered. It is the record of what was *sent*, which is what makes a missed
    page answerable afterwards.
    """
    line = {"at": datetime.now(timezone.utc).isoformat(), "key": alert.key,
            "level": alert.level, "state": state, "summary": alert.summary, "note": note}
    try:
        path = out_dir() / "alerts.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
    except OSError:
        pass                            # never let the audit trail break the alert


def reconcile(found: list[Alert], db: Path = DEFAULT_STATE_DB, notify: bool = True,
              repeat: timedelta | None = None) -> dict:
    """Compare what is wrong now with what was wrong last time, and notify the difference.

    This is the whole point of keeping state: three notifications matter — the first time a
    condition appears, a reminder if it is still true much later, and the one that says it
    stopped. Everything else is noise that trains people to ignore the channel.
    """
    repeat = repeat or _duration("COREYARD_ALERT_REPEAT", REPEAT_AFTER)
    now = datetime.now(timezone.utc)
    previous = _open(db)
    current = {alert.key: alert for alert in found}
    sent = {"new": [], "repeated": [], "resolved": [], "failed": []}

    try:
        conn = _connect(db)
    except sqlite3.Error:
        conn = None

    for key, alert in current.items():
        record = previous.get(key)
        since = record["since"] if record else now.isoformat()
        age = _age(record["notified_at"]) if record and record["notified_at"] else None
        due = record is None or record["notified_at"] is None or (
            age is not None and age >= repeat)
        notified_at = record["notified_at"] if record else None
        if due and notify:
            delivered, note = deliver(alert, "firing")
            journal(alert, "firing", note)
            (sent["new"] if record is None else sent["repeated"]).append(alert.key)
            if delivered:
                notified_at = now.isoformat()
            else:
                sent["failed"].append(f"{alert.key}: {note}")
        if conn is not None:
            with conn:
                conn.execute(
                    "INSERT INTO alerts(key, level, summary, since, notified_at)"
                    " VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
                    " level=excluded.level, summary=excluded.summary,"
                    " notified_at=excluded.notified_at",
                    (key, alert.level, alert.summary, since, notified_at))

    for key, record in previous.items():
        if key in current:
            continue
        resolved = Alert(key, record["level"], f"Resolved: {record['summary']}")
        if notify and record["notified_at"]:
            # Only if the breakage was actually announced. A recovery notice for something
            # nobody was told about is a page that reads as an incident.
            _, note = deliver(resolved, "resolved")
            journal(resolved, "resolved", note)
            sent["resolved"].append(key)
        if conn is not None:
            with conn:
                conn.execute("DELETE FROM alerts WHERE key = ?", (key,))

    if conn is not None:
        conn.close()
    return sent


# --------------------------------------------------------------------- CLI --
def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with the alerting flags."""
    ap.add_argument("--check", action="store_true",
                    help="evaluate and report; notify nothing and remember nothing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--test", metavar="KEY", nargs="?", const="alerts.test",
                    help="deliver one synthetic alert through the configured notifier, to "
                         "prove the channel works before an outage needs it")
    ap.set_defaults(func=run)
    return ap


def run(args) -> int:
    if args.test:
        probe = Alert(args.test, WARN, "Test alert from `coreyard alert --test`.",
                      "If you are reading this, delivery works. Nothing is wrong.")
        delivered, note = deliver(probe, "firing")
        journal(probe, "test", note)
        print(f"test alert: {note}")
        return 0 if delivered else 1

    found = evaluate()
    if args.check:
        sent = {"new": [], "repeated": [], "resolved": [], "failed": []}
    else:
        sent = reconcile(found)

    if args.json:
        print(json.dumps({"alerts": [{"key": a.key, "level": a.level,
                                      "summary": a.summary, "detail": a.detail}
                                     for a in found], "sent": sent}, indent=2))
        return 2 if any(a.level == FAIL for a in found) else 0

    if not found:
        print("Nothing wrong.")
    for alert in found:
        print(f"  [{alert.level:<4}] {alert.key:<22} {alert.summary}")
        if alert.detail:
            for line in alert.detail.splitlines():
                print(f"         {line}")
    if sent["resolved"]:
        print(f"\n  resolved: {', '.join(sent['resolved'])}")
    if sent["failed"]:
        for failure in sent["failed"]:
            print(f"  ! not delivered — {failure}")
    elif found and not args.check and not (sent["new"] or sent["repeated"]):
        print("\n  (already notified; next reminder after "
              f"{_minutes(_duration('COREYARD_ALERT_REPEAT', REPEAT_AFTER))})")
    return 2 if any(a.level == FAIL for a in found) else 0


def main(argv: list[str] | None = None) -> int:
    from coreyard.config import load_env

    load_env()
    parser = argparse.ArgumentParser(prog="coreyard alert",
                                     description=__doc__.splitlines()[0])
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
