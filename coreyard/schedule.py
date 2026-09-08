"""Install CoreYard as a recurring background job, using whatever the distro provides.

Preference order, chosen so the common case needs no root:

1. **systemd user timer** — no root, survives reboot once lingering is enabled.
2. **systemd system timer** — when running as root, or ``--system`` is asked for.
3. **cron** — Alpine/OpenRC, containers, WSL, or anything without systemd.

    coreyard schedule install --every 30m
    coreyard schedule install --every 5m --task "sync delta"
    coreyard schedule status
    coreyard schedule uninstall

Runs are serialised with a lock file, because a sync that takes longer than the interval
must never have a second copy start on top of it. The CLI takes ``--lock`` itself, so the
serialisation holds even where ``flock`` is not installed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from coreyard.config import DATA_ROOT, REPO_ROOT, out_dir

NAME = "coreyard-sync"
LOCK = out_dir() / ".sync.lock"
LOG = out_dir() / "sync.log"
# What a new installation schedules. `sync` publishes: the operator-facing command assumes
# Shopify, so a scheduled job cannot end up quietly writing a CSV that nobody reads.
DEFAULT_TASK = "sync"
DEFAULT_EVERY = "30m"


# --------------------------------------------------------------------- helpers ---
def parse_every(text: str) -> int:
    """"30m" / "2h" / "90s" / "1d" -> seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", text.lower())
    if not m:
        raise ValueError(f"cannot read interval {text!r} — use forms like 15m, 2h, 1d")
    value, unit = int(m.group(1)), m.group(2) or "m"
    seconds = value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds < 60:
        raise ValueError("interval must be at least 60s")
    return seconds


def launcher() -> Path:
    """The CLI entry point, preferring the generated launcher."""
    candidate = REPO_ROOT / "bin" / "coreyard"
    if candidate.exists():
        return candidate
    return REPO_ROOT / ".venv" / "bin" / "python"


def command_for(task: str) -> str:
    """The full command line for one scheduled run.

    ``--lock`` is passed to the CLI rather than relying on ``flock`` alone: the tool now
    serialises itself, on the same lock file, so an installation without flock(1) is no
    longer quietly unprotected against a slow run being lapped by the next tick.
    """
    exe = launcher()
    first_word = (task.strip().split() or [LOCK.stem.lstrip('.')])[0]
    lock_name = re.sub(r"[^A-Za-z0-9_-]", "-", first_word) or LOCK.stem.lstrip('.')
    lock = f"--lock {lock_name}"
    if exe.name == "coreyard":
        base = f"{exe} {lock} {task}"
    else:                                   # no launcher yet: call the module directly
        base = f"{exe} -m coreyard {lock} {task}"
    # Do not wrap this in flock(1) as well. A second open of the same lock file conflicts
    # with the descriptor held by the outer flock process, so the CLI sees its own wrapper
    # and skips every scheduled run. The in-process lock is portable and is the one surface
    # used by both cron and systemd.
    return base


def _systemd_user_available() -> bool:
    if not shutil.which("systemctl") or os.geteuid() == 0:
        return False
    try:
        proc = subprocess.run(["systemctl", "--user", "is-system-running"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    # "degraded" still runs timers; only a hard failure means no user manager.
    return proc.returncode == 0 or "running" in proc.stdout or "degraded" in proc.stdout


def detect_backend(prefer: str = "auto") -> str:
    if prefer != "auto":
        return prefer
    if _systemd_user_available():
        return "systemd-user"
    if shutil.which("systemctl") and os.geteuid() == 0:
        return "systemd-system"
    if shutil.which("crontab"):
        return "cron"
    return "none"


# --------------------------------------------------------------------- systemd ---
def _unit_dir(backend: str) -> Path:
    if backend == "systemd-user":
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / "systemd" / "user"
    return Path("/etc/systemd/system")


def _systemctl(backend: str) -> list[str]:
    return ["systemctl", "--user"] if backend == "systemd-user" else ["systemctl"]


def _units(task: str, seconds: int) -> tuple[str, str]:
    service = f"""[Unit]
Description=CoreYard inventory sync
Documentation=https://github.com/Luxorium/coreyard
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory={DATA_ROOT}
ExecStart={command_for(task)}
# A stalled SMB call must not wedge the timer forever.
TimeoutStartSec={max(seconds * 4, 3600)}
StandardOutput=append:{LOG}
StandardError=append:{LOG}
"""
    timer = f"""[Unit]
Description=Run CoreYard sync every {seconds // 60} minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec={seconds}s
# Catch up after the machine was asleep or powered off.
Persistent=true
AccuracySec=1min
Unit={NAME}.service

[Install]
WantedBy=timers.target
"""
    return service, timer


def install_systemd(backend: str, task: str, seconds: int, dry_run: bool) -> int:
    directory = _unit_dir(backend)
    service, timer = _units(task, seconds)
    targets = {directory / f"{NAME}.service": service, directory / f"{NAME}.timer": timer}
    if dry_run:
        for path, body in targets.items():
            print(f"--- would write {path} ---\n{body}")
        return 0
    directory.mkdir(parents=True, exist_ok=True)
    for path, body in targets.items():
        path.write_text(body, encoding="utf-8")
        print(f"  wrote {path}")
    ctl = _systemctl(backend)
    subprocess.run(ctl + ["daemon-reload"], check=True)
    subprocess.run(ctl + ["enable", "--now", f"{NAME}.timer"], check=True)
    print(f"  enabled {NAME}.timer")
    if backend == "systemd-user":
        print("\nSo it keeps running when you are not logged in:")
        print(f"  sudo loginctl enable-linger {os.environ.get('USER', 'your-user')}")
    return 0


# ------------------------------------------------------------------------ cron ---
def _crontab_lines() -> list[str]:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def _write_crontab(lines: list[str]) -> None:
    body = "\n".join(lines).strip() + "\n"
    subprocess.run(["crontab", "-"], input=body, text=True, check=True)


def _cron_schedule(seconds: int) -> str:
    minutes = seconds // 60
    if minutes < 60:
        return f"*/{minutes} * * * *"
    hours = minutes // 60
    if hours < 24:
        return f"0 */{hours} * * *"
    return "0 3 * * *"


def _task_name(task: str) -> str:
    first_word = (task.strip().split() or [DEFAULT_TASK])[0]
    return re.sub(r"[^A-Za-z0-9_-]", "-", first_word) or DEFAULT_TASK


def _managed_cron_line(line: str, task: str | None = None) -> bool:
    """Whether ``line`` belongs to CoreYard, optionally to one particular task.

    Cron can run the cheap counter beside a catalog sync. A generic ``coreyard-sync``
    substring used to make installing either one erase the other. New entries carry a
    task-specific marker; the command signature also recognizes and replaces the generic
    entry emitted by the previous generator.
    """
    legacy_double_lock = (
        f"flock -n {LOCK}" in line
        and f"--lock {LOCK.stem.lstrip('.')}" in line
    )
    if task is None:
        return NAME in line or legacy_double_lock
    task_name = _task_name(task)
    marker = f"{NAME}:{task_name}"
    command = f"--lock {task_name} {task.strip()}"
    return marker in line or (NAME in line and command in line) or (
        legacy_double_lock and task.strip() in line
    )


def install_cron(task: str, seconds: int, dry_run: bool) -> int:
    task_name = _task_name(task)
    marker_name = f"{NAME}:{task_name}"
    marker = f"# {marker_name} (managed by coreyard.schedule)"
    entry = (f"{_cron_schedule(seconds)} cd {DATA_ROOT} && {command_for(task)} "
             f">> {LOG} 2>&1 # {marker_name}")
    if dry_run:
        print(f"--- would add to crontab ---\n{marker}\n{entry}")
        return 0
    lines = [ln for ln in _crontab_lines() if not _managed_cron_line(ln, task)]
    lines += [marker, entry]
    _write_crontab(lines)
    print(f"  added cron entry: {entry}")
    return 0


def _cron_field(field: str, low: int, high: int) -> list[int] | None:
    """The values a cron field fires on, or None if it is a shape we do not read."""
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                return None
            step = int(raw_step)
        if part == "*":
            start, stop = low, high
        elif "-" in part:
            first, _, last = part.partition("-")
            if not (first.isdigit() and last.isdigit()):
                return None
            start, stop = int(first), int(last)
        elif part.isdigit():
            start = stop = int(part)
        else:
            return None
        if start < low or stop > high or stop < start:
            return None
        values.update(range(start, stop + 1, step))
    return sorted(values) or None


def cron_period(spec: str) -> int | None:
    """The **longest** gap between two consecutive runs of this cron expression.

    The longest, not the average: "stale" has to be measured against the worst wait the
    schedule actually permits, or a job firing at ``5,15,25,35,45,55`` would be called late
    every hour on the one ten-minute boundary it never crosses.

    Only minute and hour are read. A schedule narrowed by day-of-month or day-of-week is
    reported as at least a day, because a weekday-only job is legitimately silent all
    weekend and guessing more precisely than that is how a check earns a permanent place in
    the ignored pile.
    """
    fields = spec.split()
    if len(fields) < 5:
        return None
    minutes = _cron_field(fields[0], 0, 59)
    hours = _cron_field(fields[1], 0, 23)
    if not minutes or not hours:
        return None
    restricted = fields[2] != "*" or fields[4] != "*"
    firings = sorted(hour * 3600 + minute * 60 for hour in hours for minute in minutes)
    if len(firings) == 1:
        return 7 * 86400 if restricted else 86400
    gaps = [second - first for first, second in zip(firings, firings[1:])]
    gaps.append(firings[0] + 86400 - firings[-1])          # the wrap past midnight
    longest = max(gaps)
    return max(longest, 86400) if restricted else longest


def _cron_task(line: str) -> str | None:
    """Which CoreYard job a crontab line runs, or None if it runs none.

    Recognised by the launcher rather than by a marker this module wrote, because a real
    host's crontab is hand-written: this installation's hourly sync, five-minute delta and
    ten-minute order poll carry a full safety envelope of `timeout`, `prlimit`, `nice` and
    `ionice` and none of the generated marker. A scheduler check that only saw its own
    entries would report "nothing scheduled" on the busiest host it will ever run on.
    """
    if not re.search(r"(bin/coreyard|-m\s+coreyard)\b", line):
        return None
    lock = re.search(r"--lock\s+([A-Za-z0-9_-]+)", line)
    if lock:
        return lock.group(1)
    after = re.split(r"bin/coreyard|-m\s+coreyard[\w.]*", line, maxsplit=1)
    tokens = [t for t in (after[1] if len(after) > 1 else "").split() if not t.startswith("-")]
    return tokens[0] if tokens else None


def installed_intervals() -> dict[str, int]:
    """``{task: seconds}`` for the CoreYard jobs this host actually runs, or ``{}``.

    Read rather than assumed, because "stale" has no meaning without it: a job is late
    relative to *its own schedule*, and a five-minute catch-up and a nightly pass are late
    at very different times. An installation with nothing scheduled has nothing to be late
    for, which is why an empty mapping is a real answer rather than a failure — alerting on
    a job the operator deliberately paused is the fastest way to teach them to ignore
    alerts.

    Where one task is scheduled more than once — an hourly sync plus a morning deep pass —
    the shortest interval wins, because that is the one whose silence is evidence.
    """
    found: dict[str, int] = {}

    def note(task: str | None, seconds: int | None) -> None:
        if task and seconds:
            found[task] = min(seconds, found.get(task, seconds))

    if shutil.which("crontab"):
        for line in _crontab_lines():
            if line.lstrip().startswith("#"):
                continue
            note(_cron_task(line), cron_period(line))
    if shutil.which("systemctl"):
        for backend in ("systemd-user", "systemd-system"):
            timer = _unit_dir(backend) / f"{NAME}.timer"
            if not timer.exists():
                continue
            match = re.search(r"^OnUnitActiveSec=(\d+)", timer.read_text(errors="replace"),
                              re.M)
            if match:
                note(DEFAULT_TASK, int(match.group(1)))
    return found


# --------------------------------------------------------------------- actions ---
def do_install(args) -> int:
    seconds = parse_every(args.every)
    backend = detect_backend(args.backend)
    print(f"Scheduling '{args.task}' every {args.every} using: {backend}")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if backend in ("systemd-user", "systemd-system"):
        return install_systemd(backend, args.task, seconds, args.dry_run)
    if backend == "cron":
        return install_cron(args.task, seconds, args.dry_run)
    print("No supported scheduler found (systemd or cron). Run CoreYard manually, or add:\n"
          f"  {_cron_schedule(seconds)} cd {DATA_ROOT} && {command_for(args.task)}",
          file=sys.stderr)
    return 1


def do_uninstall(args) -> int:
    removed = False
    if shutil.which("systemctl"):
        for backend in ("systemd-user", "systemd-system"):
            directory = _unit_dir(backend)
            if not (directory / f"{NAME}.timer").exists():
                continue
            ctl = _systemctl(backend)
            subprocess.run(ctl + ["disable", "--now", f"{NAME}.timer"], capture_output=True)
            for suffix in (".timer", ".service"):
                (directory / f"{NAME}{suffix}").unlink(missing_ok=True)
            subprocess.run(ctl + ["daemon-reload"], capture_output=True)
            print(f"  removed {backend} units")
            removed = True
    if shutil.which("crontab"):
        lines = _crontab_lines()
        kept = [ln for ln in lines if not _managed_cron_line(ln)]
        if len(kept) != len(lines):
            _write_crontab(kept)
            print("  removed cron entry")
            removed = True
    print("Nothing scheduled." if not removed else "Uninstalled.")
    return 0


def do_status(args) -> int:
    print(f"Detected backend: {detect_backend('auto')}")
    found = False
    if shutil.which("systemctl"):
        for backend in ("systemd-user", "systemd-system"):
            if (_unit_dir(backend) / f"{NAME}.timer").exists():
                found = True
                print(f"\n{backend}:")
                subprocess.run(_systemctl(backend) + ["list-timers", f"{NAME}.timer", "--no-pager"])
    if shutil.which("crontab"):
        entries = [ln for ln in _crontab_lines() if NAME in ln and not ln.startswith("#")]
        if entries:
            found = True
            print("\ncron:")
            for entry in entries:
                print("  " + entry)
    if not found:
        print("\nNothing scheduled yet — `python -m coreyard.schedule install`")
    if LOG.exists():
        print(f"\nLast lines of {LOG}:")
        for line in LOG.read_text(errors="replace").splitlines()[-5:]:
            print("  " + line)
    return 0


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with the scheduling actions."""
    sub = ap.add_subparsers(dest="action", required=True)

    install = sub.add_parser("install", help="install the recurring job")
    install.add_argument("--every", default=DEFAULT_EVERY, help="e.g. 15m, 2h, 1d (default 30m)")
    install.add_argument("--task", default=DEFAULT_TASK, help=f"CLI args to run (default {DEFAULT_TASK!r})")
    install.add_argument("--backend", default="auto",
                         choices=["auto", "systemd-user", "systemd-system", "cron"])
    install.add_argument("--dry-run", action="store_true", help="print the units, write nothing")
    install.set_defaults(func=do_install)

    sub.add_parser("uninstall", help="remove it").set_defaults(func=do_uninstall)
    sub.add_parser("status", help="show what is scheduled").set_defaults(func=do_status)
    return ap


def dispatch(args) -> int:
    try:
        return args.func(args)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard.schedule",
                                 description=__doc__.splitlines()[0])
    add_arguments(ap)
    return dispatch(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
