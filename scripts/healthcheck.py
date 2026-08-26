#!/usr/bin/env python3
"""Notice when the pipeline has stopped working, instead of finding out from a customer.

The checks themselves now live in ``coreyard/doctor.py``, where ``coreyard doctor`` runs
them too — there is no second opinion about what "healthy" means. This script remains
because it is what the crontab and any monitor already invoke, and because it adds the part
`doctor` deliberately does not do: deciding when to *tell* somebody, and not telling them
the same thing every fifteen minutes for three days.

    scripts/healthcheck.py            # check, notify on change
    scripts/healthcheck.py --quiet    # only speak when something is wrong
    scripts/healthcheck.py --notify   # force a notification even if unchanged
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from coreyard.doctor import (FAIL, OK, RANK, WARN, check_freshness,  # noqa: E402
                             check_liveness, check_logs, check_orders, collect, verdict)

ALERT_STATE = REPO / "out" / ".healthcheck_state.json"

# Only re-alert about an unchanged problem this often, so a multi-day outage does not
# produce a notification every 15 minutes and train everyone to ignore them.
REALERT = timedelta(hours=1)


def desktop_env() -> dict:
    """The environment a GUI notifier needs, discovered rather than assumed.

    cron inherits no desktop session, so whatever the crontab passes is a guess — and it was
    wrong here: the line exported ``DISPLAY=:0`` at a Wayland desktop, so every popup for
    months died with "could not connect to display :0" and only the journal was actually
    being written to. Working it out from the running session keeps the fix in the
    repository instead of in a crontab nothing can see.

    Returns {} when there is no session to talk to, which is the normal case on a server.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not os.path.isdir(runtime):
        return {}
    wayland = sorted(Path(runtime).glob("wayland-[0-9]*"))
    wayland = [w for w in wayland if not w.name.endswith(".lock")]
    if wayland:
        return {"XDG_RUNTIME_DIR": runtime, "WAYLAND_DISPLAY": wayland[0].name,
                "QT_QPA_PLATFORM": "wayland"}
    if os.environ.get("DISPLAY"):
        return {"XDG_RUNTIME_DIR": runtime, "DISPLAY": os.environ["DISPLAY"],
                "QT_QPA_PLATFORM": "xcb"}
    return {}


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
    session = desktop_env()
    if not session:
        return
    for argv in (["notify-send", "--app-name=CoreYard", subject, body],
                 ["kdialog", "--title", subject, "--passivepopup", body, "20"],
                 ["zenity", "--notification", f"--text={subject}: {body}"]):
        if not shutil.which(argv[0]):
            continue
        try:
            if subprocess.run(argv, env={**os.environ, **session},
                              capture_output=True, timeout=20).returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            continue


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

    checks = [check_freshness, check_orders]
    if not args.no_network:
        checks.append(check_liveness)
    checks.append(check_logs)
    results = collect(checks)
    outcome = verdict(results)

    lines = [f"[{lvl:<4}] {name:<10} {detail}" for lvl, name, detail in results]
    if not args.quiet or outcome != OK:
        print(f"CoreYard health: {outcome}")
        print("\n".join(lines))

    problems = [f"{name}: {detail}" for lvl, name, detail in results if lvl != OK]
    signature = "|".join(sorted(f"{lvl}:{name}" for lvl, name, _ in results if lvl != OK))
    prev = load_alert_state()
    last_sig = prev.get("signature", "")
    try:
        last_at = datetime.fromisoformat(prev.get("at", ""))
    except ValueError:
        last_at = datetime.fromtimestamp(0, timezone.utc)

    now = datetime.now(timezone.utc)
    changed = signature != last_sig
    stale = now - last_at > REALERT
    if problems and (args.notify or changed or stale):
        notify(f"CoreYard {outcome}", "; ".join(problems)[:500])
    elif not problems and changed and last_sig:
        notify("CoreYard recovered", "all checks passing")

    try:
        ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
        ALERT_STATE.write_text(json.dumps({"signature": signature, "at": now.isoformat()}))
    except OSError:
        pass
    return RANK[outcome]


if __name__ == "__main__":
    raise SystemExit(main())
