"""Backfill or repair alt text on product photos.

Photos published before alt text was generated carry ``alt: ""`` — invisible to image
search and unreadable to a screen reader. This walks the store, works out the alt each
photo should have, and writes it with ``fileUpdate``.

``--overwrite`` also repairs alt text that is set but wrong. A donor-vehicle photograph is
one global file shared by every part pulled from that car, so whichever part was published
first named the file after itself: a blower motor's listing carried "Used OEM AC Air
Conditioning Compressor from 2019 Chevrolet Malibu" because the compressor off the same
Malibu was uploaded first. The generated alt says "Donor vehicle ..." for those, which is
one description the shared file can honestly hold. Photos already reading as generated are
left untouched, so a repair run writes only what is actually wrong.

Note ``productCreateMedia``/``productUpdateMedia`` no longer exist in current Admin API
versions; ``fileUpdate`` is the supported way to change a media file's alt, and it takes a
batch, so updates go up 25 at a time rather than one call per photo.

    python -m coreyard.sink.backfill_alt --dry-run     # report only, no writes
    python -m coreyard.sink.backfill_alt               # apply
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from coreyard.config import REPO_ROOT, StoreProfile, load_store
from coreyard.models import Part
from coreyard.sink.shopify_api import ShopifyClient
from coreyard.transform import seo
from coreyard.transform.render import r_number_from_handle

DEFAULT_LOG = REPO_ROOT / "out" / "alt_backfill_results.jsonl"
BATCH = 25

_PAGE = """query($cursor:String){
  products(first:50, after:$cursor){
    pageInfo{ hasNextPage endCursor }
    nodes{ id handle media(first:10){
      pageInfo{ hasNextPage endCursor }
      nodes{ ... on MediaImage { id alt } }
    } }
  } }"""

_MEDIA_PAGE = """query($id:ID!,$cursor:String){
  product(id:$id){ media(first:100, after:$cursor){
    pageInfo{ hasNextPage endCursor }
    nodes{ ... on MediaImage { id alt } }
  } } }"""

_FILE_UPDATE = """mutation($files:[FileUpdateInput!]!){
  fileUpdate(files:$files){ files{ id } userErrors{ field message } } }"""


def _iter_products(client: ShopifyClient, max_pages: int | None = None) -> Iterator[dict[str, Any]]:
    cursor = None
    pages = 0
    while True:
        pages += 1
        page = client.graphql(_PAGE, {"cursor": cursor})["products"]
        for node in page["nodes"]:
            yield node
        if not page["pageInfo"]["hasNextPage"] or (max_pages and pages >= max_pages):
            return
        cursor = page["pageInfo"]["endCursor"]


def _iter_media(client: ShopifyClient, product: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield every image attached to a product, including media after the first ten."""
    media = product.get("media") or {}
    yield from media.get("nodes") or []
    page_info = media.get("pageInfo") or {}
    while page_info.get("hasNextPage"):
        result = client.graphql(
            _MEDIA_PAGE,
            {"id": product["id"], "cursor": page_info.get("endCursor")},
        ).get("product")
        # The product can be deleted between the catalogue page and this overflow query.
        # Its first page was still valid; there is simply nothing left to update now.
        if not result:
            return
        media = result["media"]
        yield from media.get("nodes") or []
        page_info = media.get("pageInfo") or {}


def _completed(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("status") == "ok":
            done.add(str(entry.get("r_number")))
    return done


def _plan(client: ShopifyClient, store: StoreProfile, done: set[str], overwrite: bool,
          max_pages: int | None = None,
          limit: int | None = None) -> tuple[dict[str, list[tuple[str, int, str]]], dict[str, int]]:
    """Map R# -> [(media_id, photo_index, current_alt)] for photos needing alt text.

    Stops as soon as ``limit`` products have been collected — otherwise a small run still
    pays for a full walk of the catalogue.

    The alt already on the file is carried along so that ``--overwrite`` can tell a photo
    whose description is wrong from one that merely already has a description. Without it a
    repair run rewrote every photo in the catalogue to the string it already held.
    """
    todo: dict[str, list[tuple[str, int, str]]] = {}
    stats = {"products": 0, "ours": 0, "skipped_done": 0, "photos": 0, "already_set": 0}
    for node in _iter_products(client, max_pages):
        stats["products"] += 1
        # The same handle rules the publisher uses, so a slugged prefix cannot make this
        # walk quietly skip every product.
        r_number = r_number_from_handle(node["handle"], store)
        if r_number is None:
            continue
        stats["ours"] += 1
        if r_number in done:
            stats["skipped_done"] += 1
            continue
        wanted: list[tuple[str, int, str]] = []
        for index, media in enumerate(_iter_media(client, node), start=1):
            if not media.get("id"):
                continue
            stats["photos"] += 1
            if media.get("alt"):
                stats["already_set"] += 1
                if not overwrite:
                    continue
            wanted.append((media["id"], index, media.get("alt") or ""))
        if wanted:
            todo[r_number] = wanted
            if limit and len(todo) >= limit:
                break
    return todo, stats


def _parts_for(r_numbers: set[str]) -> dict[str, Part]:
    """Load just the parts whose photos need object-focused alt text.

    Alt text describes the photographed part and its donor vehicle; it deliberately does
    not include interchange fitment. Resolving tens of thousands of fitment keys here would
    add many minutes of source-database traffic without changing a single generated alt.
    """
    from coreyard.run_sync import _make_resolver
    from coreyard.yms.inventory import fetch_parts_by_r_number

    # A live product can now be sold, archived, or using donor photographs;
    # none of those states makes its existing image alt safe to misidentify. Targeted
    # lookups intentionally bypass the current listable gates and stay below SQL's practical
    # IN-list limits.
    found: dict[str, Part] = {}
    wanted = sorted(r_numbers)
    for start in range(0, len(wanted), 250):
        found.update(fetch_parts_by_r_number(wanted[start:start + 250]))

    # This is the same own-photo-versus-donor decision used by sync. Calling resolve sets
    # donor_image_key/uses_donor_photos on each Part; the returned filenames are irrelevant
    # here, but those facts decide what the photograph truthfully depicts.
    photos = _make_resolver(None)
    for part in found.values():
        photos.resolve(part)
    return found


def _updates_for(
    todo: dict[str, list[tuple[str, int, str]]],
    parts: dict[str, Part],
    store: StoreProfile,
) -> tuple[list[dict[str, str]], list[set[str]], dict[str, set[str]], int]:
    """Deduplicate global Shopify files and refuse conflicting descriptions.

    A donor image is one global file referenced by several products. Sending one update per
    product creates a last-write-wins race; one file must have one donor-vehicle alt instead.
    If supposedly shared media resolve to different facts, skip the unsafe update and make
    the collision visible rather than choosing whichever product happened to be scanned last.

    A photo whose alt already reads exactly as generated is dropped last, after the conflict
    check rather than before it: the alt on a shared file is one string, so skipping an owner
    early would hide a disagreement between two products about what that one file depicts and
    let the other owner's description win unchallenged. Everything surviving that check and
    still matching is simply already correct, and rewriting it would spend a catalogue-wide
    run of writes to store the strings that are there.
    """
    by_media: dict[str, dict[str, str]] = {}
    owners: dict[str, set[str]] = {}
    conflicts: dict[str, set[str]] = {}
    current: dict[str, str] = {}
    for r_number, media in todo.items():
        part = parts.get(r_number)
        if part is None:
            continue
        for media_id, index, existing in media:
            update = {"id": media_id, "alt": seo.image_alt(part, index, store)}
            current.setdefault(media_id, existing)
            prior = by_media.get(media_id)
            if media_id in conflicts:
                conflicts[media_id].add(r_number)
                continue
            if prior is not None and prior["alt"] != update["alt"]:
                conflicts.setdefault(media_id, set()).update(owners[media_id])
                conflicts[media_id].add(r_number)
            else:
                by_media[media_id] = update
                owners.setdefault(media_id, set()).add(r_number)

    for media_id in conflicts:
        by_media.pop(media_id, None)
        owners.pop(media_id, None)
    unchanged = 0
    for media_id in list(by_media):
        if by_media[media_id]["alt"] == current.get(media_id, ""):
            unchanged += 1
            by_media.pop(media_id)
            owners.pop(media_id, None)
    media_ids = list(by_media)
    return ([by_media[media_id] for media_id in media_ids],
            [owners[media_id] for media_id in media_ids], conflicts, unchanged)


def _record(log, payload: dict[str, Any]) -> None:
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    log.write(json.dumps(payload, sort_keys=True) + "\n")
    log.flush()


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with the alt-text backfill flags."""
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("--overwrite", action="store_true",
                    help="re-examine alt text that is already set, and correct it where it "
                         "no longer describes the photo")
    ap.add_argument("--limit", type=int, default=None, help="max products to update")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="stop scanning after N pages of 50 products (for a quick sample)")
    ap.set_defaults(func=run)
    return ap


def run(args) -> int:
    store = load_store()
    client = ShopifyClient()
    done = set() if args.no_resume or args.dry_run else _completed(args.log)

    print("Scanning the store for photos without alt text ...", flush=True)
    todo, stats = _plan(client, store, done, args.overwrite, args.max_pages, args.limit)
    print(f"  products scanned={stats['products']} ours={stats['ours']} "
          f"already_done={stats['skipped_done']}")
    print(f"  photos seen={stats['photos']} already_have_alt={stats['already_set']}")
    print(f"  products needing alt={len(todo)} photos to update="
          f"{sum(len(v) for v in todo.values())}")
    if not todo:
        print("Nothing to do.")
        return 0

    print(f"Loading {len(todo)} parts from the source database ...", flush=True)
    parts = _parts_for(set(todo))
    missing = sorted(set(todo) - set(parts))
    if missing:
        print(f"  {len(missing)} product(s) have no matching source part; skipping "
              f"(e.g. {missing[:5]})")

    updates, owners_by_update, conflicts, unchanged = _updates_for(todo, parts, store)
    conflict_products = set().union(*conflicts.values()) if conflicts else set()
    if unchanged:
        print(f"  {unchanged} photo(s) already carry exactly the generated alt; leaving them alone")
    if conflicts:
        sample = [(media_id, sorted(owners))
                  for media_id, owners in list(conflicts.items())[:5]]
        print(f"  ERROR: {len(conflicts)} shared media file(s) resolved to conflicting alt "
              f"text; skipped them (e.g. {sample})")

    if not updates:
        print("Every photo that matched a live part already reads correctly."
              if unchanged else "No updatable photos matched a live part.")
        return 1 if conflicts else 0

    if args.dry_run:
        print(f"\nDRY RUN — would update {len(updates)} photo(s). Samples:")
        for update, owners in list(zip(updates, owners_by_update))[:8]:
            label = ",".join(f"R#{r}" for r in sorted(owners))
            print(f"  {label:<12} {update['alt'][:78]}")
        return 1 if conflicts else 0

    args.log.parent.mkdir(parents=True, exist_ok=True)
    ok = failed = 0
    all_products = set().union(*owners_by_update) if owners_by_update else set()
    failed_products: set[str] = set(conflict_products)
    with args.log.open("a", encoding="utf-8") as log:
        for start in range(0, len(updates), BATCH):
            chunk = updates[start:start + BATCH]
            owners = sorted(set().union(*owners_by_update[start:start + BATCH]))
            try:
                result = client.graphql(_FILE_UPDATE, {"files": chunk})["fileUpdate"]
                errors = result["userErrors"]
            except Exception as exc:                      # noqa: BLE001
                errors = [{"message": str(exc)}]
            if errors:
                failed += len(chunk)
                failed_products.update(owners)
                for r_number in owners:
                    _record(log, {"status": "error", "r_number": r_number,
                                  "error": str(errors)})
                print(f"  [{start + len(chunk)}/{len(updates)}] ERROR {errors}", flush=True)
            else:
                ok += len(chunk)
                print(f"  [{start + len(chunk)}/{len(updates)}] ok", flush=True)

        # A product can have more photos than one API batch. Mark it resumable only after
        # every batch containing one of its photos succeeded; otherwise an early success
        # followed by a later failure would cause the retry to skip its remaining photos.
        for r_number in sorted(all_products - failed_products):
            _record(log, {"status": "ok", "r_number": r_number})

    print(f"\nFinished: photos updated={ok} failed={failed} log={args.log}")
    return 1 if failed or conflicts else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard altfix", description=__doc__)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
