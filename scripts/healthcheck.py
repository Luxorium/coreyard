#!/usr/bin/env python3
"""Notice when the pipeline has stopped working, instead of finding out from a customer.

Every scheduled job here writes to a log file and exits. Nothing reads those logs, there is
no MTA for cron to mail, and a failure looks exactly like success: the storefront simply
stops changing. That is not hypothetical — when the source server's IP moved, every sync
failed for as long as it took a human to happen to look.

So this checks the things whose staleness *is* the symptom, and says so loudly:

    freshness  the delta cursor should advance every few minutes, and the full sync should
               have completed within the last day. Either going stale means the pipeline is
               down, whatever the cause — credentials, network, schema drift, cron itself.
    orders     the storefront poller should speak every ten minutes, order or no order.
    liveness   the source database and Shopify both answer.
    errors     a recent traceback — or a job that died before it could produce one.

Exit status is 0 healthy, 1 degraded (warnings), 2 failed — so cron, a monitor, or a human
can all use it.

    scripts/healthcheck.py            # check, notify on change
    scripts/healthcheck.py --quiet    # only speak when something is wrong
    scripts/healthcheck.py --notify   # force a notification even if unchanged
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

STATE_DB = REPO / "coreyard_sync_state.sqlite3"
ALERT_STATE = REPO / "out" / ".healthcheck_state.json"
LOG_DIR = REPO / "out"

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
# Only re-alert about an unchanged problem this often, so a multi-day outage does not
# produce a notification every 15 minutes and train everyone to ignore them.
REALERT = timedelta(hours=1)

# A job that dies before its first line of output leaves no traceback: the interpreter or
# the shell says its piece on stderr and exits 1. That is exactly how the order poller
# failed silently for a day — the storefront scripts moved into the CoreYard CLI, cron kept
# invoking the paths they used to live at, and every tick logged one "can't open file ...
# No such file or directory" that matched none of the patterns below. "The job never
# started" is the failure least likely to be noticed by hand, so it gets its own patterns.
FATAL = ("Traceback", "No such file or directory", "command not found",
         "ModuleNotFoundError", "ImportError", "cannot import name")


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


def check_freshness() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
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
            level = FAIL if age > CURSOR_STALE else OK
            out.append((level, "delta", f"cursor {int(age.total_seconds()//60)} min old"))

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


def check_orders() -> list[tuple[str, str, str]]:
    """Watch the one pipeline whose silence costs money rather than face.

    A stale catalog is embarrassing. A storefront sale that never reaches the yard is a part
    still on the shelf for the counter to sell twice, and a customer waiting on a pull ticket
    nobody printed — so this is checked separately from the catalog freshness above.

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


def check_liveness() -> list[tuple[str, str, str]]:
    out = []
    try:
        from coreyard.yms.db import ping

        ping()
        out.append((OK, "source-db", "reachable"))
    except Exception as exc:
        out.append((FAIL, "source-db", f"{type(exc).__name__}: {str(exc)[:110]}"))
    try:
        from coreyard.sink.shopify_api import ShopifySink

        out.append((OK, "shopify", f"connected to {ShopifySink().check()}"))
    except Exception as exc:
        out.append((FAIL, "shopify", f"{type(exc).__name__}: {str(exc)[:110]}"))
    return out


def check_logs(within=timedelta(hours=2)) -> list[tuple[str, str, str]]:
    out = []
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
            # Keep the line window roughly consistent with the file-age window above. The
            # catch-up job writes ~50 lines an hour, so a deeper tail would keep re-reporting
            # a single failure long after it stopped happening — and an alert that stays lit
            # for a fixed problem is how people learn to ignore alerts.
            tail = log.read_text(errors="replace").splitlines()[-150:]
        except OSError:
            continue
        bad = [ln for ln in tail
               if any(sig in ln for sig in FATAL) or ln.lstrip().startswith("!!")
               or "FAILED" in ln or "REFUSING" in ln]
        if bad:
            out.append((WARN, log.stem, bad[-1].strip()[:110]))
    return out


def notify(subject: str, body: str) -> None:
    """Say it everywhere this machine can actually be heard.

    There is no MTA, so cron's MAILTO goes nowhere. The journal always works and is what a
    later investigation will read; the desktop popup is what gets noticed today.
    """
    subprocess.run(["logger", "-t", "coreyard-health", f"{subject} :: {body}"],
                   check=False)
    cmd = os.environ.get("COREYARD_ALERT_CMD")
    if cmd:
        subprocess.run(cmd, shell=True, check=False,
                       env={**os.environ, "COREYARD_ALERT_SUBJECT": subject,
                            "COREYARD_ALERT_BODY": body})
    if shutil.which("kdialog") and os.environ.get("DISPLAY"):
        subprocess.run(["kdialog", "--title", subject, "--passivepopup", body, "20"],
                       check=False)


def load_alert_state() -> dict:
    try:
        return json.loads(ALERT_STATE.read_text())
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="print only when not healthy")
    ap.add_argument("--notify", action="store_true", help="notify even if nothing changed")
    ap.add_argument("--no-network", action="store_true", help="skip DB/Shopify checks")
    args = ap.parse_args()

    results = check_freshness()
    results += check_orders()
    if not args.no_network:
        results += check_liveness()
    results += check_logs()

    worst = max((RANK[level] for level, _, _ in results), default=0)
    verdict = {0: OK, 1: WARN, 2: FAIL}[worst]

    lines = [f"[{lvl:<4}] {name:<10} {detail}" for lvl, name, detail in results]
    if not args.quiet or worst:
        print(f"CoreYard health: {verdict}")
        print("\n".join(lines))

    problems = [f"{name}: {detail}" for lvl, name, detail in results if lvl != OK]
    signature = "|".join(sorted(f"{lvl}:{name}" for lvl, name, _ in results if lvl != OK))
    prev = load_alert_state()
    last_sig = prev.get("signature", "")
    last_at = prev.get("at")
    stale_alert = True
    if last_at:
        try:
            stale_alert = _age(datetime.fromisoformat(last_at)) > REALERT
        except ValueError:
            pass

    should = args.notify or (
        # A new problem, the same problem again after the re-alert window, or a recovery.
        (problems and (signature != last_sig or stale_alert))
        or (not problems and last_sig)
    )
    if should:
        if problems:
            notify(f"CoreYard {verdict}", "; ".join(problems[:4]))
        else:
            notify("CoreYard recovered", "all checks passing again")

    ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
    ALERT_STATE.write_text(json.dumps(
        {"signature": signature, "at": datetime.now(timezone.utc).isoformat(),
         "verdict": verdict}))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
