"""``coreyard state`` — what the snapshot says about itself, and adopting it deliberately.

The sync state is a set of claims of the form "the storefront holds this version of R#51".
Those are only meaningful about one store, under one handle prefix, so the snapshot records
which. Re-pointing an installation at a different store, or renaming the handle prefix,
makes every stored claim false — in one of two ways, both quiet:

* **A different store.** The fingerprints still say every part is published, so the next
  sync finds nothing to do. The new store stays empty and the run reports success.
* **A different handle prefix.** The handle is part of the rendered product, so every
  fingerprint moves at once and the next sync republishes the entire catalogue under new
  handles — beside the old products, which stay live, on sale, and now unmanaged.

So a command that writes to the store refuses when the snapshot was written for a different
one, and this is how the operator says "yes, I meant that".
"""

from __future__ import annotations

import argparse
import sys

from coreyard.config import cli_name, load_store
from coreyard.state import DEFAULT_STATE_DB, SyncState


def _identity() -> tuple[str, str]:
    """This installation's (store, handle prefix), from configuration alone."""
    from coreyard.sink.shopify_api import load_creds

    return load_creds().store, load_store().handle_prefix


def cmd_adopt(args) -> int:
    if not DEFAULT_STATE_DB.exists():
        print("No sync state yet, so there is nothing to adopt. The first sync writes one "
              "and it will belong to this store from the start.")
        return 0
    try:
        store, prefix = _identity()
    except Exception as exc:
        print(f"Cannot tell which store this installation publishes to: {exc}",
              file=sys.stderr)
        return 2

    with SyncState(DEFAULT_STATE_DB) as state:
        held = state.identity()
        changed = state.mismatch(store, prefix)
        parts = len(state.load())
        if not changed:
            if held:
                print(f"This snapshot already belongs to {held.get('store', '?')} under "
                      f"handle prefix {held.get('handle_prefix', '?')!r} "
                      f"({parts:,} part(s)).")
            else:
                print(f"This snapshot carries no store identity yet ({parts:,} part(s)). "
                      f"Adopting records it as {store} / {prefix!r}.")
                if not args.apply:
                    print(f"\nPlan only — re-run with --apply.")
                    return 0
                state.claim(store, prefix)
                print(f"Adopted: {store} / {prefix!r}.")
            return 0

        print(f"This snapshot describes a different installation: {changed}.")
        print(f"  {parts:,} part(s) recorded as published.\n")
        if held.get("store") and held["store"] != store:
            print("  Carried to another store, every fingerprint still says the part is "
                  "published,\n  so the next sync would find nothing to do and the new "
                  "store would stay empty.")
        if held.get("handle_prefix") and held["handle_prefix"] != prefix:
            print("  Under a new handle prefix, every fingerprint moves at once: the next "
                  "sync\n  republishes the whole catalogue under new handles, and the "
                  "products already\n  on the store keep selling under the old ones, "
                  "unmanaged. Archive those first\n  (`{cli} reconcile --apply` with the "
                  "old prefix configured) or keep the prefix.".format(cli=cli_name()))
        print(f"\nAdopting rewrites the snapshot's identity. It does not change a single "
              f"product:\n  the {parts:,} recorded fingerprints stay exactly as they are, "
              f"and the next sync\n  acts on them. Run `{cli_name()} sync --deep --dry-run` "
              f"afterwards to see what that\n  would mean against the live store before "
              f"letting it write anything.")
        if not args.apply:
            print(f"\nPlan only — re-run with --apply.")
            return 0
        state.claim(store, prefix)
        print(f"\nAdopted: {store} / {prefix!r}.")
    return 0


def add_arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with the snapshot-identity commands."""
    sub = p.add_subparsers(dest="action", metavar="<action>")
    adopt = sub.add_parser(
        "adopt", help="record this store and handle prefix on the existing snapshot",
        description=cmd_adopt.__doc__ or __doc__)
    adopt.add_argument("--apply", action="store_true",
                       help="write it (default: say what would change)")
    adopt.set_defaults(func=cmd_adopt)
    p.set_defaults(func=lambda args: (p.print_help(), 0)[1])
    return p


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coreyard state", description=__doc__)
    add_arguments(parser)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
