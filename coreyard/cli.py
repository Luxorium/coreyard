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
from coreyard.config import out_dir

# Commands that write nothing outside ``out/``. They are left out of the run history so
# that "when did a sync last finish" is not buried under a hundred status checks.
READ_ONLY = frozenset({"status", "doctor", "audit", "validate", "images", "schema"})


def _reports_only(args) -> bool:
    """Is *this* invocation one that changes nothing outside ``out/``?

    Asked per invocation rather than per command because one command is both. ``images``
    lists and downloads photographs, which is why it is on the list above — but
    ``images --delete --apply`` removes them from the share, and being on the list meant
    the single most destructive image operation was the one nothing recorded. A run
    history that omits exactly the deletions is worse than one that omits nothing.
    """
    if args.command not in READ_ONLY:
        return False
    return not (args.command == "images" and getattr(args, "delete", None))


# What each command does and what it needs, keyed by its path in the tree. REL-01: no
# public command may have an undocumented support status, so `tests/test_inventory.py`
# fails if a node here has no entry or an entry here names no node — adding a command
# without saying what it writes is not possible without the test going red.
#
# Effects, in increasing order of consequence. `local` covers `out/`, the state database,
# `.env` and the scheduler; the rest name whose system changes.
READS = "read-only"
LOCAL = "local files"
STORE = "Shopify"
SOURCE = "source database"

# path -> (effect, capabilities it cannot run without, the flag that unlocks the write)
#
# "cannot run without" is strict: it is what makes the command refuse *before* any side
# effect, so a capability that only some invocations need does not belong here. `orders
# poll` needs the Admin API and nothing else — gating it on the webhook secret would refuse
# to poll for a site that deliberately never registered a webhook. `orders retry` and
# `orders replay` work the local queue and need nothing; their `--write-orders` flag is
# gated by the booking path itself, which has its own mapping check.
SUPPORT: dict[str, tuple[str, tuple[str, ...], str]] = {
    "init": (LOCAL, (), ""),
    "status": (READS, (), ""),
    "counts": (STORE, ("shopify",), "--apply"),
    "doctor": (READS, (), ""),
    "sync": (STORE, ("source",), ""),
    "sync delta": (STORE, ("source", "delta"), ""),
    "sync inventory": (STORE, ("source",), ""),
    "sync photos": (STORE, ("source", "photos"), ""),
    "sync catalog": (STORE, ("source",), ""),
    "reconcile": (STORE, ("source", "shopify"), "--apply"),
    "links": (SOURCE, ("source", "shopify"), "--apply"),
    "repair": (READS, ("shopify",), ""),
    "repair titles": (STORE, ("source", "shopify"), "--apply"),
    "repair tags": (STORE, ("source", "shopify"), "--apply"),
    "repair seo": (STORE, ("source", "shopify"), "--apply"),
    "repair descriptions": (STORE, ("source", "shopify"), "--apply"),
    "repair weights": (STORE, ("source", "shopify"), "--apply"),
    "repair metafields": (STORE, ("source", "shopify"), "--apply"),
    "repair all": (STORE, ("source", "shopify"), "--apply"),
    "repair fingerprints": (LOCAL, ("source",), "--apply"),
    "audit": (READS, ("shopify",), ""),
    "audit catalog": (READS, ("shopify",), ""),
    "orders": (READS, (), ""),
    "orders serve": (SOURCE, ("orders",), "--write-orders"),
    "orders register": (STORE, ("orders", "shopify"), ""),
    "orders list": (READS, ("shopify",), ""),
    "orders unregister": (STORE, ("shopify",), ""),
    "orders replay": (SOURCE, (), "--write-order"),
    "orders retry": (SOURCE, (), "--write-orders"),
    "orders status": (READS, (), ""),
    "orders poll": (SOURCE, ("shopify",), "--write-orders"),
    "orders sync-status": (STORE, ("shopify",), "--apply"),
    "orders invoice": (SOURCE, ("shopify",), "--apply"),
    "alert": (LOCAL, (), ""),
    "images": (LOCAL, ("database", "photos"), "--delete --apply"),
    "part-types": (READS, ("source",), ""),
    "schema": (READS, ("database",), ""),
    "validate": (READS, (), ""),
    "bulk": (STORE, ("source", "shopify"), ""),
    "altfix": (STORE, ("shopify",), ""),
    "oauth": (LOCAL, (), ""),
    "schedule": (READS, (), ""),
    "schedule install": (LOCAL, (), ""),
    "schedule uninstall": (LOCAL, (), ""),
    "schedule status": (READS, (), ""),
}

# name -> (module path, help text). The module supplies its own flags via add_arguments().
# Order is the order `--help` prints them: the daily verbs first, then the occasional ones.
COMMANDS: list[tuple[str, str, str]] = [
    ("init", "coreyard.setup_wizard",
     "write a working .env and store.json (start here)"),
    ("status", "coreyard.status",
     "what the pipeline believes right now (read-only)"),
    ("counts", "coreyard.storefront_counts",
     "refresh the exact source-inventory counter shown on the storefront"),
    ("doctor", "coreyard.doctor",
     "check the installation, and whether the scheduled jobs are still running"),
    ("alert", "coreyard.alerts",
     "notify the operator when the pipeline has stopped, and when it recovers"),
    ("sync", "coreyard.run_sync",
     "publish the yard to the store (the main loop)"),
    ("reconcile", "coreyard.reconcile.cli",
     "compare the yard with the live store and close the safe differences"),
    ("links", "coreyard.backlinks",
     "write each published part's storefront address back into the yard record"),
    ("repair", "coreyard.repair.cli",
     "rewrite catalog output an older renderer produced"),
    ("audit", "coreyard.audit.cli",
     "read-only listing-quality report"),
    ("orders", "coreyard.webhook",
     "orders: serve / poll / register / retry / replay / status / sync-status / "
     "invoice"),
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


class LockBusy(SystemExit):
    """Another run holds this lock, so this tick does not run.

    Carries the lock's name so the caller can record which job it yielded to. A
    ``SystemExit`` subclass because that is what it has always been — exiting 0 is the
    intended outcome — and anything that catches ``SystemExit`` keeps working unchanged.
    """

    def __init__(self, holder: str):
        super().__init__(0)
        self.holder = holder


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

    path = out_dir() / f".{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(f"Another run holds {path.name}; skipping this one.")
            raise LockBusy(path.name)
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


def tree(parser: argparse.ArgumentParser, prefix: str = "") -> list[tuple]:
    """Every node below ``parser``, depth first, as ``(path, parser)``.

    One implementation, because two would drift: the preflight below decides what a command
    needs from these paths, and ``scripts/inventory.py`` documents the same paths. A
    generated document that walked the tree differently from the code that enforces it
    would be a document describing a different tool.
    """
    action = next((a for a in parser._actions
                   if isinstance(a, argparse._SubParsersAction)), None)
    if action is None:
        return []
    found: list[tuple] = []
    for name, child in action.choices.items():
        path = f"{prefix} {name}".strip()
        found.append((path, child))
        found.extend(tree(child, path))
    return found


def unmet(path: str, caps) -> list[tuple[str, str]]:
    """Capabilities this command cannot run without that this installation does not have.

    REL-01: an unsupported combination must fail before side effects with a useful
    explanation. A tabular source genuinely cannot serve `coreyard schema` or a database
    delta, and the useful moment to say so is before the run starts — not as a traceback
    from somewhere inside the extract, after a lock has been taken and a log line written
    that looks like a job that ran.
    """
    _, needs, _ = SUPPORT.get(path, (READS, (), ""))
    return [(name, caps.get(name).detail) for name in needs if not caps.enabled(name)]


def _refuse(path: str, missing: list[tuple[str, str]], caps) -> int:
    """Say what cannot run here, why, and that nothing was changed."""
    print(f"`coreyard {path}` cannot run on this installation.\n", file=sys.stderr)
    for name, detail in missing:
        print(f"  {name:<14} {detail}", file=sys.stderr)
    print(f"\nThis installation's source is {caps.source_spec}. "
          f"`coreyard doctor` lists every capability, and docs/CAPABILITY_MATRIX.md says "
          f"which commands each one supports.\nNothing was changed.", file=sys.stderr)
    return 2


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
    # argparse applies a subparser's defaults after the parent's, so the deepest node the
    # invocation reached is the one left in the namespace. That is exactly the path
    # `SUPPORT` is keyed by, and reading it back beats re-deriving it from argv.
    for path, node in tree(root):
        node.set_defaults(_support_path=path)
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

    # Before the lock and before the deadline: a command that cannot run here should not
    # take a lock the next scheduled run then waits on.
    from coreyard import capabilities

    caps = capabilities.detect()
    path = getattr(args, "_support_path", None) or args.command
    missing = unmet(path, caps)

    try:
        return _run(args, path, missing, caps)
    except LockBusy as busy:
        # A skipped tick is a normal outcome and still an event. Recorded so that "scheduled
        # and never gets in" stops reading like "no longer scheduled": the two are the same
        # stale timestamp otherwise, and telling them apart took four days the last time.
        ops.skipped(args.command, getattr(args, "scope", None) or "", busy.holder)
        return 0


def _run(args, path: str, missing, caps) -> int:
    """The command itself, inside the lock and the deadline it asked for."""
    with _single_instance(args.lock) if args.lock else _nullcontext():
        with _deadline(args.timeout) if args.timeout else _nullcontext():
            if _reports_only(args):
                return _refuse(path, missing, caps) if missing else args.func(args)
            scope = getattr(args, "scope", None) or ""
            # Only when redirected. At a terminal the operator can see where their own run
            # started; in a log file nothing else marks the boundary.
            if not sys.stdout.isatty():
                print(ops.run_header(args.command, scope), flush=True)
            # The code is handed to the recorder rather than only returned, because a
            # command reports failure by *returning* nonzero: from inside the ``with``
            # block a run that exited 2 and one that exited 0 are indistinguishable, and
            # believing the second wrote "ok" into the history for runs the shell had
            # already called failures.
            #
            # A refusal is recorded too. A scheduled job that starts refusing every tick
            # must not read as a job that stopped being scheduled — the run history is
            # where "this has been failing since Tuesday" is legible.
            with ops.record(args.command, scope) as run:
                if missing:
                    ops.count(refused=", ".join(name for name, _ in missing))
                    run.code = _refuse(path, missing, caps)
                else:
                    run.code = args.func(args)
                return run.code


@contextmanager
def _nullcontext():
    yield


if __name__ == "__main__":
    raise SystemExit(main())
