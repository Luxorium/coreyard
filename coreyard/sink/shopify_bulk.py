"""Resumable, bounded-concurrency bulk publisher for image-backed parts."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coreyard.config import load_store, out_dir
from coreyard.models import Part
from coreyard.state import DEFAULT_STATE_DB, SyncState, fingerprints_all
from coreyard.state import subset as fp_subset
from coreyard.yms.db import connect
from coreyard.yms.interchange import InterchangeResolver
from coreyard.yms.inventory import fetch_parts, photos_required
from coreyard.sink.shopify_write import ShopifyPublisher

DEFAULT_LOG = out_dir() / "shopify_bulk_results.jsonl"
_thread_local = threading.local()


def _completed_r_numbers(path: Path) -> set[str]:
    latest: dict[str, str] = {}
    if not path.exists():
        return set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            latest[str(entry["r_number"])] = entry["status"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return {r_number for r_number, status in latest.items() if status == "ok"}


def _publisher(status: str, require_images: bool = True) -> ShopifyPublisher:
    publisher = getattr(_thread_local, "publisher", None)
    if publisher is None:
        # One state connection per worker, not one shared: sqlite3 binds a connection to
        # the thread that opened it. It exists only to remember which donor frames are
        # already Shopify files — without it every part off a donor stages the same
        # photographs again, and roughly seven parts come off each donor.
        state = SyncState(DEFAULT_STATE_DB)
        _thread_local.state = state
        publisher = ShopifyPublisher(status=status, require_images=require_images,
                                     donor_files=state)
        _thread_local.publisher = publisher
    return publisher


def _publish_with_retry(part: Part, status: str, max_attempts: int,
                        require_images: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            product_id, images_added = _publisher(status, require_images).publish(part)
            return {
                "status": "ok",
                "r_number": part.r_number,
                "product_id": product_id,
                "images_added": images_added,
                "attempts": attempt,
                "seconds": round(time.monotonic() - started, 2),
            }
        except Exception as exc:
            last_error = exc
            if attempt < max_attempts:
                time.sleep(min(2 ** attempt, 30))
    return {
        "status": "error",
        "r_number": part.r_number,
        "error": str(last_error),
        "attempts": max_attempts,
        "seconds": round(time.monotonic() - started, 2),
    }


def _write_result(log_file, result: dict[str, Any]) -> None:
    result["recorded_at"] = datetime.now(timezone.utc).isoformat()
    log_file.write(json.dumps(result, sort_keys=True) + "\n")
    log_file.flush()


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with the bulk-publish flags."""
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--status", choices=("DRAFT",), default="DRAFT")
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--no-resume", action="store_true")
    images = parser.add_mutually_exclusive_group()
    images.add_argument("--images-only", dest="images_only", action="store_true",
                        default=None,
                        help="only publish parts the mapping says have photos "
                             "(default: this installation's STORE_REQUIRE_IMAGES policy)")
    images.add_argument("--all-parts", dest="images_only", action="store_false",
                        help="publish every listable part, photographed or not")
    parser.set_defaults(func=run)
    return parser


def run(args) -> int:
    if args.workers < 1:
        print("error: --workers must be positive", file=sys.stderr)
        return 2

    # The same definition of "listable" the sync and reconciliation use, so a bulk load
    # cannot create products that the next reconcile immediately archives.
    parts = fetch_parts(limit=args.limit, images_only=args.images_only)
    require_images = photos_required() if args.images_only is None else args.images_only
    already_done = set() if args.no_resume else _completed_r_numbers(args.log)
    selected = [part for part in parts if part.r_number not in already_done]
    print(
        f"Eligible={len(parts)} already_completed={len(parts) - len(selected)} "
        f"remaining={len(selected)} workers={args.workers}",
        flush=True,
    )
    if not selected:
        return 0

    args.log.parent.mkdir(parents=True, exist_ok=True)
    successes = 0
    failures = 0
    processed = 0
    pending: dict[Future, Part] = {}

    # Which photographs a part publishes with — its own, or its donor vehicle's — is
    # decided in exactly one place, and this is the third caller of it after the full and
    # delta sync paths. Resolving here is what tells the publisher a part fell back to its
    # donor; without it an unphotographed part reaches ``_staged_files`` with nothing to
    # stage and, under ``require_images``, fails outright. It also lists each folder once
    # for the whole run rather than once per part.
    from coreyard.run_sync import CHANNEL, CHECKPOINT_EVERY, _make_resolver

    photos = _make_resolver(None, scan_images=True)
    store = load_store()

    # One fingerprint pass over the whole selection, before the publish path touches these
    # parts — which is exactly when the sync takes its own. It has to be the same moment:
    # `fitment_for` below attaches interchange copy to the part in place, and a fingerprint
    # taken after that describes a part the sync never fingerprints, so every part with
    # fitment would read as changed on the next run and be published all over again. Taking
    # it here also renders each part once rather than twice.
    prints = fingerprints_all(selected, photos.resolve, store, stamps=photos.stamps)
    unbanked: list[str] = []

    def bank(state, force: bool = False) -> None:
        """Tell the snapshot what this run published, in the sync's own terms.

        Without this the two publish paths share no state at all: a bulk load of twenty-odd
        thousand products left the snapshot empty, so the very next `coreyard sync` saw every
        one of them as new and published the whole catalogue again through the slow path.
        The work was not lost, but nothing this run did was *known*, which is the same thing
        from the next run's point of view.

        Only parts that actually published are recorded, and at the fingerprint taken before
        this run touched them, which is the one the sync takes. Anything else records a
        version no sync will ever compute, and every part carrying it reads as changed
        forever after.

        Recording is best-effort on purpose. A long bulk load that publishes correctly must
        not die because the state database was busy; the cost of failing here is that the
        next sync republishes those parts, which is exactly what happened before.
        """
        if not unbanked or (len(unbanked) < CHECKPOINT_EVERY and not force):
            return
        batch, unbanked[:] = list(unbanked), []
        try:
            banked = fp_subset(prints, batch)
            state.update(banked)
            state.record_channel(CHANNEL, banked)
        except Exception as exc:      # noqa: BLE001 - see the docstring
            print(f"  (could not record {len(batch)} part(s) in the sync state: {exc})",
                  flush=True)

    def record_completed(log_file, state, block: bool) -> None:
        nonlocal successes, failures, processed
        if not pending:
            return
        done, _ = wait(
            pending,
            return_when=FIRST_COMPLETED,
            timeout=None if block else 0,
        )
        # A worker that died hard — KeyboardInterrupt, SystemExit — must not take the
        # results already in this batch with it. They describe products Shopify has taken,
        # and dropping them here means the log never learns about them and neither does the
        # snapshot. Finish accounting for what is in hand, then let it out.
        interrupted: BaseException | None = None
        for future in done:
            part = pending.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                result = {"status": "error", "r_number": part.r_number, "error": str(exc)}
            except BaseException as exc:      # noqa: BLE001 - re-raised below
                interrupted = exc
                continue
            _write_result(log_file, result)
            processed += 1
            if result["status"] == "ok":
                successes += 1
                unbanked.append(part.uid())
                bank(state)
                marker = "OK"
                detail = f'+{result["images_added"]} images'
            else:
                failures += 1
                marker = "ERROR"
                detail = result.get("error", "unknown error")
            print(
                f"[{processed}/{len(selected)}] {marker} R#{part.r_number} {detail} "
                f'({result.get("seconds", 0)}s, attempt {result.get("attempts", 1)})',
                flush=True,
            )
        if interrupted is not None:
            raise interrupted

    with args.log.open("a", encoding="utf-8") as log_file, \
            SyncState(DEFAULT_STATE_DB) as state:
        try:
            with connect() as conn, ThreadPoolExecutor(max_workers=args.workers) as pool:
                resolver = InterchangeResolver(conn)
                for part in selected:
                    part.fitment = resolver.fitment_for(part)
                    photos.resolve(part)
                    future = pool.submit(_publish_with_retry, part, args.status,
                                         args.max_attempts, require_images)
                    pending[future] = part
                    while len(pending) >= args.workers * 3:
                        record_completed(log_file, state, block=True)
                while pending:
                    record_completed(log_file, state, block=True)
        finally:
            # However this run is left — drained, interrupted, or killed at the keyboard —
            # every part in here is one Shopify has already taken. The same lesson as the
            # publish loop's own checkpoint: banking only at the end means a run that does
            # not reach the end banks nothing.
            bank(state, force=True)

    print(f"Finished: successes={successes} failures={failures} log={args.log}", flush=True)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coreyard bulk", description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
