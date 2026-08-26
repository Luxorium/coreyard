"""``coreyard reconcile`` — compare the yard with the store and close the safe differences.

    bin/coreyard reconcile                 # plan only; writes nothing
    bin/coreyard reconcile --apply
    bin/coreyard reconcile --apply --activate      # promote listable drafts
    bin/coreyard reconcile --apply --no-retire
    bin/coreyard reconcile --repair-state          # forget snapshot entries the store lacks

Read-only against the source database.
"""

from __future__ import annotations

import argparse
import sys

from coreyard.config import load_store, publication_names
from coreyard.reconcile import planner
from coreyard.reconcile.store import scan
from coreyard.state import DEFAULT_STATE_DB, SyncState

_SET_STATUS = """mutation($id:ID!,$status:ProductStatus!){
  productUpdate(product:{id:$id, status:$status}){ userErrors{ field message } } }"""


def _sample(label: str, items) -> None:
    if items:
        shown = [i if isinstance(i, str) else i[0] for i in items[:8]]
        print(f"    {label} sample: {shown}")


def run(args) -> int:
    from coreyard.sink.shopify_api import ShopifyClient
    from coreyard.sink.shopify_write import ShopifyPublisher
    from coreyard.yms.inventory import is_configured, listable_r_numbers, photos_required

    if not is_configured():
        print("No schema mapping yet — see README 'Map your database'.", file=sys.stderr)
        return 2

    store = load_store()
    client = ShopifyClient()

    print("Asking the yard which parts are listable ...")
    listable = listable_r_numbers()
    print(f"  {len(listable)} listable"
          f"{' (photos required)' if photos_required() else ''}")

    print("Asking Shopify what it holds ...")
    shop = scan(client, store)
    by_status: dict[str, int] = {}
    for product in shop.values():
        by_status[product.status] = by_status.get(product.status, 0) + 1
    print(f"  {len(shop)} product(s) under handle prefix {store.handle_prefix!r}  {by_status}")

    with SyncState(DEFAULT_STATE_DB) as state:
        actions = planner.plan(
            listable, shop,
            state=state.load().keys(),
            remembered=state.retired_statuses(),
            activate=args.activate,
            retire=not args.no_retire,
            publish_channels=bool(publication_names()),
            max_retire_fraction=args.max_retire_fraction,
            force_retire=args.force_retire,
        )

        print()
        print(f"  create   (absent; run a sync)   {len(actions.create)}")
        print(f"  activate (DRAFT -> ACTIVE)      {len(actions.activate)}"
              f"{'' if args.activate else '   [--activate to enable]'}")
        print(f"  revive   (ARCHIVED -> listable) {len(actions.revive)}")
        print(f"  retire   (ACTIVE -> ARCHIVED)   {len(actions.retire)}")
        print(f"  publish  (on no sales channel)  {len(actions.publish)}")
        print(f"  state    stale {len(actions.drift.stale)}, "
              f"untracked {len(actions.drift.untracked)}")
        for label, items in (("create", actions.create), ("activate", actions.activate),
                             ("revive", actions.revive), ("retire", actions.retire),
                             ("publish", actions.publish),
                             ("state stale", actions.drift.stale)):
            _sample(label, items)
        if actions.refused:
            print(f"\n  {actions.refused}")

        if not (args.apply or args.repair_state):
            print(f"\nPlan only — {actions.writes} status change(s) and "
                  f"{len(actions.drift.stale)} snapshot entr(ies) would be corrected. "
                  f"Re-run with --apply.")
            return 0

        if args.repair_state and actions.drift.stale:
            # These are the entries that make the sync skip a part forever: it believes the
            # part is published and the store has no such product. Forgetting them lets the
            # next run create it.
            state.forget(actions.drift.stale)
            print(f"\nForgot {len(actions.drift.stale)} snapshot entr(ies) with no product; "
                  f"the next sync will create them.")

        if not args.apply:
            return 0

        publisher = ShopifyPublisher(store=store)

        def set_status(r_number: str, status: str) -> bool:
            try:
                client.mutate(_SET_STATUS,
                              {"id": shop[r_number].product_id, "status": status},
                              "productUpdate")
                return True
            except RuntimeError as exc:
                print(f"    ! R#{r_number}: {exc}", file=sys.stderr)
                return False

        done = 0
        for r_number in actions.activate:
            done += set_status(r_number, planner.ACTIVE)
        if actions.activate:
            print(f"  activated: {done}/{len(actions.activate)}")

        done = 0
        revived: list[str] = []
        for r_number, status in actions.revive:
            if set_status(r_number, status):
                done += 1
                revived.append(r_number)
        if actions.revive:
            print(f"  revived: {done}/{len(actions.revive)}")
        # Only once the product is actually back does the memory stop being needed; a failed
        # revival must stay remembered or the retry republishes it as ARCHIVED.
        state.clear_retired(revived)

        if actions.publish and publisher.publications:
            done = 0
            for r_number in actions.publish:
                try:
                    publisher.publish_to_channels(shop[r_number].product_id)
                    done += 1
                except RuntimeError as exc:
                    print(f"    ! R#{r_number}: {exc}", file=sys.stderr)
            print(f"  published to channel: {done}/{len(actions.publish)}")

        if actions.retire:
            print(f"  Retiring {len(actions.retire)} product(s) "
                  f"(qty 0, then {publisher.retire_status}) ...")
            retired: list[str] = []
            for i, r_number in enumerate(actions.retire, 1):
                try:
                    # The sync path's retirement, not a second implementation: same
                    # zero-stock-first ordering, same pre-archive status memory.
                    publisher.retire(r_number, record_prior=state.record_retired)
                    retired.append(r_number)
                except RuntimeError as exc:
                    print(f"    ! R#{r_number}: {exc}", file=sys.stderr)
                if i % 25 == 0 or i == len(actions.retire):
                    print(f"    {i}/{len(actions.retire)}")
            state.forget(retired)

    if actions.create:
        print(f"\n{len(actions.create)} listable part(s) are absent from Shopify. "
              f"A sync creates them with photos, weights and SEO: "
              f"bin/coreyard --sink api --dry-run")
    return 0


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with the reconciliation flags."""
    ap.add_argument("--apply", action="store_true", help="write the status changes")
    # Accepted everywhere, so an operator never has to remember which commands plan by
    # default and which write. Here it is the explicit spelling of "do not --apply".
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only, writing nothing (the default)")
    ap.add_argument("--activate", action="store_true",
                    help="promote listable DRAFT products to ACTIVE")
    ap.add_argument("--no-retire", action="store_true", help="publish and revive only")
    ap.add_argument("--force-retire", action="store_true",
                    help="archive even beyond the safety fraction")
    ap.add_argument("--max-retire-fraction", type=float, default=0.10,
                    help="refuse to archive more than this share of active products")
    ap.add_argument("--repair-state", action="store_true",
                    help="forget snapshot entries whose product does not exist, so the "
                         "next sync creates them")
    ap.set_defaults(func=dispatch)
    return ap


def dispatch(args) -> int:
    if getattr(args, "dry_run", False):
        args.apply = False
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard reconcile",
                                 description=__doc__.splitlines()[0])
    add_arguments(ap)
    return dispatch(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
