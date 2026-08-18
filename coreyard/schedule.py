"""Install CoreYard as a recurring background job, using whatever the distro provides.

Preference order, chosen so the common case needs no root:

1. **systemd user timer** — no root, survives reboot once lingering is enabled.
2. **systemd system timer** — when running as root, or ``--system`` is asked for.
3. **cron** — Alpine/OpenRC, containers, WSL, or anything without systemd.

    python -m coreyard.schedule install --every 30m
    python -m coreyard.schedule status
    python -m coreyard.schedule uninstall

Runs are serialised with a lock file, because a sync that takes longer than the interval
must never have a second copy start on top of it.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from coreyard.config import REPO_ROOT

NAME = "coreyard-sync"
LOCK = REPO_ROOT / "out" / ".sync.lock"
LOG = REPO_ROOT / "out" / "sync.log"
DEFAULT_TASK = "--sink api"
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
    exe = launcher()
    if exe.name == "coreyard":
        base = f"{exe} {task}"
    else:                                   # no launcher yet: call the module directly
        base = f"{exe} -m coreyard.run_sync {task}"
    # flock keeps a slow run from being lapped by the next tick.
    if shutil.which("flock"):
        return f"flock -n {LOCK} {base}"
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
WorkingDirectory={REPO_ROOT}
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


def install_cron(task: str, seconds: int, dry_run: bool) -> int:
    marker = f"# {NAME} (managed by coreyard.schedule)"
    entry = f"{_cron_schedule(seconds)} cd {REPO_ROOT} && {command_for(task)} >> {LOG} 2>&1"
    if dry_run:
        print(f"--- would add to crontab ---\n{marker}\n{entry}")
        return 0
    lines = [ln for ln in _crontab_lines() if NAME not in ln]
    lines += [marker, entry]
    _write_crontab(lines)
    print(f"  added cron entry: {entry}")
    return 0


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
          f"  {_cron_schedule(seconds)} cd {REPO_ROOT} && {command_for(args.task)}",
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
        kept = [ln for ln in lines if NAME not in ln]
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard.schedule", description=__doc__.splitlines()[0])
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

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
