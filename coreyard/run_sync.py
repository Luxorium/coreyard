"""Orchestrator: source database -> transform -> Shopify, with incremental diffing.

Typical uses:
    python -m coreyard.run_sync --check                 # test DB (and Shopify) connectivity
    python -m coreyard.run_sync --sink csv --limit 25   # dry catalog to out/products.csv
    python -m coreyard.run_sync --sink csv              # full CSV catalog
    python -m coreyard.run_sync --sink api              # push straight to Shopify

The first real run doubles as the bulk load. State lives in ``coreyard_sync_state.sqlite3`` so
subsequent runs only act on adds/changes and can retire sold parts.

The diff here compares the yard against that snapshot, never against the store. That is the
right trade for a run on a timer and it is blind to drift on Shopify's side, which is what
``coreyard reconcile`` and ``coreyard repair`` exist to answer.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from coreyard.config import REPO_ROOT, load_settings
from coreyard.state import DEFAULT_STATE_DB, SyncState, fingerprints_with_images
from coreyard.transform.shopify_csv import ImageResolver

STATE_DB = DEFAULT_STATE_DB
DEFAULT_CSV = REPO_ROOT / "out" / "products.csv"


def _make_resolver(image_base_url: str | None, scan_images: bool = True) -> ImageResolver:
    """Resolve each part's photos, for both the CSV rows and the change fingerprint.

    With a public base URL (e.g. a mirrored bucket) this emits ``{base}/{R#}/{file}``, which
    the CSV sink can link to directly. Without one it still returns the bare filenames,
    because the fingerprint needs to *see* the photo set: with the old empty-list resolver
    every part hashed as "no images", so a photo added or replaced on the share was
    invisible and the listing kept whatever it was first published with, forever.

    The whole share is listed once per run rather than once per part — 26,000 anchored
    lookups would take over an hour, a single directory listing takes one round trip.
    """
    if not scan_images:
        return lambda part: []

    from coreyard.yms.images import SmbImageStore

    store = SmbImageStore()
    base = image_base_url.rstrip("/") if image_base_url else None
    index = store.list_all_inventory_images()

    def resolver(part) -> list[str]:
        key = part.image_key()  # R# — the photo filename stem on the share
        names = index.get(key, [])
        return [f"{base}/{key}/{n}" for n in names] if base else list(names)

    return resolver


def _make_part_resolver(image_base_url: str | None) -> ImageResolver:
    """Per-part photo lookup, for the small candidate sets a delta run produces.

    The full sync lists the whole share once because it asks about 26,000 parts. A delta run
    asks about a few dozen, where anchored per-part lookups are both cheaper and fresher.
    The returned shape is deliberately identical to the full resolver's — same filenames, same
    order — because these strings land in the stored fingerprint, and a delta run that spelled
    them differently would mark every part it touched as changed.
    """
    from coreyard.yms.images import SmbImageStore

    store = SmbImageStore()
    base = image_base_url.rstrip("/") if image_base_url else None

    def resolver(part) -> list[str]:
        key = part.image_key()
        names = store.list_inventory_images(key)
        return [f"{base}/{key}/{n}" for n in names] if base else list(names)

    return resolver


def cmd_check(args) -> int:
    from coreyard.yms.db import ping

    print("Checking source database ...")
    try:
        print("  OK:", ping().splitlines()[0])
    except Exception as exc:
        print("  FAILED:", exc)
        return 1

    # Shopify is optional at check time.
    try:
        from coreyard.sink.shopify_api import ShopifySink

        print("Checking Shopify Admin API ...")
        print("  OK: connected to", ShopifySink().check())
    except Exception as exc:
        print("  (Shopify not configured / skipped):", exc)
    return 0


def _retirement_plan(diff, args) -> tuple[list[str], str]:
    """Decide whether ``diff.removed`` may actually be retired. Returns (r_numbers, note).

    Retirement is the one destructive thing this tool does, and "the part is no longer in
    the extract" has innocent causes as well as guilty ones: a partial run, a schema edit, a
    database that answered but returned nothing. Two guards, both aimed at unattended runs:

    * ``--limit`` means the extract deliberately saw only a slice of the yard, so almost
      everything is "missing". Never retire from a partial view.
    * A removal fraction above the threshold is treated as a fault, not as a sale. Real
      sales trickle; a sudden majority means the extract, not the yard, changed.
    """
    if args.sink != "api" or not diff.removed:
        return [], ""
    if args.limit:
        return [], (f"  NOT retiring {len(diff.removed)} missing part(s): --limit means this run "
                    f"only saw part of the yard.")
    known = len(diff.removed) + len(diff.added) + len(diff.changed) + len(diff.unchanged)
    share = len(diff.removed) / known if known else 0.0
    if share > args.max_retire_fraction and not args.force_retire:
        return [], (f"  REFUSING to retire {len(diff.removed)} part(s) — {share:.0%} of the "
                    f"catalogue, over the {args.max_retire_fraction:.0%} limit. That usually "
                    f"means the extract broke, not that the yard sold out. Re-run with "
                    f"--force-retire if it is real.")
    return list(diff.removed), ""


def _enrich(conn, parts) -> None:
    """Attach part-type aliases and donor-vehicle detail, when this site asked for them.

    Off unless ``COREYARD_ENRICH`` is set, because these strings reach the rendered product
    and therefore the fingerprint: turning it on re-publishes every part it touches, which
    is a decision to schedule rather than to inherit from an upgrade.
    """
    from coreyard.yms.enrich import CatalogEnricher, enabled

    if not (parts and enabled()):
        return
    try:
        CatalogEnricher(conn).attach(parts)
    except Exception as exc:
        # Enrichment is additive. Losing it degrades the copy; failing the run over it would
        # stop parts reaching the storefront at all.
        print(f"  (enrichment skipped: {exc})")


def _delta_baseline():
    """The server clock to hand the next delta run, or None if this site has no delta mapping.

    Best-effort: a full sync is useful whether or not delta support is configured, so a
    failure to read the clock must not fail the run — it only means the next delta run has
    no fresher cursor to start from.
    """
    try:
        from coreyard.yms import schema as schema_mod

        if not schema_mod.load().supports_delta:
            return None
        from coreyard.yms.db import connect, server_now
        from coreyard.yms.delta import DEFAULT_OVERLAP

        with connect() as conn:
            return server_now(conn) - DEFAULT_OVERLAP
    except Exception as exc:
        print(f"  (could not read the source clock for a delta cursor: {exc})")
        return None


def cmd_delta(args) -> int:
    """Catch-up run: publish what changed since the last cursor, retire what left scope.

    Complements the full sync rather than replacing it. It is cheap enough to run every few
    minutes, which is what closes the window where a part sold at the counter (or on another
    sales channel) stays buyable online until the next hourly extract.
    """
    from coreyard.yms import schema as schema_mod
    from coreyard.yms.delta import CURSOR_NAME, fetch_changes
    from coreyard.yms.inventory import is_configured

    if not is_configured():
        print("No schema mapping yet — see README 'Map your database'.", file=sys.stderr)
        return 2
    mapping = schema_mod.load()
    if not mapping.supports_delta:
        print("This mapping has no 'modified_at' expression, so it cannot answer "
              "'what changed since ...'.\nAdd one (see schema.example.json) or run a full "
              "sync.", file=sys.stderr)
        return 2

    settings = load_settings()
    with SyncState(STATE_DB) as state:
        cursor = state.get_cursor(CURSOR_NAME)
        if not cursor:
            print("No delta cursor yet. Run a full sync first — it records the cursor "
                  "alongside the snapshot this run has to diff against.", file=sys.stderr)
            return 2

        print(f"Fetching changes since {cursor} ...")
        changes = fetch_changes(datetime.fromisoformat(cursor))
        print("  " + changes.summary())
        if changes.truncated:
            print(f"  NOTE: more than {len(changes.listable) + len(changes.left_scope)} rows "
                  f"changed; this is no longer a delta. Publishing what was read, but NOT "
                  f"retiring — run a full sync to resynchronise.")

        resolver = _make_part_resolver(args.image_base_url)
        current, image_fps = fingerprints_with_images(
            changes.listable, resolver, settings.store)
        diff = state.diff(current, image_fps, detect_removals=False)
        print("  vs last publish:", diff.summary())

        # Only parts we believe are published may be retired, and only because we hold the
        # row that says they left scope. Absence proves nothing here and is never used.
        published = state.load()
        retire = [r for r in changes.left_scope if r in published]
        if changes.truncated:
            retire = []
        elif retire and published:
            share = len(retire) / len(published)
            if share > args.max_retire_fraction and not args.force_retire:
                print(f"  REFUSING to retire {len(retire)} part(s) — {share:.0%} of the "
                      f"catalogue in one delta. Re-run with --force-retire if it is real.")
                retire = []

        revivals = {r: s for r, s in state.retired_statuses().items() if r in current}
        todo_keys = set(diff.added) | set(diff.changed)
        todo = [p for p in changes.listable if p.uid() in todo_keys]

        if args.dry_run:
            print(f"Dry run: would upsert {len(todo)} product(s) "
                  f"({len(set(diff.image_changed) & todo_keys)} photo refresh), "
                  f"revive {len(revivals)}, and retire {len(retire)}.")
            print("Dry run: cursor NOT advanced.")
            return 0

        from coreyard.sink.shopify_write import ShopifyPublisher

        publisher = ShopifyPublisher(store=settings.store, status=args.status)
        if todo:
            from coreyard.yms.db import connect
            from coreyard.yms.interchange import InterchangeResolver

            print(f"Resolving fitment for {len(todo)} part(s) ...")
            with connect() as conn:
                InterchangeResolver(conn).attach(todo)
                _enrich(conn, todo)

        # A part whose photos moved needs its media rebuilt even when the fingerprint moved
        # for an unrelated reason, so both signals are unioned.
        needs_photos = set(diff.image_changed) | changes.photo_changed
        revived: set[str] = set()
        published_ok: set[str] = set()
        for i, part in enumerate(todo, 1):
            key = part.uid()
            try:
                publisher.publish(part, refresh_images=key in needs_photos,
                                  revive_status=revivals.get(key))
            except RuntimeError as exc:
                # Leave this part out of the state update so the next run retries it.
                print(f"  R#{key}: {exc}")
                continue
            published_ok.add(key)
            if key in revivals:
                revived.add(key)
            if i % 25 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)}")
        state.clear_retired(revived)

        retired: set[str] = set()
        if retire:
            print(f"Retiring {len(retire)} part(s) that left scope "
                  f"(qty 0, {publisher.retire_status}) ...")
            for i, r_number in enumerate(retire, 1):
                try:
                    # Out of scope, not sold: leave an already-invisible draft as it is.
                    publisher.retire(r_number, record_prior=state.record_retired,
                                     skip_draft=True)
                    retired.add(r_number)
                except RuntimeError as exc:
                    print(f"  R#{r_number}: {exc}")
                if i % 25 == 0 or i == len(retire):
                    print(f"  {i}/{len(retire)}")

        # Record only what actually landed, then advance the cursor. A part that failed to
        # publish keeps its old fingerprint (or none), so the next run picks it up again.
        state.update({k: v for k, v in current.items() if k in published_ok},
                     {k: v for k, v in image_fps.items() if k in published_ok})
        state.forget(retired)
        if changes.cursor:
            state.set_cursor(CURSOR_NAME, changes.cursor.isoformat())
            print(f"Cursor advanced to {changes.cursor.isoformat()}.")
    return 0


def cmd_sync(args) -> int:
    from coreyard.yms.inventory import fetch_parts, is_configured, photos_required

    if not is_configured():
        print(
            "No schema mapping yet — CoreYard ships none, because the table and column\n"
            "names belong to your yard system's vendor, not to this project.\n\n"
            "  cp schema.example.json schema.json\n"
            "  python -m coreyard.yms.discover_schema   # lists your tables/columns\n\n"
            "then replace each PLACEHOLDER in schema.json. See README 'Map your database'.",
            file=sys.stderr,
        )
        return 2

    settings = load_settings()

    # Taken before the extract, not after: rows that move while a long full run is reading
    # are still ahead of this mark, so the next delta run picks them up instead of assuming
    # the full run must already have seen them.
    baseline = _delta_baseline() if not args.limit else None

    resolver = _make_resolver(args.image_base_url, scan_images=not args.no_image_scan)
    print(f"Fetching parts (limit={args.limit or 'none'}) ...")
    parts = fetch_parts(limit=args.limit)
    print(f"  {len(parts)} listable part(s)"
          f"{' (photos required)' if photos_required() else ''}")

    # Incremental diff
    current, image_fps = fingerprints_with_images(parts, resolver, settings.store)
    with SyncState(STATE_DB) as state:
        diff = state.diff(current, image_fps)
        print("Diff vs last run:", diff.summary())

        publisher = None
        if args.sink == "api":
            from coreyard.sink.shopify_write import ShopifyPublisher

            publisher = ShopifyPublisher(store=settings.store, status=args.status)
            if args.reconcile:
                # Ask the store what it actually has, rather than trusting the state file.
                # A fresh state file knows about nothing, and the bulk path keeps its own
                # progress log, so parts sold before this run existed would otherwise stay
                # on sale forever with no record that they were ever published.
                print("Reconciling against the live store ...")
                published = publisher.published_r_numbers()
                diff.removed = sorted(published - set(current))
                print(f"  {len(published)} published, {len(diff.removed)} no longer listable")

        retire, why = _retirement_plan(diff, args)
        if why:
            print(why)

        retired: set[str] = set()
        # Parts that were archived and are listable again — a voided work order puts one
        # back in the yard. They come back as whatever they were before, not as ARCHIVED
        # (which the status read-back would otherwise re-send) and not as DRAFT (which
        # would hide a product that was on sale).
        revivals = {r: s for r, s in state.retired_statuses().items() if r in current}
        if revivals:
            print(f"  {len(revivals)} archived part(s) are back in the yard.")

        if args.sink == "csv":
            from coreyard.sink.csv_sink import write

            out = Path(args.out)
            products, rows = write(parts, out, resolver, settings.store)
            print(f"Wrote {products} products / {rows} rows -> {out}")
            if diff.removed:
                print(f"NOTE: {len(diff.removed)} previously-listed parts are gone (sold). "
                      f"A CSV import cannot retire them — use --sink api. They stay in the "
                      f"state file and will be reported again until something does.")
        elif args.dry_run:
            # A dry run against the API sink must not write to the live store. Writing the
            # CSV above is harmless and is the point of that path; upserting 9,000 products
            # "as a preview" is not.
            upserts = 0 if args.retire_only else len(diff.added) + len(diff.changed)
            print(f"Dry run: would upsert {upserts} product(s) "
                  f"({0 if args.retire_only else len(diff.image_changed)} photo refresh), "
                  f"revive {0 if args.retire_only else len(revivals)}, "
                  f"and retire {len(retire)}.")
        else:  # api
            todo = [p for p in parts if p.uid() in set(diff.added) | set(diff.changed)]
            if todo:
                # Resolve fitment BEFORE publishing. Without it the renderer falls back to
                # the donor vehicle alone and a routine incremental run would strip the
                # multi-model titles, "Fits" list and year tags off every changed product.
                # Only the added/changed subset is resolved, so this stays cheap.
                from coreyard.yms.db import connect
                from coreyard.yms.interchange import InterchangeResolver

                print(f"Resolving fitment for {len(todo)} part(s) ...")
                with connect() as conn:
                    InterchangeResolver(conn).attach(todo)
                    _enrich(conn, todo)

            needs_photos = set(diff.image_changed)
            if args.retire_only:
                print(f"Retire-only: skipping {len(todo)} upsert(s).")
                todo = []
            print(f"Upserting {len(todo)} new/changed products to Shopify "
                  f"({len(needs_photos & {p.uid() for p in todo})} with changed photos) ...")
            revived: set[str] = set()
            for i, part in enumerate(todo, 1):
                publisher.publish(part, refresh_images=part.uid() in needs_photos,
                                  revive_status=revivals.get(part.uid()))
                if part.uid() in revivals:
                    revived.add(part.uid())
                if i % 25 == 0 or i == len(todo):
                    print(f"  {i}/{len(todo)}")
            # Only after the upsert landed: a part whose revival failed must still be
            # remembered, or the retry would republish it as ARCHIVED.
            state.clear_retired(revived)

            if retire:
                print(f"Retiring {len(retire)} sold part(s) (qty 0, {publisher.retire_status}) ...")
                skipped_drafts = 0
                for i, r_number in enumerate(retire, 1):
                    try:
                        # Absence is why this part is here, not a sale. A DRAFT product is
                        # already invisible, so archiving it would only destroy the
                        # difference between "not ready" and "gone".
                        outcome = publisher.retire(r_number,
                                                   record_prior=state.record_retired,
                                                   skip_draft=True)
                        # A skipped draft still leaves the snapshot: it is not listable, and
                        # keeping it would re-propose the same no-op on every future run.
                        retired.add(r_number)
                        skipped_drafts += outcome == "draft"
                    except RuntimeError as exc:
                        # One stubborn product must not strand the rest, and an R# that was
                        # not retired must stay in the snapshot so the next run tries again.
                        print(f"  R#{r_number}: {exc}")
                    if i % 25 == 0 or i == len(retire):
                        print(f"  {i}/{len(retire)}")
                if skipped_drafts:
                    print(f"  ({skipped_drafts} already-draft product(s) left alone)")

        if args.retire_only:
            # Nothing was published, so committing `current` would record 26,000 parts as
            # live and the next run would skip every one of them as "unchanged".
            print("Retire-only: state NOT committed.")
        elif not args.dry_run:
            # Anything we believed was published but did not retire is carried forward at
            # its old fingerprint. Dropping it here would silently forget a part that is
            # still live on the storefront with stock it no longer has.
            snapshot = dict(current)
            carried = {r: fp for r, fp in state.load().items()
                       if r in set(diff.removed) - retired}
            snapshot.update(carried)
            state.commit(snapshot, image_fps)
            print(f"State committed{f' ({len(carried)} unretired carried forward)' if carried else ''}.")
            if baseline is not None:
                # Only a full run may set this: it is the only one that has just reconciled
                # the whole yard, which is what makes "everything before this mark is already
                # published" true.
                from coreyard.yms.delta import CURSOR_NAME

                state.set_cursor(CURSOR_NAME, baseline.isoformat())
        else:
            print("Dry run: state NOT committed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="coreyard.run_sync", description="source database -> Shopify sync")
    p.add_argument("--check", action="store_true", help="test connectivity and exit")
    p.add_argument("--delta", action="store_true",
                   help="publish only what changed since the last run's cursor and retire "
                        "what left scope (cheap catch-up; needs a prior full sync)")
    p.add_argument("--sink", choices=["csv", "api"], default="csv")
    p.add_argument("--limit", type=int, default=None, help="max parts (for dry runs)")
    p.add_argument("--out", default=str(DEFAULT_CSV), help="CSV output path")
    p.add_argument("--image-base-url", default=None,
                   help="public base URL for mirrored images (CSV sink)")
    p.add_argument("--no-image-scan", action="store_true",
                   help="skip the photo-share listing (faster; photo changes go undetected)")
    p.add_argument("--status", default="DRAFT", choices=["DRAFT", "ACTIVE"],
                   help="status for NEW products; existing products keep theirs (default DRAFT)")
    p.add_argument("--retire-only", action="store_true",
                   help="retire sold parts without publishing anything (pairs with --reconcile)")
    p.add_argument("--reconcile", action="store_true",
                   help="ask Shopify what is published instead of trusting the state file "
                        "(use for the first run, or after a bulk load)")
    p.add_argument("--force-retire", action="store_true",
                   help="retire sold parts even if they exceed the safety fraction")
    p.add_argument("--max-retire-fraction", type=float, default=0.10,
                   help="refuse to retire more than this share of the catalogue (default 0.10)")
    p.add_argument("--dry-run", action="store_true",
                   help="report the diff only: no Shopify writes, no state commit")
    args = p.parse_args(argv)

    try:
        if args.check:
            return cmd_check(args)
        if args.delta:
            if args.sink != "api":
                print("--delta publishes through the Admin API; pass --sink api.",
                      file=sys.stderr)
                return 2
            return cmd_delta(args)
        return cmd_sync(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
