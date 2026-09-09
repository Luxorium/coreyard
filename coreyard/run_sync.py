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
from pathlib import Path, PurePosixPath
from typing import NamedTuple, Optional

from coreyard import ops
from coreyard.config import DATA_ROOT, bundled, cli_name, load_settings, out_dir
from coreyard.state import DEFAULT_STATE_DB, SyncState, fingerprints_all

# The channel this pipeline publishes to. Recorded alongside the canonical
# snapshot so a second channel's successes and failures stay its own.
CHANNEL = "shopify"
from coreyard.state import subset as fp_subset
from coreyard.transform.shopify_csv import ImageResolver

STATE_DB = DEFAULT_STATE_DB

# How often a long publish run writes its progress to the snapshot.
#
# Without this a run that does not finish records nothing, and "does not finish" is the
# normal case for a large backlog: the scheduled job is wrapped in a timeout so it cannot
# overrun the next launch, so a backlog bigger than one window could never be worked off.
# It is not a hypothetical — an installation here spent twelve consecutive hourly runs
# publishing the same ~2,000 of the same ~9,200 products, because each run was stopped
# before its commit and the next one started again from the beginning.
#
# Checkpointing is only safe because a fingerprint is recorded strictly after the publish
# it describes returned cleanly, so an interrupted run leaves a smaller snapshot, never a
# wrong one.
CHECKPOINT_EVERY = 100
DEFAULT_CSV = out_dir() / "products.csv"


class Photos(NamedTuple):
    """How one run reads the photo share.

    ``resolve`` yields the references that reach the rendered product; ``stamps`` yields the
    share's manifest for the same part — the same filenames plus size and modification time.
    They are separate because they answer different questions, and because only the second
    can see a photo overwritten under its own name.
    """

    resolve: "ImageResolver"
    stamps: object


# Donor photos are namespaced before they reach a fingerprint. The two folders share a key
# space — donor vehicle 1001 and R# 1001 both file a "1001_01.jpg" — so an unnamespaced
# reference could not tell one picture from the other.
_DONOR_PREFIX = "donor:"


def _photo_view(inventory_entries, vehicle_entries, donors: dict, base: str | None) -> Photos:
    """The (resolve, stamps) pair both sync paths share.

    ``inventory_entries`` and ``vehicle_entries`` take a folder key and return that folder's
    ordered (name, stamp) pairs. The full path serves them from one listing of each folder;
    the delta path lists per key. Everything downstream of that is deliberately identical:
    these strings land in the stored fingerprint, and a delta run that spelled them
    differently would mark every part it touched as changed.

    Choosing the photo set is also where a part learns it fell back, because the renderer has
    to say so and only this code knows. A part with photographs of its own never consults the
    donor folder, so its references, its fingerprint and its copy are all untouched by this.
    """
    from coreyard.yms.images import donor_photo_limit

    def chosen(part) -> tuple[str, list]:
        own = inventory_entries(part.image_key())
        if own:
            part.uses_donor_photos = False
            return "", own
        donor_key = donors.get(str(part.stock_number or "").strip(), "")
        entries = vehicle_entries(donor_key)[:donor_photo_limit()] if donor_key else []
        part.donor_image_key = donor_key or None
        part.uses_donor_photos = bool(entries)
        return (donor_key if entries else ""), entries

    def resolve(part) -> list[str]:
        donor_key, entries = chosen(part)
        names = [name for name, _ in entries]
        if base:
            return [f"{base}/{donor_key or part.image_key()}/{n}" for n in names]
        return [f"{_DONOR_PREFIX}{n}" for n in names] if donor_key else names

    def stamps(part) -> list[str]:
        donor_key, entries = chosen(part)
        return [f"{_DONOR_PREFIX}{stamp}" if donor_key else stamp for _, stamp in entries]

    return Photos(resolve, stamps)


def _carried_photos(image_base_url: str | None) -> "Photos | None":
    """The photo view for a source that carries its own image lists, or None for the database.

    A tabular source attaches ``part.images`` while it reads its rows — from an ``images``
    column, or by globbing a local directory named by ``COREYARD_SOURCE_IMAGES``. There is
    no share to list, so listing one is not merely wasted work: it made `sync` shell out to
    `smbclient` on an installation that has no file server, no credentials and, in the
    demo's case, no photographs at all. That was the last thing standing between a clean
    install and the quickstart on the front page.

    Stamps are the same strings as the references. A local file's size and modification time
    would be a stronger stamp, but only the SMB manifest can see a photo overwritten under
    its own name, and inventing a second spelling here would mark every part changed on the
    first run that used it. Donor photographs are a database capability and stay absent.
    """
    from coreyard.config import source_traits

    if not source_traits().carries_own_photos:
        return None

    base = image_base_url.rstrip("/") if image_base_url else None

    def names(part) -> list[str]:
        return [PurePosixPath(str(name)).name for name in (part.images or [])]

    def resolve(part) -> list[str]:
        found = names(part)
        if base:
            return [f"{base}/{part.image_key()}/{name}" for name in found]
        return found

    return Photos(resolve, names)


def _make_resolver(image_base_url: str | None, scan_images: bool = True) -> Photos:
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
        return Photos(lambda part: [], lambda part: [])

    carried = _carried_photos(image_base_url)
    if carried is not None:
        return carried

    from coreyard.yms.images import SmbImageStore

    from coreyard.yms.inventory import donor_image_keys

    store = SmbImageStore()
    base = image_base_url.rstrip("/") if image_base_url else None
    index = store.list_all_inventory_manifest()
    # Empty unless the site mapped ``donor_images``, and the second listing is skipped
    # entirely when it did not — an installation without the mapping pays nothing.
    donors = donor_image_keys()
    vehicles = store.list_all_vehicle_manifest() if donors else {}
    return _photo_view(lambda key: index.get(key, []),
                       lambda key: vehicles.get(key, []), donors, base)


def _make_part_resolver(image_base_url: str | None) -> Photos:
    """Per-part photo lookup, for the small candidate sets a delta run produces.

    The full sync lists the whole share once because it asks about 26,000 parts. A delta run
    asks about a few dozen, where anchored per-part lookups are both cheaper and fresher.
    The returned shape is deliberately identical to the full resolver's — same filenames, same
    order — because these strings land in the stored fingerprint, and a delta run that spelled
    them differently would mark every part it touched as changed.
    """
    carried = _carried_photos(image_base_url)
    if carried is not None:
        return carried

    from coreyard.yms.images import SmbImageStore
    from coreyard.yms.inventory import donor_image_keys

    store = SmbImageStore()
    base = image_base_url.rstrip("/") if image_base_url else None
    # One listing per part, reused by both callbacks, so a delta run makes one round trip
    # per part rather than two — and so the references and the manifest describe the same
    # instant. Two listings could straddle a photo being replaced.
    cache: dict[str, list[tuple[str, str]]] = {}
    donor_cache: dict[str, list[tuple[str, str]]] = {}

    def entries(key: str) -> list[tuple[str, str]]:
        if key not in cache:
            cache[key] = store.list_inventory_manifest(key)
        return cache[key]

    def donor_entries(key: str) -> list[tuple[str, str]]:
        if key not in donor_cache:
            donor_cache[key] = store.list_vehicle_manifest(key)
        return donor_cache[key]

    return _photo_view(entries, donor_entries, donor_image_keys(), base)


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

    * A partial view — ``--limit``, ``--r-number`` — means the extract deliberately saw only
      a slice of the yard, so almost everything is "missing". Never retire from one.
    * A scope other than inventory has no opinion about availability at all: a photo run
      that archived parts would be taking a decision it did not gather evidence for.
    * A removal fraction above the threshold is treated as a fault, not as a sale. Real
      sales trickle; a sudden majority means the extract, not the yard, changed.
    """
    if args.sink != "api" or not diff.removed:
        return [], ""
    scope = getattr(args, "scope", None)
    if scope not in (None, "inventory"):
        return [], (f"  NOT retiring {len(diff.removed)} missing part(s): `sync {scope}` "
                    f"does not decide availability — use `coreyard sync inventory`.")
    partial = _partial_view(args)
    if partial:
        return [], (f"  NOT retiring {len(diff.removed)} missing part(s): {partial}.")
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
    from coreyard.config import source_traits

    # A delta cursor is anchored to the source server's own clock, so a source without one
    # has nothing to fail at — and reporting the failure told a demo user their missing
    # vendor schema was a problem when nothing on their path needs one.
    if not source_traits().supports_delta:
        return None
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
                  f"retiring and NOT advancing the cursor — run a full sync to resynchronise.")

        photos = _make_part_resolver(args.image_base_url)
        resolver = photos.resolve
        fingerprints = fingerprints_all(changes.listable, resolver, settings.store,
                                        stamps=photos.stamps)
        current, image_fps = fingerprints.content, fingerprints.images
        diff = state.diff(fingerprints, detect_removals=False)
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
                  f"({len(_photo_refreshes(diff, args, todo_keys, todo))} photo refresh), "
                  f"revive {len(revivals)}, and retire {len(retire)}.")
            print("Dry run: cursor NOT advanced.")
            return 0

        from coreyard.sink.shopify_write import ShopifyPublisher

        publisher = ShopifyPublisher(store=settings.store, status=args.status,
                                     donor_files=state)
        if todo:
            from coreyard.yms.db import connect
            from coreyard.yms.interchange import InterchangeResolver

            print(f"Resolving fitment for {len(todo)} part(s) ...")
            with connect() as conn:
                InterchangeResolver(conn).attach(todo)
                _enrich(conn, todo)

        # A part whose photos moved needs its media rebuilt even when the fingerprint moved
        # for an unrelated reason, so both signals are unioned.
        needs_photos = _photo_refreshes(diff, args, todo_keys, todo) | changes.photo_changed
        revived: set[str] = set()
        published_ok: set[str] = set()
        checkpointed: set[str] = set()
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
            # Bank progress here too. A delta run is normally a handful of parts, but it is
            # scheduled on a short timeout, and a window it cannot finish inside that
            # timeout would otherwise be republished in full on every tick forever — which
            # is what happens after a long full sync has held the shared lock for an hour.
            # The cursor still only advances at the end, so nothing is skipped.
            if len(published_ok) - len(checkpointed) >= CHECKPOINT_EVERY:
                banked = fp_subset(fingerprints, published_ok - checkpointed)
                state.update(banked)
                state.record_channel(CHANNEL, banked)
                checkpointed |= published_ok
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
        banked = fp_subset(fingerprints, published_ok)
        state.update(banked)
        state.record_channel(CHANNEL, banked)
        state.forget(retired)
        state.forget_channel(CHANNEL, retired)
        hold = _cursor_hold_reason(changes)
        if hold:
            print(f"Cursor NOT advanced: {hold}.")
        else:
            state.set_cursor(CURSOR_NAME, changes.cursor.isoformat())
            print(f"Cursor advanced to {changes.cursor.isoformat()}.")

        _refresh_source_inventory_count(publisher)
        _summarise(diff, published_ok, revived, retired, todo, args)
    return 0


def _cursor_hold_reason(changes) -> str:
    """Why this delta must not advance its cursor, or "" if it may.

    The delta query pages on R# (``ORDER BY {identity}``), not on ``modified_at``. So a read
    that hit the row cap saw an arbitrary slice of what changed, not the oldest part of it,
    and a row past the cap can carry any timestamp at all. Advancing the cursor over rows
    that were never read puts them permanently behind it: the next run's
    ``WHERE modified_at > cursor`` no longer selects them, and only a full sync would ever
    find them again. Retirement is suppressed on a truncated read for the same reason —
    what the run did not see, it cannot reason about.
    """
    if changes.truncated:
        return "the read was truncated, so rows that changed in this window are still unread"
    if not changes.cursor:
        return "the read reported no cursor"
    return ""


def _refresh_source_inventory_count(publisher) -> Optional[int]:
    """Refresh the homepage's exact source-inventory count after an API sync.

    This ignores the storefront-only image gate on purpose: Shopify supplies its own
    live listed count, while this number answers how many in-stock parts the source database holds.
    The identifier-only query is the cheap reconciliation path and is suitable for delta
    runs. A counter failure is reported but does not invalidate product work already done.
    """
    from coreyard.yms.inventory import source_inventory_count

    try:
        count = source_inventory_count(images_only=False)
        publisher.publish_source_inventory_count(count)
    except Exception as exc:  # noqa: BLE001 - best-effort external status update
        print(f"  (could not refresh storefront inventory count: {exc})")
        return None
    print(f"Storefront inventory count refreshed: {count:,} in-stock part(s).")
    return count


def _partial_view(args) -> str:
    """Why this run saw only part of the yard, or "" if it saw all of it.

    Absence is the only evidence a full sync has that a part sold, so a run that deliberately
    looked at a slice must never reason from it. ``--limit`` has always blocked retirement
    for this reason; ``--r-number`` is the same argument, and a scope other than inventory
    is a third: a photo run has no opinion about whether a part is still in the yard.
    """
    if getattr(args, "r_numbers", None):
        return "--r-number selected specific parts"
    if args.limit:
        return "--limit means this run only saw part of the yard"
    return ""


def _selected(diff, args) -> set[str]:
    """Which parts this run publishes: everything that moved, or one scope's share of it."""
    scope = getattr(args, "scope", None)
    if scope in (None, "delta"):
        return set(diff.added) | set(diff.changed)
    return set(diff.changed_in(scope))


def _photo_refreshes(diff, args, selected: set[str], parts=()) -> set[str]:
    """Media work a scoped run owns.

    An inventory or catalogue run may touch a part whose photos also moved, but that does
    not broaden the requested scope. Leaving the media fingerprint untouched lets the next
    ``sync photos`` or full sync perform that work; rebuilding it here makes a copy-only
    run fail on an unrelated stale-media problem and defeats the point of scopes.
    """
    if getattr(args, "scope", None) in {"inventory", "catalog"}:
        return set()
    # Bulk-created donor listings can already have media while lacking sync state.
    # Establish their complete shared photo set before recording its fingerprint;
    # otherwise their old limited set would be recorded as current forever.
    added = set(diff.added)
    donor_baselines = {p.uid() for p in parts
                       if p.uid() in added and p.uses_donor_photos}
    return (set(diff.image_changed) | donor_baselines) & selected


def _committable(current, images, previous, previous_images, diff, published_ok):
    """The snapshot a full run may commit, and the photo manifests to go with it.

    A full run commits everything it extracted, which is right for the parts it published
    and wrong for the parts it did not. A part the diff called added-or-changed that never
    landed would otherwise be recorded at its *new* fingerprint, so the next run would find
    it unchanged and never retry it — the change would be lost silently and permanently,
    which is the one failure a sync must not have.

    The scoped and delta paths already got this right by recording only what landed
    (`state.update(subset(...))`). This is the same rule for the path that commits
    wholesale: an outstanding part keeps the version the storefront actually has, and a
    part that has never published stays absent so it keeps reading as new.
    """
    snapshot, manifests = dict(current), dict(images or {})
    outstanding = (set(diff.added) | set(diff.changed)) - set(published_ok)
    for key in outstanding:
        if key in previous:
            snapshot[key] = previous[key]
            if key in (previous_images or {}):
                manifests[key] = previous_images[key]
            else:
                manifests.pop(key, None)
        else:
            snapshot.pop(key, None)
            manifests.pop(key, None)
    return snapshot, manifests, outstanding


def _published_fingerprints(fingerprints, keys, state, args, diff):
    """The state a scoped publish actually established.

    ``productSet`` sends the whole non-media product, so inventory and catalogue
    projections can both advance. Media is a separate mutation: when those scopes leave a
    changed photo set alone, preserve its old manifest fingerprint so ``sync photos`` still
    sees and performs the outstanding work.
    """
    bundle = fp_subset(fingerprints, keys)
    if getattr(args, "scope", None) not in {"inventory", "catalog"}:
        return bundle
    previous = state.load_images()
    added = set(diff.added)
    for key in list(bundle.images):
        if key not in added and key in previous:
            bundle.images[key] = previous[key]
    return bundle


def cmd_sync(args) -> int:
    from coreyard.yms.inventory import fetch_parts, is_configured, photos_required

    if not is_configured():
        print(
            "No schema mapping yet — CoreYard ships none, because the table and column\n"
            "names belong to your yard system's vendor, not to this project.\n\n"
            f"  cp {bundled('schema.example.json')} \\\n     {DATA_ROOT / 'schema.json'}\n"
            f"  {cli_name()} schema                      # lists your tables/columns\n\n"
            "then replace every name ending in _TABLE or _COLUMN with your own.\n"
            "See docs/SETUP.md, 'Map your database'.",
            file=sys.stderr,
        )
        return 2

    settings = load_settings()
    scope = getattr(args, "scope", None)
    partial = _partial_view(args)

    # Taken before the extract, not after: rows that move while a long full run is reading
    # are still ahead of this mark, so the next delta run picks them up instead of assuming
    # the full run must already have seen them.
    baseline = _delta_baseline() if not partial else None

    photos = _make_resolver(args.image_base_url, scan_images=not args.no_image_scan)
    resolver = photos.resolve
    print(f"Fetching parts (limit={args.limit or 'none'}) ...")
    parts = fetch_parts(limit=args.limit)
    if getattr(args, "r_numbers", None):
        wanted = {str(r).strip() for r in args.r_numbers}
        parts = [p for p in parts if p.uid() in wanted]
        missing = sorted(wanted - {p.uid() for p in parts})
        print(f"  {len(parts)} of {len(wanted)} requested part(s) are listable"
              + (f"; not listable: {missing}" if missing else ""))
    print(f"  {len(parts)} listable part(s)"
          f"{' (photos required)' if photos_required() else ''}")

    # Incremental diff
    fingerprints = fingerprints_all(parts, resolver, settings.store, stamps=photos.stamps)
    current, image_fps = fingerprints.content, fingerprints.images
    with SyncState(STATE_DB) as state:
        # A partial view cannot tell "gone" from "not looked at", so it never asks.
        diff = state.diff(fingerprints, detect_removals=not partial)
        print("Diff vs last run:", diff.summary())
        if diff.scope_changed:
            line = "  by scope: " + "  ".join(
                f"{name}={len(diff.changed_in(name))}"
                for name in ("inventory", "catalog", "photos"))
            if diff.unattributed:
                # Otherwise three zeroes beside a four-figure `changed` reads as "nothing
                # to do" rather than "nothing can say which scope yet".
                line += (f"   ({len(diff.unattributed)} changed before the scope columns "
                         f"existed, so every scope claims them)")
            print(line)

        publisher = None
        if args.sink == "api":
            from coreyard.sink.shopify_write import ShopifyPublisher

            publisher = ShopifyPublisher(store=settings.store, status=args.status,
                                     donor_files=state)
            if args.reconcile:
                # Ask the store what it actually has, rather than trusting the state file.
                # A fresh state file knows about nothing, and the bulk path keeps its own
                # progress log, so parts sold before this run existed would otherwise stay
                # on sale forever with no record that they were ever published.
                print("Reconciling against the live store ...")
                published = publisher.published_r_numbers()
                diff.removed = [] if partial else sorted(published - set(current))
                print(f"  {len(published)} published, {len(diff.removed)} no longer listable")

        retire, why = _retirement_plan(diff, args)
        if why:
            print(why)

        retired: set[str] = set()
        revived: set[str] = set()
        todo: list = []
        published_ok: set[str] = set()
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
            selected = _selected(diff, args)
            upserts = 0 if args.retire_only else len(selected)
            photo_refreshes = _photo_refreshes(diff, args, selected, parts)
            print(f"Dry run: would upsert {upserts} product(s) "
                  f"({0 if args.retire_only else len(photo_refreshes)} "
                  f"photo refresh), "
                  f"revive {0 if args.retire_only else len(revivals)}, "
                  f"and consider {len(retire)} for retirement.")
            if retire:
                # The plan cannot tell a DRAFT from an ACTIVE without asking per product,
                # and retirement skips drafts: they are already invisible, so archiving one
                # only destroys the difference between "not ready" and "gone". So this
                # figure is an upper bound, and saying "would retire 912" overstates it.
                print("  (that is an upper bound — already-draft products are left alone)")
        else:  # api
            selected = _selected(diff, args)
            todo = [p for p in parts if p.uid() in selected]
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

            # Media is torn down and re-uploaded only for parts whose photo set actually
            # moved, and only among the ones this run is touching.
            needs_photos = _photo_refreshes(diff, args, selected, todo)
            if args.retire_only:
                print(f"Retire-only: skipping {len(todo)} upsert(s).")
                todo = []
            print(f"Upserting {len(todo)} new/changed products to Shopify "
                  f"({len(needs_photos & {p.uid() for p in todo})} with changed photos) ...")
            checkpointed: set[str] = set()
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
                # Bank the work so far. A run stopped by its timeout keeps everything it
                # published instead of starting the same backlog again next hour.
                if not args.dry_run and len(published_ok) - len(checkpointed) >= CHECKPOINT_EVERY:
                    banked = _published_fingerprints(
                        fingerprints, published_ok - checkpointed, state, args, diff
                    )
                    state.update(banked)
                    state.record_channel(CHANNEL, banked)
                    checkpointed |= published_ok
            if published_ok - checkpointed and not args.dry_run:
                banked = _published_fingerprints(
                    fingerprints, published_ok - checkpointed, state, args, diff
                )
                state.update(banked)
                state.record_channel(CHANNEL, banked)
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
        elif args.dry_run:
            print("Dry run: state NOT committed.")
        elif partial or scope:
            # This run either saw a slice of the yard or was allowed to act on only part of
            # what moved. Committing would record every part it *declined* to publish as
            # up to date, and the change it skipped would never be published by anything.
            # So record exactly what landed and leave the rest of the snapshot alone.
            banked = _published_fingerprints(
                fingerprints, published_ok, state, args, diff
            )
            state.update(banked)
            state.record_channel(CHANNEL, banked)
            state.forget(retired)
            state.forget_channel(CHANNEL, retired)
            print(f"State updated for {len(published_ok)} published part(s); "
                  f"the rest of the snapshot is untouched.")
        else:
            # Anything we believed was published but did not retire is carried forward at
            # its old fingerprint. Dropping it here would silently forget a part that is
            # still live on the storefront with stock it no longer has.
            previous = state.load()
            snapshot, manifests, outstanding = _committable(
                current, image_fps, previous, state.load_images(), diff, published_ok
            )
            carried = {r: fp for r, fp in previous.items()
                       if r in set(diff.removed) - retired}
            snapshot.update(carried)
            state.commit(snapshot, manifests)
            # The scope columns follow the same rule the canonical map just did: only the
            # parts this run actually rendered *and published* get fresh values.
            state.update(fp_subset(fingerprints, set(current) - outstanding))
            state.record_channel(CHANNEL, fp_subset(fingerprints, published_ok))
            state.forget_channel(CHANNEL, retired)
            if outstanding:
                print(f"{len(outstanding)} part(s) did not publish and stay pending.")
            print(f"State committed{f' ({len(carried)} unretired carried forward)' if carried else ''}.")
            if baseline is not None:
                # Only a full run may set this: it is the only one that has just reconciled
                # the whole yard, which is what makes "everything before this mark is already
                # published" true.
                from coreyard.yms.delta import CURSOR_NAME

                state.set_cursor(CURSOR_NAME, baseline.isoformat())

        if publisher is not None and not args.dry_run:
            _refresh_source_inventory_count(publisher)
        _summarise(diff, published_ok, revived, retired, todo, args)
    return 0


def _summarise(diff, published_ok, revived, retired, todo, args) -> None:
    """One block an operator can read at a glance, and the counts ``status`` remembers.

    Every path records its counts, including the catch-up run: without them ``status`` could
    say a delta run finished but not what it did, which is most of the question.

    The block is only *printed* when something happened. A quiet delta tick every five
    minutes is 288 a day, and eight lines of zeroes each time buries the ticks that matter.
    """
    created = len(set(diff.added) & published_ok)
    counts = {
        "created": created,
        "updated": len(published_ok) - created,
        "revived": len(revived),
        "retired": len(retired),
        "unchanged": len(diff.unchanged),
        "failed": len(todo) - len(published_ok),
    }
    ops.count(scope=getattr(args, "scope", None) or "full", dry_run=bool(args.dry_run),
              **counts)
    if args.dry_run:
        return
    acted = {k: v for k, v in counts.items() if k != "unchanged" and v}
    if not acted:
        print(f"Nothing to publish ({counts['unchanged']:,} unchanged).")
        return
    print("\nSync complete.\n")
    for label, value in counts.items():
        print(f"  {label.replace('_', ' ').capitalize():<14}{value:>10,}")
    if counts["failed"]:
        print(f"\n  {counts['failed']} part(s) failed to publish and keep their previous "
              f"state, so the next run retries them.")


SCOPE_HELP = {
    "delta": "publish only what changed since the stored cursor (cheap catch-up)",
    "inventory": "availability only: new, revived, repriced, restocked, retired",
    "photos": "media only: parts whose photo set moved on the share",
    "catalog": "copy only: parts whose rendered text, tags, weight or metafields moved",
}


def _sync_flags(p: argparse.ArgumentParser,
                default_sink: str = "api") -> argparse.ArgumentParser:
    """The flags every sync scope accepts.

    Applied to the ``sync`` parser and to each scope subparser, so ``coreyard sync
    --dry-run`` and ``coreyard sync photos --dry-run`` mean the same thing.

    ``default_sink`` differs between the two entry points on purpose. ``coreyard sync`` is
    the operator's convergence command and publishes; asking it to also be told *where* to
    publish, every time, is how a scheduled job ends up quietly writing a CSV nobody reads.
    The legacy ``python -m coreyard.run_sync`` keeps its original CSV default, because this
    installation's crontab names it and a default is not worth a surprise.
    """
    p.add_argument("--check", action="store_true", help="test connectivity and exit")
    p.add_argument("--delta", action="store_true",
                   help="publish only what changed since the last run's cursor and retire "
                        "what left scope (cheap catch-up; needs a prior full sync)")
    p.add_argument("--sink", choices=["csv", "api"], default=default_sink,
                   help=f"where to publish (default {default_sink})")
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
    deep = p.add_mutually_exclusive_group()
    deep.add_argument("--deep", dest="reconcile", action="store_true",
                      help="ask the live store what it holds instead of trusting the state "
                           "file. Slower (it pages the whole catalogue), so it belongs on a "
                           "daily run rather than an hourly one — but it is the only thing "
                           "that sees drift the snapshot cannot.")
    # The name this flag shipped under. The crontab and the README both used it, and a
    # scheduled job that silently stops reconciling looks exactly like one that works.
    deep.add_argument("--reconcile", dest="reconcile", action="store_true",
                      help=argparse.SUPPRESS)
    p.add_argument("--r-number", dest="r_numbers", metavar="R#", action="append",
                   default=None,
                   help="act on these parts only (repeatable). Never retires anything: a "
                        "part outside the selection is unexamined, not missing.")
    p.add_argument("--force-retire", action="store_true",
                   help="retire sold parts even if they exceed the safety fraction")
    p.add_argument("--max-retire-fraction", type=float, default=0.10,
                   help="refuse to retire more than this share of the catalogue (default 0.10)")
    p.add_argument("--dry-run", action="store_true",
                   help="report the diff only: no Shopify writes, no state commit")
    return p


def add_arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with the sync flags and the scope subcommands.

    Shared by ``coreyard sync`` and the legacy ``python -m coreyard.run_sync`` entry point
    that this installation's crontab still names, so the two can never drift into
    disagreeing about what a flag means.

    The scopes are selectors over one plan, not separate engines. There is one extract, one
    renderer and one diff; a scope only decides which of the parts that moved this run is
    allowed to act on. Building them as independent commands would have meant a second
    answer to "what changed", which is the bug `transform/render.py` exists to prevent.
    """
    _sync_flags(p)
    p.set_defaults(func=dispatch, scope=None)
    sub = p.add_subparsers(dest="scope", metavar="<scope>")
    for name, help_text in SCOPE_HELP.items():
        scoped = sub.add_parser(name, help=help_text, description=help_text)
        _sync_flags(scoped)
        scoped.set_defaults(func=dispatch, scope=name)
    return p


def dispatch(args) -> int:
    """Route parsed sync arguments to the right run. The one place that decision is made."""
    scope = getattr(args, "scope", None)
    if scope == "delta":
        args.delta = True
    # A scope subcommand is an operator command against the live store; the bare `--sink`
    # default only exists because the CSV preview predates the API sink.
    if scope and args.sink != "api":
        args.sink = "api"
    try:
        if getattr(args, "check", False):
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


def main(argv: list[str] | None = None) -> int:
    """The legacy ``python -m coreyard.run_sync`` entry point.

    Kept working, and kept behaving exactly as it did, because this installation's crontab
    invokes it by module path — and a scheduled job that changes behaviour silently is the
    failure mode that costs a day before anyone notices.
    """
    p = argparse.ArgumentParser(prog="coreyard.run_sync",
                                description="source database -> Shopify sync")
    _sync_flags(p, default_sink="csv")
    p.set_defaults(func=dispatch, scope=None)
    return dispatch(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
