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

Exit status is 0 healthy, 1 degraded (warnings), 2 failed, so cron, a monitor, or a person
can all use it. Writes nothing to Shopify or the yard database.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from coreyard import ops
from coreyard.config import REPO_ROOT
from coreyard.ops import RUN_HEADER
from coreyard.state import DEFAULT_STATE_DB

STATE_DB = DEFAULT_STATE_DB
LOG_DIR = REPO_ROOT / "out"

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


# ------------------------------------------------------------- installation --
def check_environment() -> list[Result]:
    """The things that are nobody's fault but stop everything: a missing tool, a read-only
    directory, an interpreter too old."""
    out: list[Result] = []
    if shutil.which("smbclient"):
        out.append((OK, "smbclient", "on PATH"))
    else:
        out.append((FAIL, "smbclient",
                    "not on PATH — photo fetching shells out to it (install samba-client)"))
    out_dir = REPO_ROOT / "out"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".doctor-write-probe"
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


def check_configuration() -> list[Result]:
    """`.env`, the schema mapping, and the four external config files.

    Every one of these is a thing CoreYard deliberately does not ship, because it belongs to
    the installation rather than to the project — which also means every one of them is a
    thing that can be missing.
    """
    from coreyard.config import load_env

    out: list[Result] = []
    load_env()
    required = ("SMB_HOST", "SMB_USER", "SMB_PASSWORD")
    missing = [key for key in required if not (os.environ.get(key) or "").strip()]
    out.append((FAIL, ".env", f"missing {', '.join(missing)}") if missing
               else (OK, ".env", "source credentials present"))

    if (os.environ.get("SHOPIFY_ADMIN_TOKEN") or "").strip():
        out.append((OK, "shopify-token", "present"))
    else:
        out.append((WARN, "shopify-token",
                    "no SHOPIFY_ADMIN_TOKEN — run `coreyard oauth` (CSV output still works)"))

    try:
        from coreyard.yms.inventory import is_configured

        if is_configured():
            out.append((OK, "schema.json", "mapped"))
        else:
            out.append((FAIL, "schema.json",
                        "unmapped — see README 'Map your database', or run `coreyard schema`"))
    except Exception as exc:
        out.append((FAIL, "schema.json", f"{type(exc).__name__}: {str(exc)[:100]}"))

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


def check_liveness() -> list[Result]:
    out: list[Result] = []
    try:
        from coreyard.yms.db import ping

        ping()
        out.append((OK, "source-db", "reachable"))
    except Exception as exc:
        out.append((FAIL, "source-db", f"{type(exc).__name__}: {str(exc)[:110]}"))
    try:
        from coreyard.yms.images import SmbImageStore

        SmbImageStore().list_inventory_images("0")
        out.append((OK, "photo-share", "reachable"))
    except Exception as exc:
        out.append((FAIL, "photo-share", f"{type(exc).__name__}: {str(exc)[:110]}"))
    try:
        from coreyard.sink.shopify_api import ShopifySink

        out.append((OK, "shopify", f"connected to {ShopifySink().check()}"))
    except Exception as exc:
        out.append((FAIL, "shopify", f"{type(exc).__name__}: {str(exc)[:110]}"))
    return out


def check_publishing() -> list[Result]:
    """The location stock is set at, and the channels a product has to reach to be visible.

    Status alone never made a product visible: an ACTIVE product on no sales channel still
    returns 404 to every shopper and to Google, which is a failure that looks like success
    in every report except a customer's.
    """
    out: list[Result] = []
    try:
        from coreyard.sink.shopify_write import ShopifyPublisher

        publisher = ShopifyPublisher()
        out.append((OK, "location", publisher.location or "?"))
        if publisher.publications:
            out.append((OK, "publications", f"{len(publisher.publications)} channel(s)"))
        else:
            out.append((WARN, "publications",
                        "STORE_PUBLICATIONS unset — new products reach no sales channel"))
    except Exception as exc:
        out.append((WARN, "publishing", f"{type(exc).__name__}: {str(exc)[:110]}"))
    return out


# ----------------------------------------------------------------- liveness --
def check_freshness() -> list[Result]:
    out: list[Result] = []
    if not STATE_DB.exists():
        return [(FAIL, "state", f"no sync state at {STATE_DB.name}")]
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
        if not row:
            out.append((WARN, "delta", "no delta cursor yet (run a full sync)"))
        else:
            age = _age(datetime.fromisoformat(row[0]))
            minutes = int(age.total_seconds() // 60)
            if age <= CURSOR_STALE:
                out.append((OK, "delta", f"cursor {minutes} min old"))
            elif ops.running("sync", ""):
                # The catch-up job shares the full sync's lock on purpose: a full run
                # supersedes it, so the tick is skipped rather than raced. During a long
                # full sync the cursor is *expected* to age, and calling that a failure
                # every hour is how a check earns itself a permanent place in the ignored
                # pile.
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


def check_orders() -> list[Result]:
    """Watch the one pipeline whose silence costs money rather than face.

    A stale catalog is embarrassing. A storefront sale that never reaches the yard is a part
    still on the shelf for the counter to sell twice, and a work order nobody ever raised —
    so this is checked separately from the catalog freshness above.

    Freshness has to come from the log, not from the poll cursor. The cursor only advances
    when an order actually arrives, so on a quiet Tuesday a healthy poller and a dead one
    leave identical state. What happens every ten minutes either way is that the job runs and
    says how many orders it saw, so an orders.log that has stopped growing is the signal.
    """
    log = LOG_DIR / "orders.log"
    if not log.exists():
        return [(FAIL, "orders", "no orders.log — is the poller still scheduled?")]
    try:
        age = timedelta(seconds=time.time() - log.stat().st_mtime)
    except OSError as exc:
        return [(WARN, "orders", f"cannot stat orders.log: {exc}")]
    mins = int(age.total_seconds() // 60)
    if age > ORDERS_STALE:
        return [(FAIL, "orders",
                 f"poller silent {mins} min — storefront sales are not reaching the yard")]
    return [(OK, "orders", f"polled {mins} min ago")]


def check_logs(within=timedelta(hours=2)) -> list[Result]:
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
INSTALLATION = (check_environment, check_configuration)
NETWORK = (check_liveness, check_publishing)
PIPELINE = (check_freshness, check_orders, check_logs)


def collect(checks) -> list[Result]:
    """Run each check, and never let one failing check hide the rest of the report."""
    results: list[Result] = []
    for check in checks:
        try:
            results.extend(check())
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


def run(args) -> int:
    checks = list(INSTALLATION)
    if not args.no_network:
        checks += list(NETWORK)
    if not args.install_only:
        checks += list(PIPELINE)
    results = collect(checks)
    outcome = verdict(results)

    if args.json:
        print(json.dumps({"verdict": outcome,
                          "checks": [{"level": lvl, "name": name, "detail": detail}
                                     for lvl, name, detail in results]}, indent=2))
    elif not args.quiet or outcome != OK:
        print(f"CoreYard doctor: {outcome}\n")
        for level, name, detail in results:
            print(f"  [{level:<4}] {name:<14} {detail}")
        if outcome != OK:
            print("\nFix the FAIL lines first; a WARN is usually a deliberate choice.")
    return RANK[outcome]
