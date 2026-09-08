"""``coreyard doctor`` — is this installation wired up, and is the pipeline still running?

Two questions, one command, because in practice they are asked together and an operator
should not have to know which one their symptom belongs to:

    installation  can this machine reach the database, the photo share and Shopify at all,
                  with the credentials, schema mapping and config files it has been given?
    liveness      are the scheduled jobs still doing their work? Every job here writes to a
                  log and exits, nothing reads those logs, and a failure looks exactly like
                  success — the storefront simply stops changing. That is not hypothetical:
                  when the source server's IP moved, every sync failed for as long as it
                  took a human to happen to look.

Every check is asked against :mod:`coreyard.capabilities` first, because "not configured"
and "broken" are different answers and only one of them is worth waking somebody for. A
yard running ``COREYARD_SOURCE=tabular:parts.csv`` has no SMB credentials to be missing and
no ``schema.json`` to map; reporting three FAIL lines for a correct installation is the same
defect as a check that stays lit for something already fixed, and it is worse, because the
first thing a new customer sees is a diagnosis that their working install is broken.

Exit status is 0 healthy, 1 degraded (warnings), 2 failed, so cron, a monitor, or a person
can all use it. Writes nothing to Shopify or the yard database.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from coreyard import __version__, capabilities, ops
from coreyard.capabilities import MISSING, ON
from coreyard.config import out_dir
from coreyard.ops import RUN_HEADER
from coreyard.state import DEFAULT_STATE_DB

STATE_DB = DEFAULT_STATE_DB
LOG_DIR = out_dir()

OK, WARN, FAIL = "OK", "WARN", "FAIL"
RANK = {OK: 0, WARN: 1, FAIL: 2}

# The delta job runs every 5 minutes and rewinds its cursor by a 5-minute overlap, so a
# healthy cursor is always ~5-10 minutes behind. Alert well past that but inside the hour,
# so a stuck pipeline is caught long before the next full sync would have masked it.
CURSOR_STALE = timedelta(minutes=30)
# The full sync only runs during business hours on weekdays, so this has to tolerate a
# weekend. It is here to catch "the full run has stopped entirely", not to time it.
SNAPSHOT_STALE = timedelta(hours=30)
# The order poller runs every ten minutes, all week. Three missed ticks is past any
# plausible slow run and still catches a dead poller inside half an hour.
ORDERS_STALE = timedelta(minutes=35)

# How long a sale may sit in the queue unbooked before it is a problem rather than a
# retry. A failed booking is not a stale log: the poller keeps running, keeps finding the
# order in its overlap window, and keeps reporting OK while the sale waits. Two poll ticks,
# so a failure that the next run clears never fires.
ORDERS_STUCK = timedelta(minutes=20)

# A job that dies before its first line of output leaves no traceback: the interpreter or
# the shell says its piece on stderr and exits 1. That is exactly how the order poller
# failed silently for a day — the storefront scripts moved into the CoreYard CLI, cron kept
# invoking the paths they used to live at, and every tick logged one "can't open file ...
# No such file or directory" that matched none of the patterns below. "The job never
# started" is the failure least likely to be noticed by hand, so it gets its own patterns.
FATAL = ("Traceback", "No such file or directory", "command not found",
         "ModuleNotFoundError", "ImportError", "cannot import name")

Result = tuple


def _age(then: datetime) -> timedelta:
    """Age of a timestamp, honouring which clock it was written by.

    The two timestamps in the state file are not the same kind, and treating them alike is
    an easy way to invent a five-hour outage that is not happening:

    * ``parts.last_seen`` is written by this tool as an aware UTC isoformat.
    * the delta cursor is the *source server's* ``GETDATE()`` — a naive local wall clock.

    So a naive value is compared against local wall time, not against UTC.
    """
    if then.tzinfo is None:
        return datetime.now() - then
    return datetime.now(timezone.utc) - then


def _span(gap: timedelta) -> str:
    """A duration read at a glance: minutes until there are too many of them."""
    total = int(gap.total_seconds() // 60)
    return f"{total} min" if total < 120 else f"{total // 60}h {total % 60}m"


def _received(stamp: str) -> timedelta:
    """How long ago a queue row arrived, or zero if its timestamp is unreadable.

    An unparseable timestamp must not turn a real stuck-order report into a crash that
    ``collect`` renders as "check itself failed" — the order still needs saying.
    """
    try:
        return _age(datetime.fromisoformat(stamp))
    except (TypeError, ValueError):
        return timedelta(0)


# ------------------------------------------------------------- installation --
def check_capabilities(*, caps=None) -> list[Result]:
    """What this installation is configured to do — before asking whether it works.

    This is the section that makes every skipped check below legible. Without it an
    operator sees no ``smbclient`` line and no photo-share line and cannot tell whether
    they were checked and passed or never ran; with it the report says "photos: local
    directory ./photos" and the absent SMB lines explain themselves.

    A capability that is simply off is one line, collapsed with the others, because a
    report that spends eleven lines listing things this site deliberately does not use is
    a report people stop reading. A capability that is *required by something enabled here*
    and not configured is a FAIL on its own line, with the setting that fixes it.
    """
    caps = caps or capabilities.detect()
    out: list[Result] = []
    off: list[str] = []
    for name, state, detail in caps.summary():
        if state == MISSING:
            out.append((FAIL, name, detail))
        elif state == ON:
            out.append((OK, name, detail))
        else:
            off.append(name)
    if off:
        out.append((OK, "not in use", ", ".join(off)))
    return out


def check_environment(*, caps=None) -> list[Result]:
    """The things that are nobody's fault but stop everything: a missing tool, a read-only
    directory, an interpreter too old."""
    caps = caps or capabilities.detect()
    out: list[Result] = []
    # Only the SMB photo path shells out to smbclient. Demanding it from an installation
    # that reads its photographs off a local directory is asking for a package that will
    # never be called.
    if caps.source_kind == "database" and caps.enabled("photos"):
        if shutil.which("smbclient"):
            out.append((OK, "smbclient", "on PATH"))
        else:
            out.append((FAIL, "smbclient",
                        "not on PATH — photo fetching shells out to it "
                        "(install samba-client)"))
    workspace = out_dir()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        probe = workspace / ".doctor-write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        out.append((OK, "out/", "writable"))
    except OSError as exc:
        out.append((FAIL, "out/", f"not writable: {exc}"))
    if os.access(STATE_DB.parent, os.W_OK):
        out.append((OK, "state-db", f"{STATE_DB.name} directory writable"))
    else:
        out.append((FAIL, "state-db",
                    f"{STATE_DB.parent} is not writable — the sync cannot record state"))
    return out


def check_configuration(*, caps=None) -> list[Result]:
    """The config files, and whether the settings this installation needs are present.

    Every one of these is a thing CoreYard deliberately does not ship, because it belongs to
    the installation rather than to the project — which also means every one of them is a
    thing that can be missing. Which of them are *required* is not fixed: it follows from
    the capabilities above, which is why the credentials and the schema mapping are checked
    there and only the files are checked here.
    """
    from coreyard.config import ENV_PATH

    # `capabilities.detect()` has already loaded `.env` at the command boundary. Loading it
    # again from inside a check is how the site's real configuration reaches a unit test.
    caps = caps or capabilities.detect()
    out: list[Result] = []
    # Not a failure on its own: a container or a systemd unit legitimately supplies every
    # setting through the real environment, and `capabilities` has already reported
    # anything actually missing by name.
    if ENV_PATH.exists():
        out.append((OK, ".env", f"{ENV_PATH.name} present"))
    else:
        out.append((WARN, ".env",
                    f"no {ENV_PATH.name} — settings must come from the environment "
                    f"(run `coreyard init` to write one)"))

    try:
        from coreyard.validate import _configured, validate

        paths = _configured()
        if not any(paths.values()):
            out.append((OK, "config files", "none configured (defaults apply)"))
        else:
            report = validate(**paths)
            named = ", ".join(name for name, path in paths.items() if path)
            out.append((OK, "config files", f"{named} valid") if report.ok
                       else (FAIL, "config files", "invalid — run `coreyard validate`"))
    except Exception as exc:
        out.append((WARN, "config files", f"could not check: {str(exc)[:100]}"))
    return out


def check_liveness(*, caps=None) -> list[Result]:
    """Can this machine actually reach the services it has been configured to use?

    Asked through the configured :class:`~coreyard.source.Source` rather than through
    ``yms.db`` directly, so a tabular installation is diagnosed against the file it reads
    instead of against a named pipe it will never open. A capability that is off is not
    probed at all: an unreachable service is a failure, but a service this site does not
    use is not a service.
    """
    caps = caps or capabilities.detect()
    out: list[Result] = []

    source = caps.get("source")
    if source.state == MISSING:
        out.append((FAIL, "source", source.detail))
    else:
        try:
            from coreyard.source import load

            line = str(load(caps.source_spec).ping()).splitlines()[0]
            out.append((OK, "source", f"{caps.source_kind}: {line[:90]}"))
        except Exception as exc:
            out.append((FAIL, "source", f"{type(exc).__name__}: {str(exc)[:110]}"))

    photos = caps.get("photos")
    if photos.state == MISSING:
        out.append((FAIL, "photos", photos.detail))
    elif photos.enabled and caps.source_kind == "database":
        try:
            from coreyard.yms.images import SmbImageStore

            SmbImageStore().list_inventory_images("0")
            out.append((OK, "photo-share", "reachable"))
        except Exception as exc:
            out.append((FAIL, "photo-share", f"{type(exc).__name__}: {str(exc)[:110]}"))
    elif photos.enabled:
        out.append((OK, "photo-dir", photos.detail))

    shopify = caps.get("shopify")
    if shopify.state == MISSING:
        out.append((FAIL, "shopify", shopify.detail))
    elif shopify.enabled:
        try:
            from coreyard.sink.shopify_api import ShopifySink

            out.append((OK, "shopify", f"connected to {ShopifySink().check()}"))
        except Exception as exc:
            out.append((FAIL, "shopify", f"{type(exc).__name__}: {str(exc)[:110]}"))
    return out


def check_publishing(*, caps=None) -> list[Result]:
    """The location stock is set at, and the channels a product has to reach to be visible.

    Status alone never made a product visible: an ACTIVE product on no sales channel still
    returns 404 to every shopper and to Google, which is a failure that looks like success
    in every report except a customer's.
    """
    caps = caps or capabilities.detect()
    out: list[Result] = []
    # Nothing here is answerable without the Admin API, and a CSV-sink installation is not
    # a broken one. `capabilities` has already said the store credentials are absent.
    if not caps.enabled("shopify"):
        return out
    try:
        from coreyard.sink.shopify_write import ShopifyPublisher

        publisher = ShopifyPublisher()
        out.append((OK, "location", publisher.location or "?"))
        if publisher.publications:
            out.append((OK, "channels", f"{len(publisher.publications)} channel(s)"))
        else:
            out.append((WARN, "channels",
                        "STORE_PUBLICATIONS unset — new products reach no sales channel"))
        out.extend(check_unpublished(publisher.client))
    except Exception as exc:
        out.append((WARN, "publishing", f"{type(exc).__name__}: {str(exc)[:110]}"))
    return out


# One product-search count, not a page of the catalogue, so this can run on every doctor.
# Shopify caps the count at 10,000 and says so in `precision`; "at least 10,000" is still
# exactly the alarm worth raising.
_UNPUBLISHED = """query($q:String){ productsCount(query:$q){ count precision } }"""


def check_unpublished(client) -> list[Result]:
    """ACTIVE products that reached no sales channel — a 404 wearing a green status.

    This is the failure that looks like success everywhere else: the product exists, is
    ACTIVE, has stock, a price and photographs, and every count in every report includes it.
    Only a shopper finds out, and only by being sent a link that does not load. It went
    unnoticed across 12,900 products because the one check that could see it lived behind
    `status --deep`, which pages the whole store and so is never run casually. A count is
    cheap enough to run every time.
    """
    try:
        result = client.graphql(
            _UNPUBLISHED, {"q": "status:active AND published_status:unpublished"})
        node = result.get("productsCount") or {}
        count = int(node.get("count") or 0)
        at_least = str(node.get("precision") or "") == "AT_LEAST"
    except Exception as exc:
        return [(WARN, "unpublished", f"{type(exc).__name__}: {str(exc)[:110]}")]
    if not count:
        return [(OK, "unpublished", "no ACTIVE product is missing its sales channel")]
    return [(FAIL, "unpublished",
             f"{'at least ' if at_least else ''}{count} ACTIVE product(s) are on no sales "
             f"channel and return 404 — run `bin/coreyard reconcile --apply`")]


# ----------------------------------------------------------------- liveness --
def _suppression_grace() -> timedelta:
    """How long a running full sync may excuse an ageing delta cursor.

    One full cycle past the staleness threshold: beyond that, a run in flight is no longer
    the explanation, because a cursor is only written when a full run reaches its end.
    """
    try:
        from coreyard.schedule import installed_intervals

        period = installed_intervals().get("sync", 3600)
    except Exception:
        period = 3600
    return timedelta(seconds=period) + CURSOR_STALE


def check_freshness(*, caps=None) -> list[Result]:
    caps = caps or capabilities.detect()
    out: list[Result] = []
    if not STATE_DB.exists():
        # A new installation has not run a sync yet. That is the expected state five
        # minutes after `coreyard init`, and telling a new customer their install has
        # FAILED is how the first thing they see becomes a wrong diagnosis.
        return [(WARN, "state", f"no sync state at {STATE_DB.name} yet — "
                                f"run `coreyard sync --dry-run`, then `coreyard sync`")]
    try:
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        return [(FAIL, "state", f"cannot open sync state: {exc}")]
    with conn:
        try:
            row = conn.execute(
                "SELECT value FROM cursors WHERE name = 'delta_modified_at'"
            ).fetchone()
        except sqlite3.Error:
            row = None
        if not caps.enabled("delta"):
            # Nothing advances this cursor on an installation with no delta query, so its
            # age says nothing about the pipeline. Timing a clock nobody winds is the
            # purest form of a check that is lit for a non-problem.
            pass
        elif not row:
            out.append((WARN, "delta", "no delta cursor yet (run a full sync)"))
        else:
            age = _age(datetime.fromisoformat(row[0]))
            minutes = int(age.total_seconds() // 60)
            if age <= CURSOR_STALE:
                out.append((OK, "delta", f"cursor {minutes} min old"))
            elif ops.running("sync", "") and age <= _suppression_grace():
                # The catch-up job shares the full sync's lock on purpose: a full run
                # supersedes it, so the tick is skipped rather than raced. During a long
                # full sync the cursor is *expected* to age, and calling that a failure
                # every hour is how a check earns itself a permanent place in the ignored
                # pile.
                #
                # Bounded, though. On a host whose hourly full sync takes most of an hour a
                # sync is almost *always* running, so an unbounded excuse inverts the
                # check: it explained a cursor frozen for five days exactly as readily as
                # one frozen for six minutes, and reported the five days as OK.
                out.append((OK, "delta", f"cursor {minutes} min old — catch-up is "
                                         f"skipped while a full sync holds the lock"))
            else:
                out.append((FAIL, "delta", f"cursor {minutes} min old"))

        try:
            newest = conn.execute("SELECT MAX(last_seen) FROM parts").fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0]
        except sqlite3.Error as exc:
            return out + [(FAIL, "snapshot", f"unreadable: {exc}")]
        if not newest:
            out.append((FAIL, "snapshot", "snapshot is empty"))
        else:
            age = _age(datetime.fromisoformat(newest))
            level = FAIL if age > SNAPSHOT_STALE else OK
            hours = age.total_seconds() / 3600
            out.append((level, "snapshot", f"{count:,} parts, updated {hours:.1f}h ago"))
    return out


def check_orders(*, caps=None) -> list[Result]:
    """Watch the one pipeline whose silence costs money rather than face.

    A stale catalog is embarrassing. A storefront sale that never reaches the yard is a part
    still on the shelf for the counter to sell twice, and a work order nobody ever raised —
    so this is checked separately from the catalog freshness above.

    Freshness has to come from the log, not from the poll cursor. The cursor only advances
    when an order actually arrives, so on a quiet Tuesday a healthy poller and a dead one
    leave identical state. What happens every ten minutes either way is that the job runs and
    says how many orders it saw, so an orders.log that has stopped growing is the signal.
    """
    caps = caps or capabilities.detect()
    log = LOG_DIR / "orders.log"
    # A log that exists is evidence the job ran, whatever the configuration says — and it
    # is the only evidence a *polling* site leaves, because polling needs no webhook secret
    # and so turns the capability on nowhere. Gating on the capability alone would have
    # stopped watching the transport that is hardest to notice failing.
    if not caps.enabled("orders") and not log.exists():
        # Order receipt is opt-in. A site that never registered a webhook and never runs
        # the poller has no log to be stale, and reporting a missing one as a failure
        # tells that operator to fix something they deliberately did not turn on.
        return []
    if not log.exists():
        return [(WARN, "orders",
                 "order receipt is configured but out/orders.log does not exist — "
                 "start `coreyard orders serve`, or schedule `coreyard orders poll`")]
    try:
        age = timedelta(seconds=time.time() - log.stat().st_mtime)
    except OSError as exc:
        return [(WARN, "orders", f"cannot stat orders.log: {exc}")]
    mins = int(age.total_seconds() // 60)
    if age > ORDERS_STALE:
        return [(FAIL, "orders",
                 f"poller silent {mins} min — storefront sales are not reaching the yard")]
    return [(OK, "orders", f"polled {mins} min ago")]


def check_order_queue(queue_db=None) -> list[Result]:
    """Sales that reached the queue and never came out of it.

    ``check_orders`` above watches the transport — is the poller still running? This watches
    the outcome, and the two fail independently. A booking is refused for reasons outside
    CoreYard: the part was sold at the counter first, or the yard database was unreachable.
    The event is then parked in ``error`` and deliberately not retried on its own, because a
    booking whose cause is unfixed only fails again. Nothing on a schedule reads that queue,
    so until somebody runs ``orders status`` by hand the only symptom is a work order that
    does not exist.

    That is not hypothetical. Order #1012 was refused on 2026-09-06 because its part had
    left the shelf two days earlier, and waited 43 hours behind a healthy
    ``[OK] orders  polled 6 min ago`` — the transport was never the thing that was wrong.

    Read-only, and never opens a queue that is not already there: ``EventQueue`` creates its
    database on construction, and a diagnostic must not leave one behind on a host that has
    never taken an order. Reports order names and never the payload, the queue's one
    PII-bearing column: the reference is what an operator acts on, and a customer's address
    says nothing about why a booking failed.
    """
    from coreyard.orders import pipeline

    path = pipeline.QUEUE_DB if queue_db is None else queue_db
    if not path.exists():
        return []
    try:
        with pipeline.EventQueue(path) as queue:
            stuck = queue.stuck(datetime.now(timezone.utc) - ORDERS_STUCK)
    except Exception as exc:
        return [(WARN, "bookings", f"order queue unreadable: {type(exc).__name__}")]
    if not stuck:
        return [(OK, "bookings", "every order booked")]
    failed = sum(1 for _id, state, _at, _name in stuck if state == "error")
    names = ", ".join(sorted({name for *_, name in stuck if name})[:5]) or "unnamed"
    return [(FAIL, "bookings",
             f"{len(stuck)} order(s) unbooked for over {_span(ORDERS_STUCK)} "
             f"({failed} failed), oldest {_span(_received(stuck[0][2]))} — {names}; "
             f"run `coreyard orders status`, then `orders retry --id <webhook-id>`")]


def check_alerting(*, caps=None) -> list[Result]:
    """Is anything actually going to tell somebody?

    The one check whose subject is the checks. Everything else here reports what is wrong
    to whoever ran the command; this reports whether anyone finds out when nobody does. A
    WARN rather than a FAIL: an operator who watches the pipeline another way has not
    misconfigured anything, but the gap is worth naming, because it is invisible until an
    outage has already gone unnoticed.
    """
    caps = caps or capabilities.detect()
    if not caps.enabled("alerting"):
        return [(WARN, "alerting",
                 "no COREYARD_ALERT_COMMAND — nothing will notify you when the pipeline "
                 "stops; see `coreyard alert --help`")]
    out: list[Result] = [(OK, "alerting", caps.get("alerting").detail)]
    try:
        from coreyard import alerts

        firing = alerts.evaluate(caps=caps)
    except Exception as exc:
        return out + [(WARN, "alerts", f"could not evaluate: {str(exc)[:90]}")]
    for alert in firing:
        out.append((FAIL if alert.level == alerts.FAIL else WARN,
                    alert.key.split(".")[0], alert.summary))
    return out


def check_logs(within=timedelta(hours=2), *, caps=None) -> list[Result]:
    out: list[Result] = []
    cutoff = time.time() - within.total_seconds()
    for log in sorted(LOG_DIR.glob("*.log")):
        # Never read this check's own output. It prints the warnings it finds, so scanning it
        # makes every warning reappear as a fresh warning on the next run, compounding into
        # nested "[WARN] health [WARN] health …" noise that outlives the problem it described.
        if log.stem == "health":
            continue
        try:
            if log.stat().st_mtime < cutoff:
                continue
            lines = log.read_text(errors="replace").splitlines()[-400:]
        except OSError:
            continue
        # Only the most recent run's output. Without this an error is re-reported until
        # enough traffic pushes it out of the window: this installation was still being
        # warned about an ImportError fixed a day earlier and about script paths retired
        # in a migration before that, because the jobs that log them print a handful of
        # lines per tick. An alert that stays lit for a fixed problem is how people learn
        # to ignore alerts.
        starts = [i for i, ln in enumerate(lines) if ln.startswith(RUN_HEADER)]
        if starts:
            tail = lines[starts[-1]:]
        else:
            # A log nothing here writes (a storefront script, a backup). No boundary to
            # find, so fall back to a shallow window.
            tail = lines[-150:]
        bad = [ln for ln in tail
               if any(sig in ln for sig in FATAL) or ln.lstrip().startswith("!!")
               or "FAILED" in ln or "REFUSING" in ln]
        if bad:
            out.append((WARN, log.stem, bad[-1].strip()[:110]))
    return out


# --------------------------------------------------------------------- CLI --
INSTALLATION = (check_capabilities, check_environment, check_configuration)
NETWORK = (check_liveness, check_publishing)
PIPELINE = (check_freshness, check_orders, check_order_queue,
            check_alerting, check_logs)


def collect(checks, caps=None) -> list[Result]:
    """Run each check, and never let one failing check hide the rest of the report.

    The capability snapshot is resolved once and handed to every check, so a report cannot
    describe two different installations because a setting changed while it was printing.
    """
    caps = caps or capabilities.detect()
    results: list[Result] = []
    for check in checks:
        try:
            # A check that does not care which installation this is says so by not
            # accepting the argument. Decided from the signature rather than by catching
            # TypeError, which would re-run a check that raised one from inside itself.
            wants = "caps" in inspect.signature(check).parameters
            results.extend(check(caps=caps) if wants else check())
        except Exception as exc:
            results.append((FAIL, check.__name__.replace("check_", ""),
                            f"check itself failed: {type(exc).__name__}: {str(exc)[:90]}"))
    return results


def verdict(results) -> str:
    return {0: OK, 1: WARN, 2: FAIL}[max((RANK[lvl] for lvl, _, _ in results), default=0)]


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with the diagnostic flags."""
    ap.add_argument("--no-network", action="store_true",
                    help="skip the checks that talk to the database, share or Shopify")
    ap.add_argument("--install-only", action="store_true",
                    help="check the installation, not whether scheduled jobs are running")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true", help="print only when not healthy")
    ap.set_defaults(func=run)
    return ap


# The three questions, in the order an operator asks them. Grouping them is not decoration:
# "source" means the configured one in the first section and the reachable one in the
# second, and a flat list made those two lines look like one line printed twice.
SECTIONS = (
    ("what this installation is set up to do", INSTALLATION),
    ("whether it can reach what it uses", NETWORK),
    ("whether the scheduled work is still happening", PIPELINE),
)


def run(args) -> int:
    caps = capabilities.detect()
    skip = set()
    if args.no_network:
        skip.add(NETWORK)
    if args.install_only:
        skip.add(PIPELINE)
    sections = [(label, collect(checks, caps))
                for label, checks in SECTIONS if checks not in skip]
    results = [row for _, rows in sections for row in rows]
    outcome = verdict(results)

    if args.json:
        print(json.dumps({"verdict": outcome,
                          "version": __version__,
                          "source": caps.source_kind,
                          "capabilities": {name: state
                                           for name, state, _ in caps.summary()},
                          "checks": [{"section": label, "level": lvl, "name": name,
                                      "detail": detail}
                                     for label, rows in sections
                                     for lvl, name, detail in rows]}, indent=2))
    elif not args.quiet or outcome != OK:
        print(f"CoreYard doctor: {outcome}")
        for label, rows in sections:
            if not rows:
                continue
            print(f"\n  {label}")
            for level, name, detail in rows:
                print(f"    [{level:<4}] {name:<14} {detail}")
        if outcome != OK:
            print("\nFix the FAIL lines first; a WARN is usually a deliberate choice.")
    return RANK[outcome]
