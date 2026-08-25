"""Backfill alt text onto product photos that were uploaded without it.

Photos published before alt text was generated carry ``alt: ""`` — invisible to image
search and unreadable to a screen reader. This walks the store, works out the alt each
photo should have, and writes it with ``fileUpdate``.

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
    nodes{ id handle media(first:10){ nodes{ ... on MediaImage { id alt } } } }
  } }"""

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
          limit: int | None = None) -> tuple[dict[str, list[tuple[str, int]]], dict[str, int]]:
    """Map R# -> [(media_id, photo_index)] for photos still needing alt text.

    Stops as soon as ``limit`` products have been collected — otherwise a small run still
    pays for a full walk of the catalogue.
    """
    todo: dict[str, list[tuple[str, int]]] = {}
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
        wanted: list[tuple[str, int]] = []
        for index, media in enumerate(node["media"]["nodes"], start=1):
            if not media.get("id"):
                continue
            stats["photos"] += 1
            if media.get("alt") and not overwrite:
                stats["already_set"] += 1
                continue
            wanted.append((media["id"], index))
        if wanted:
            todo[r_number] = wanted
            if limit and len(todo) >= limit:
                break
    return todo, stats


def _parts_for(r_numbers: set[str]) -> dict[str, Part]:
    """Load just the parts we need, with interchange fitment resolved for their titles."""
    from coreyard.yms.db import connect
    from coreyard.yms.interchange import InterchangeResolver
    from coreyard.yms.inventory import fetch_parts

    with connect() as conn:
        parts = [p for p in fetch_parts(images_only=True) if p.r_number in r_numbers]
        InterchangeResolver(conn).attach(parts)
    return {p.r_number: p for p in parts}


def _record(log, payload: dict[str, Any]) -> None:
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    log.write(json.dumps(payload, sort_keys=True) + "\n")
    log.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("--overwrite", action="store_true", help="replace alt text that is already set")
    ap.add_argument("--limit", type=int, default=None, help="max products to update")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="stop scanning after N pages of 50 products (for a quick sample)")
    args = ap.parse_args(argv)

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

    print(f"Loading {len(todo)} parts from the source database (resolving fitment for titles) ...",
          flush=True)
    parts = _parts_for(set(todo))
    missing = sorted(set(todo) - set(parts))
    if missing:
        print(f"  {len(missing)} product(s) have no matching listable part; skipping "
              f"(e.g. {missing[:5]})")

    updates: list[dict[str, str]] = []
    owner: list[str] = []
    for r_number, media in todo.items():
        part = parts.get(r_number)
        if part is None:
            continue
        for media_id, index in media:
            updates.append({"id": media_id, "alt": seo.image_alt(part, index, store)})
            owner.append(r_number)

    if not updates:
        print("No updatable photos matched a live part.")
        return 0

    if args.dry_run:
        print(f"\nDRY RUN — would update {len(updates)} photo(s). Samples:")
        for u, r_number in list(zip(updates, owner))[:8]:
            print(f"  R#{r_number:<8} {u['alt'][:78]}")
        return 0

    args.log.parent.mkdir(parents=True, exist_ok=True)
    ok = failed = 0
    with args.log.open("a", encoding="utf-8") as log:
        for start in range(0, len(updates), BATCH):
            chunk = updates[start:start + BATCH]
            owners = sorted(set(owner[start:start + BATCH]))
            try:
                result = client.graphql(_FILE_UPDATE, {"files": chunk})["fileUpdate"]
                errors = result["userErrors"]
            except Exception as exc:                      # noqa: BLE001
                errors = [{"message": str(exc)}]
            if errors:
                failed += len(chunk)
                for r_number in owners:
                    _record(log, {"status": "error", "r_number": r_number,
                                  "error": str(errors)})
                print(f"  [{start + len(chunk)}/{len(updates)}] ERROR {errors}", flush=True)
            else:
                ok += len(chunk)
                for r_number in owners:
                    _record(log, {"status": "ok", "r_number": r_number})
                print(f"  [{start + len(chunk)}/{len(updates)}] ok", flush=True)

    print(f"\nFinished: photos updated={ok} failed={failed} log={args.log}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
