"""Resumable, bounded-concurrency bulk publisher for image-backed parts."""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coreyard.config import REPO_ROOT
from coreyard.models import Part
from coreyard.yms.db import connect
from coreyard.yms.interchange import InterchangeResolver
from coreyard.yms.inventory import fetch_parts
from coreyard.sink.shopify_write import ShopifyPublisher

DEFAULT_LOG = REPO_ROOT / "out" / "shopify_bulk_results.jsonl"
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


def _publisher(status: str) -> ShopifyPublisher:
    publisher = getattr(_thread_local, "publisher", None)
    if publisher is None:
        publisher = ShopifyPublisher(status=status, require_images=True)
        _thread_local.publisher = publisher
    return publisher


def _publish_with_retry(part: Part, status: str, max_attempts: int) -> dict[str, Any]:
    started = time.monotonic()
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            product_id, images_added = _publisher(status).publish(part)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--status", choices=("DRAFT",), default="DRAFT")
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")

    parts = fetch_parts(limit=args.limit, images_only=True)
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

    def record_completed(log_file, block: bool) -> None:
        nonlocal successes, failures, processed
        if not pending:
            return
        done, _ = wait(
            pending,
            return_when=FIRST_COMPLETED,
            timeout=None if block else 0,
        )
        for future in done:
            part = pending.pop(future)
            try:
                result = future.result()
            except Exception as exc:
                result = {"status": "error", "r_number": part.r_number, "error": str(exc)}
            _write_result(log_file, result)
            processed += 1
            if result["status"] == "ok":
                successes += 1
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

    with args.log.open("a", encoding="utf-8") as log_file:
        with connect() as conn, ThreadPoolExecutor(max_workers=args.workers) as pool:
            resolver = InterchangeResolver(conn)
            for part in selected:
                part.fitment = resolver.fitment_for(part)
                future = pool.submit(_publish_with_retry, part, args.status, args.max_attempts)
                pending[future] = part
                while len(pending) >= args.workers * 3:
                    record_completed(log_file, block=True)
            while pending:
                record_completed(log_file, block=True)

    print(f"Finished: successes={successes} failures={failures} log={args.log}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
