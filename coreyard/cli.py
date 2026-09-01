"""The one CoreYard command tree.

Every operator-facing command lives here, and nowhere else. Before this module the routing
was a ``case`` statement in a Bash heredoc inside ``install.sh``, which generated the
gitignored ``bin/coreyard``: the command list was invisible to ``--help``, invisible to code
review, and testable only by scraping the installer with a regular expression. ``coreyard
--help`` fell through to the sync parser and printed sync flags, so ``reconcile``,
``repair``, ``audit``, ``orders`` and ``validate`` could not be discovered from the tool at
all.

Each subcommand is mounted by calling the owning module's ``add_arguments(parser)``. That
function is the *same* one the module's own ``python -m coreyard.X`` entry point calls, so a
flag can never mean one thing here and another there — which matters because this
installation's crontab still names those module paths directly.
"""

from __future__ import annotations

import argparse
import signal
import sys
from contextlib import contextmanager

from coreyard import __version__, ops
from coreyard.config import REPO_ROOT

# Commands that write nothing, anywhere. They are left out of the run history so that
# "when did a sync last finish" is not buried under a hundred status checks.
READ_ONLY = frozenset({"status", "doctor", "audit", "validate", "images", "schema"})

# name -> (module path, help text). The module supplies its own flags via add_arguments().
# Order is the order `--help` prints them: the daily verbs first, then the occasional ones.
COMMANDS: list[tuple[str, str, str]] = [
    ("status", "coreyard.status",
     "what the pipeline believes right now (read-only)"),
    ("doctor", "coreyard.doctor",
     "check the installation, and whether the scheduled jobs are still running"),
    ("sync", "coreyard.run_sync",
     "publish the yard to the store (the main loop)"),
    ("ebay", "coreyard.ebay.cli",
     "pull and manage the eBay catalogue through the listing portal"),
    ("reconcile", "coreyard.reconcile.cli",
     "compare the yard with the live store and close the safe differences"),
    ("repair", "coreyard.repair.cli",
     "rewrite catalog output an older renderer produced"),
    ("audit", "coreyard.audit.cli",
     "read-only listing-quality report"),
    ("orders", "coreyard.webhook",
     "orders: serve / poll / register / retry / replay / status / sync-status"),
    ("images", "coreyard.yms.images",
     "list or fetch one part's photos from the share"),
    ("part-types", "coreyard.yms.part_types",
     "every part type the yard can inventory, and where its wording runs out"),
    ("schema", "coreyard.yms.discover_schema",
     "introspect the source database and rank likely tables"),
    ("validate", "coreyard.validate",
     "check the external config files against their schemas (offline)"),
    ("bulk", "coreyard.sink.shopify_bulk",
     "resumable concurrent bulk publish"),
    ("altfix", "coreyard.sink.backfill_alt",
     "backfill alt text onto photos published before it was generated"),
    ("oauth", "coreyard.sink.shopify_oauth",
     "exchange a client id/secret for an admin token"),
    ("schedule", "coreyard.schedule",
     "install, remove or show the recurring job"),
]

_EPILOG = """\
the usual order of business:

  coreyard doctor                 is this installation wired up correctly?
  coreyard status                 what does the pipeline think is true right now?
  coreyard sync --dry-run         what would a sync do?
  coreyard sync                   do it

scheduled work:

  coreyard sync delta             cheap catch-up, safe every few minutes
  coreyard sync                   the full extract, hourly
  coreyard sync --deep            + ask the live store, for the daily run

Commands that write say so in their own --help. `--dry-run` is accepted everywhere and
always means "decide, report, change nothing".
"""


def _duration(text: str) -> int:
    """Parse ``45m`` / ``2h`` / ``90s`` / ``3600`` into seconds.

    Accepts the same spellings the crontab's ``timeout`` calls already use, so moving a job
    into ``--timeout`` is a transcription rather than a conversion.
    """
    raw = str(text).strip().lower()
    if not raw:
        raise argparse.ArgumentTypeError("empty duration")
    if raw.isdigit():
        return int(raw)
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total, number = 0, ""
    for char in raw:
        if char.isdigit():
            number += char
        elif char in units and number:
            total += int(number) * units[char]
            number = ""
        else:
            raise argparse.ArgumentTypeError(f"bad duration {text!r} (try 45m, 2h, 90s)")
    if number:
        raise argparse.ArgumentTypeError(f"bad duration {text!r} (missing unit)")
    return total


@contextmanager
def _single_instance(name: str):
    """Refuse to start if another run holds this lock, exactly as cron's ``flock -n`` did.

    Moved into the tool because it was only ever expressed in the crontab, which lives in no
    repository: a job could be rescheduled, or copied to a second host, and quietly lose the
    protection that stops two syncs writing to Shopify and the state file at once.

    A busy lock is a *normal* outcome, not a failure — the five-minute delta run shares the
    hourly sync's lock precisely so that a full run supersedes it — so the caller exits 0.
    """
    import fcntl

    path = REPO_ROOT / "out" / f".{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(f"Another run holds {path.name}; skipping this one.")
            raise SystemExit(0)
        yield
    finally:
        handle.close()


@contextmanager
def _deadline(seconds: int):
    """Stop a wedged run before the next one is launched.

    Exits 124, the status GNU ``timeout`` uses, so a monitor that already understands the
    crontab's ``timeout`` reads this the same way.
    """
    def fire(signum, frame):
        print(f"Timed out after {seconds}s — stopping.", file=sys.stderr)
        raise SystemExit(124)

    previous = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def build_parser() -> argparse.ArgumentParser:
    """The whole command tree, built eagerly so ``--help`` can show all of it."""
    import importlib

    root = argparse.ArgumentParser(
        prog="coreyard",
        description="CoreYard — publish a salvage yard's inventory to Shopify, and keep it "
                    "in step.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.add_argument("--version", action="version", version=f"coreyard {__version__}")
    root.add_argument("--lock", metavar="NAME", default=None,
                      help="hold out/.NAME.lock for the run; exit quietly if another run "
                           "already has it")
    root.add_argument("--timeout", metavar="DUR", type=_duration, default=None,
                      help="stop the run after this long (e.g. 45m); exits 124")

    sub = root.add_subparsers(dest="command", metavar="<command>")
    for name, module_path, help_text in COMMANDS:
        module = importlib.import_module(module_path)
        parser = sub.add_parser(
            name,
            help=help_text,
            description=(module.__doc__ or help_text).splitlines()[0],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        module.add_arguments(parser)
    return root


def main(argv: list[str] | None = None) -> int:
    root = build_parser()
    args = root.parse_args(argv)
    if not getattr(args, "command", None):
        root.print_help()
        return 0
    if not hasattr(args, "func"):
        # A command with required subcommands and none given (argparse already errors on
        # `required=True`, so this is only reachable for a tree that grows a new branch).
        root.parse_args([args.command, "--help"])
        return 2

    with _single_instance(args.lock) if args.lock else _nullcontext():
        with _deadline(args.timeout) if args.timeout else _nullcontext():
            if args.command in READ_ONLY:
                return args.func(args)
            scope = getattr(args, "scope", None) or ""
            # Only when redirected. At a terminal the operator can see where their own run
            # started; in a log file nothing else marks the boundary.
            if not sys.stdout.isatty():
                print(ops.run_header(args.command, scope), flush=True)
            with ops.record(args.command, scope):
                return args.func(args)


@contextmanager
def _nullcontext():
    yield


if __name__ == "__main__":
    raise SystemExit(main())
