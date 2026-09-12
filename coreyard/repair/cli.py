"""``coreyard repair`` — rewrite catalog output that an older renderer produced.

    bin/coreyard repair titles --dry-run
    bin/coreyard repair titles --apply
    bin/coreyard repair tags   --dry-run
    bin/coreyard repair seo --apply --limit 200
    bin/coreyard repair weights --apply
    bin/coreyard repair metafields --apply       # grade, mileage, condition, fitment
    bin/coreyard repair all --dry-run
    bin/coreyard repair fingerprints --dry-run     # state only; no Shopify writes

Every subcommand reports before it writes, and ``--dry-run`` is the default. Repairs are
idempotent: a re-run finds only what still differs, so an interrupted run just resumes.
"""

from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from coreyard.config import load_store
from coreyard.repair import engine

# Which rendered fields each subcommand owns.
_SUBCOMMANDS = {
    "titles": ("title", "type"),
    "tags": ("tags",),
    "seo": ("seo",),
    "descriptions": ("description",),
    "weights": ("weight",),
    "metafields": ("metafields",),
    "all": engine.FIELDS,
}


def _short(value: object, width: int = 96) -> str:
    text = ", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _desired(store, r_numbers: set[str], need_fitment: bool) -> dict:
    """Render what each part should look like now, for the parts still in the extract.

    Fitment has to be resolved before rendering. ``fetch_parts`` returns the donor vehicle
    only, and the renderer falls back to it when ``part.fitment`` is empty — so repairing
    straight off a bare extract would replace every multi-model title with the single donor
    car, which is a downgrade dressed up as a repair.
    """
    from coreyard.transform.render import render
    from coreyard.yms.inventory import fetch_parts

    parts = [p for p in fetch_parts() if p.uid() in r_numbers]
    if need_fitment and parts:
        from coreyard.yms.db import connect
        from coreyard.yms.interchange import InterchangeResolver

        print(f"  resolving fitment for {len(parts)} part(s) ...")
        with connect() as conn:
            resolver = InterchangeResolver(conn)
            for index, part in enumerate(parts, 1):
                part.fitment = resolver.fitment_for(part)
                if index % 1000 == 0 or index == len(parts):
                    print(f"    {index}/{len(parts)}", flush=True)
            resolver.flush()
            summary = resolver.cache_summary()
            if summary:
                print(f"  {summary}")
            _enrich(conn, parts)
    return {p.uid(): render(p, [], store) for p in parts}


def _enrich(conn, parts) -> None:
    from coreyard.yms.enrich import CatalogEnricher, enabled

    if not enabled():
        return
    try:
        CatalogEnricher(conn).attach(parts)
    except Exception as exc:                       # additive; never fail a repair over it
        print(f"  (enrichment skipped: {exc})")


def cmd_fingerprints(args) -> int:
    """Re-baseline the sync snapshot against the current renderer, publishing nothing.

    For one situation only: the renderer changed, the *published* products already match it
    (because a repair or a reconcile just made them match), and the alternative is a full
    catalogue rewrite that would send Shopify thousands of updates with identical content.

    It is a deliberate assertion that the store is already correct. Run the relevant repairs
    first, or the snapshot will record output nobody ever published and the sync will never
    notice again.
    """
    from coreyard.state import DEFAULT_STATE_DB, SyncState, fingerprints_all
    from coreyard.yms.inventory import fetch_parts, is_configured

    if not is_configured():
        print("No schema mapping yet — see README 'Map your database'.", file=sys.stderr)
        return 2
    store = load_store()
    print("Fetching listable parts ...")
    parts = fetch_parts()
    print(f"  {len(parts)} part(s)")

    from coreyard.run_sync import _make_resolver

    photos = _make_resolver(None, scan_images=not args.no_image_scan)
    fingerprints = fingerprints_all(parts, photos.resolve, store, stamps=photos.stamps)
    current, images = fingerprints.content, fingerprints.images
    with SyncState(DEFAULT_STATE_DB) as state:
        diff = state.diff(fingerprints)
        print("  vs stored snapshot:", diff.summary())
        if not args.apply:
            print(f"\nDry run: would re-baseline {len(diff.added) + len(diff.changed)} "
                  f"entr(ies) without publishing anything.")
            return 0
        state.update(fingerprints)
        print(f"Re-baselined {len(current)} entr(ies). The next sync publishes only what "
              f"moves from here.")
    return 0


_LOCAL = threading.local()


def _thread_client():
    """One Shopify client per worker thread.

    ``requests.Session`` is not safe to share across threads, and the client's own
    rate-budget tracker (``_resume_at``) is per-instance. ``coreyard bulk`` solves this the
    same way — a publisher, and therefore a client, per thread. The GraphQL cost budget is
    the *store's*, not the connection's, so parallel clients spend it faster and each backs
    off on the 429 or THROTTLED it sees; that degrades throughput rather than failing.
    """
    client = getattr(_LOCAL, "client", None)
    if client is None:
        from coreyard.sink.shopify_api import ShopifyClient

        client = _LOCAL.client = ShopifyClient()
    return client


def _apply_parallel(todo, workers, report):
    """Apply ``todo`` across ``workers`` threads, reporting in submission order.

    Order matters only for the progress line and for which failures are printed first; each
    repair is independent and idempotent, so a thread that fails leaves the product exactly
    as it was for the next run to retry.
    """
    def one(change):
        try:
            engine.apply(_thread_client(), change)
            return change, None
        except RuntimeError as exc:
            return change, exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, (change, exc) in enumerate(pool.map(one, todo), 1):
            report(index, change, exc)


def cmd_repair(args) -> int:
    from coreyard.sink.shopify_api import ShopifyClient
    from coreyard.yms.inventory import is_configured

    fields = _SUBCOMMANDS[args.what]
    needs_yard = set(fields) - {"weight"}
    if needs_yard and not is_configured():
        print("No schema mapping yet — see README 'Map your database'.", file=sys.stderr)
        return 2

    store = load_store()
    client = ShopifyClient()

    print("Reading the catalogue ...")
    live = engine.scan(client, store)
    print(f"  {len(live)} product(s) under handle prefix {store.handle_prefix!r}")
    if args.status:
        live = {r: p for r, p in live.items() if p.status == args.status}
        print(f"  {len(live)} with status {args.status}")
    if not live:
        print("Nothing to do.")
        return 0

    desired = {}
    if needs_yard:
        print("Asking the yard what each one should say ...")
        desired = _desired(store, set(live), need_fitment=True)
        print(f"  {len(desired)} matched a listable part "
              f"({len(live) - len(desired)} not in the yard's current list)")
    weights = (engine.weights_by_product_type(store, (p.product_type for p in live.values()))
               if "weight" in fields else {})

    changes = engine.plan(live, desired, fields, store=store, weights_by_type=weights)
    counts: dict[str, int] = {}
    for change in changes:
        for name in change.fields:
            counts[name] = counts.get(name, 0) + 1
    print(f"\n  {len(changes)} product(s) differ from what CoreYard would publish")
    for name in engine.FIELDS:
        if counts.get(name):
            print(f"    {name:<12} {counts[name]}")
    if not changes:
        return 0

    dropping = [c for c in changes if c.dropped_tags]
    if dropping:
        # Tags are how a storefront filter finds a part; deleting one silently is how a
        # product quietly stops appearing in a collection nobody thought to check.
        total = sum(len(c.dropped_tags) for c in dropping)
        print(f"\n  {total} tag(s) on {len(dropping)} product(s) would be REMOVED as no "
              f"longer generated (external and namespaced tags are kept):")
        for change in dropping[:5]:
            print(f"      R#{change.r_number}: {_short(change.dropped_tags)}")

    shown = changes[: args.show]
    for change in shown:
        print(f"\n  R#{change.r_number}  [{', '.join(change.names())}]")
        for name, (before, after) in sorted(change.fields.items()):
            print(f"      {name} now:  {_short(before)}")
            print(f"      {name} new:  {_short(after)}")
    if len(changes) > len(shown):
        print(f"\n  … and {len(changes) - len(shown)} more")

    if not args.apply:
        print(f"\nDry run — nothing written. Re-run with --apply to repair {len(changes)} "
              f"product(s).")
        return 0

    todo = changes[: args.limit] if args.limit else changes
    workers = max(1, getattr(args, "workers", 1))
    suffix = f" with {workers} workers" if workers > 1 else ""
    print(f"\nApplying {len(todo)} repair(s){suffix} ...")
    done = failed = 0

    def report(i, change, exc):
        nonlocal done, failed
        if exc is None:
            done += 1
        else:
            failed += 1
            if failed <= 5:
                print(f"  ! R#{change.r_number}: {exc}", file=sys.stderr)
        if i % 100 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)}", flush=True)

    if workers == 1:
        # The serial path keeps the client that already walked the catalogue, and its
        # warmed rate budget with it.
        for i, change in enumerate(todo, 1):
            try:
                engine.apply(client, change)
                report(i, change, None)
            except RuntimeError as exc:
                report(i, change, exc)
    else:
        _apply_parallel(todo, workers, report)
    print(f"Repaired {done} product(s), {failed} failed.")
    if done:
        print("The sync snapshot still holds the old fingerprints; the next sync will "
              "re-publish these parts, or `repair fingerprints --apply` records them as "
              "already correct.")
    return 1 if failed else 0


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with one subcommand per repairable group of rendered fields."""
    sub = ap.add_subparsers(dest="what", required=True)
    for name in _SUBCOMMANDS:
        p = sub.add_parser(name, help=f"repair {name}")
        p.add_argument("--apply", action="store_true", help="write the changes")
        p.add_argument("--dry-run", action="store_true",
                       help="report only (the default)")
        p.add_argument("--limit", type=int, default=0,
                       help="apply at most this many, for a cautious first run")
        p.add_argument("--show", type=int, default=10, help="examples to print")
        p.add_argument("--workers", type=int, default=1,
                       help="apply this many products at once (default 1). The write is "
                            "one API call per product, so a whole-catalogue repair is "
                            "bound by round trips rather than by the store's budget.")
        p.add_argument("--status", default=None, choices=["ACTIVE", "DRAFT", "ARCHIVED"],
                       help="only products with this status")
        p.set_defaults(func=cmd_repair)

    f = sub.add_parser("fingerprints",
                       help="record the current render as the sync baseline (no Shopify "
                            "writes) — only when the store is already correct")
    f.add_argument("--apply", action="store_true",
                   help="write the recomputed fingerprints to the sync state")
    f.add_argument("--dry-run", action="store_true",
                   help="report what would be rewritten (the default)")
    f.add_argument("--no-image-scan", action="store_true",
                   help="skip the photo-share listing")
    f.set_defaults(func=cmd_fingerprints)
    return ap


def dispatch(args) -> int:
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard repair",
                                 description=__doc__.splitlines()[0])
    add_arguments(ap)
    return dispatch(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
