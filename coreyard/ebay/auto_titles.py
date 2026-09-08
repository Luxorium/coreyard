"""Bounded, resumable title maintenance for the unlisted portal queue."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict
from pathlib import Path

from coreyard.config import REPO_ROOT, load_env, load_store, out_dir
from coreyard.ebay import link, titles, workflow
from coreyard.ebay.client import PortalClient
from coreyard.ebay.portal import load as load_portal
from coreyard.ebay.util import TITLE_MAX

RECHECK_SECONDS = 86400

_IDENTIFIERS = re.compile(
    r"\b(?:CLASSIC|LED|HALOGEN|HID|XENON|ADAPTIVE|TURBO|DIESEL|CVT|AWD|FWD|RWD|"
    r"2WD|4WD|4X4|DOHC|SOHC)\b|\bVIN\s+[A-Z0-9]+\b|"
    r"\b\d(?:\.\d)?\s*L\b|\b\d+\s*(?:LUG|SPOKE)\b|"
    r"\b(?:ID|CODE)\s+[A-Z0-9-]{3,}\b", re.I
)


def lost_identifiers(old, new):
    """Hold a rewrite that loses a known variant; never invent replacement wording."""
    compact = lambda text: re.sub(r"[\s-]+", "", text.upper())
    target = compact(new)
    return [match.group() for match in _IDENTIFIERS.finditer(old)
            if compact(match.group()) not in target]


def signature(row, part, revision):
    """Source facts and grid identity, before the expensive fitment lookup."""
    facts = asdict(part)
    facts.pop("fitment", None)
    payload = {"part": facts, "revision": revision, "grid": {
        key: row.get(key) for key in
        ("stock_number", "part_type", "interchange_number", "title")
    }}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def pending(rows, resolved, state, revision, now, batch_size):
    """Check unseen/changed listings first, rotating retries behind untouched work."""
    candidates = []
    for row in rows:
        key = str(row["listing_id"])
        if key not in resolved:
            continue
        stamp = signature(row, resolved[key], revision)
        old = state.get(key, {})
        if old.get("signature") == stamp and now - old.get("checked", 0) < RECHECK_SECONDS:
            continue
        candidates.append((old.get("attempted", 0), -old.get("seen", now), key, stamp))
    candidates.sort()
    return [(key, stamp) for _, _, key, stamp in candidates[:batch_size]]


def collect(client, codes, rows=1000):
    """Complete filtered grids; partial responses are refused by the portal client."""
    found = {}
    for number, code in enumerate(codes, 1):
        for row in client.iter_listings(tab="unlisted", rows=rows, part_type=code):
            found[str(row["listing_id"])] = row
        if number % 25 == 0:
            print(f"  scanned {number}/{len(codes)} types; {len(found)} listings", flush=True)
    return list(found.values())


def revision(store):
    """Renderer edits invalidate checks without coupling marketplace and Shopify titles."""
    digest = hashlib.sha256(json.dumps(asdict(store), sort_keys=True, default=str).encode())
    for name in ("transform/seo.py", "profile.py", "ebay/titles.py"):
        digest.update((REPO_ROOT / "coreyard" / name).read_bytes())
    return digest.hexdigest()


def run_batch(client, rows, catalogue, store, state, *, batch_size, cap, apply,
              attach, checkpoint, now=None, renderer_revision=""):
    """Plan, recheck, save and verify one batch; only title fields may be written."""
    now = time.time() if now is None else now
    if batch_size < 1 or cap < 1:
        raise ValueError("batch size and cap must be positive")
    workflow.check_cap(batch_size, cap, False)
    resolved, unresolved = link.resolve(rows, catalogue)
    if apply:
        for row in rows:
            state.setdefault(str(row["listing_id"]), {}).setdefault("seen", now)
    selected = pending(rows, resolved, state, renderer_revision, now, batch_size)
    print(f"{len(rows)} unlisted; {len(resolved)} matched; "
          f"{len(unresolved)} ambiguous/unmatched; checking {len(selected)}", flush=True)
    chosen = {key: resolved[key] for key, _ in selected}
    attach(list({part.uid(): part for part in chosen.values()}.values()))
    accepted, held = titles.decisions_for(rows, chosen, store, limit=TITLE_MAX)
    # Check collisions against the entire queue, including previously optimized titles.
    proposed = {item["listing_id"]: item for item in accepted}
    names = {}
    for row in rows:
        key = str(row["listing_id"])
        title = proposed.get(key, {}).get("new_title", row.get("title", ""))
        names.setdefault(title, set()).add(str(row.get("interchange_number") or ""))
    safe = []
    for item in accepted:
        if len(names[item["new_title"]]) > 1:
            held.append({**item, "reason": "title collides with another interchange group"})
        elif "truncated" in item.get("dropped", []):
            held.append({**item, "reason": "essential title text would be truncated"})
        elif lost_identifiers(item["old_title"], item["new_title"]):
            held.append({**item, "reason": "rewrite loses variant identifiers: " +
                         ", ".join(lost_identifiers(item["old_title"], item["new_title"]))})
        else:
            safe.append(item)
    plan = titles.group_for_portal(safe)
    workflow.check_cap(sum(len(item["listing_ids"]) for item in plan), cap, False)
    report = {"titles": plan, "decisions": safe, "held": held, "unresolved": unresolved,
              "selected": len(selected), "verified": 0, "saved": 0,
              "failed": 0, "deferred": 0, "verified_listings": []}
    for item in safe[:8]:
        print(f"  {item['length']:2}/80: {item['old_title']} -> {item['new_title']}", flush=True)
    if not apply or not selected:
        return report

    original = {str(row["listing_id"]): dict(row) for row in rows}
    codes = sorted({str(chosen[key].part_type_code) for key, _ in selected})
    current = {str(row["listing_id"]): row for row in collect(client, codes)}
    unchanged = {key for key, _ in selected if current.get(key) == original[key]}
    report["deferred"] = len(selected) - len(unchanged)
    # Persist attempts before writes: a timed-out item cannot starve the rest of the queue.
    for key, _ in selected:
        state.setdefault(key, {})["attempted"] = now
    checkpoint(state)
    expected = {}
    for item in safe:
        key = item["listing_id"]
        if key in unchanged:
            expected[key] = item["new_title"]
    for item in held:
        if item.get("reason") == "unchanged" and item["listing_id"] in unchanged:
            expected[item["listing_id"]] = item["old_title"]
    for item in plan:
        ids = [key for key in item["listing_ids"] if key in unchanged]
        if not ids:
            continue
        try:
            results = workflow.apply_titles(client, [{**item, "listing_ids": ids}],
                                            dry_run=False, cap=cap)
            if any(result["status"] != "ok" for result in results):
                for key in ids:
                    expected.pop(key, None)
                report["failed"] += len(ids)
        except Exception as exc:
            print(f"  title save failed: {type(exc).__name__}", flush=True)
            for key in ids:
                expected.pop(key, None)
            report["failed"] += len(ids)
    verified = {str(row["listing_id"]): row for row in collect(client, codes)}
    for key, title in expected.items():
        row = verified.get(key)
        if row and row.get("title") == title:
            state[key] = {"seen": state[key].get("seen", now),
                          "attempted": now, "checked": now,
                          "signature": signature(row, chosen[key], renderer_revision)}
            report["verified"] += 1
            report["saved"] += original[key].get("title") != title
            report["verified_listings"].append({
                "listing_id": key, "r_number": chosen[key].uid(),
                "old_title": original[key].get("title"), "new_title": title,
                "length": len(title),
            })
        else:
            report["failed"] += 1
    checkpoint(state)
    print(f"Verified {report['verified']}; saved {report['saved']}; failed {report['failed']}; "
          f"held {len(held)}. Saved titles in unlisted only.", flush=True)
    return report


def run(args):
    from coreyard.ebay.cli import _read_json, _write_json
    from coreyard.yms.db import connect
    from coreyard.yms.interchange import InterchangeResolver
    from coreyard.yms.inventory import fetch_parts

    if args.batch_size < 1 or args.cap < 1:
        raise ValueError("batch size and cap must be positive")
    workflow.check_cap(args.batch_size, args.cap, False)
    load_env()
    store = load_store()
    state_path = Path(args.state)
    state = _read_json(state_path) if state_path.exists() else {}
    catalogue = fetch_parts(images_only=False)
    client = PortalClient(load_portal(args.portal))
    codes = sorted({str(part.part_type_code) for part in catalogue
                    if part.part_type_code is not None})
    if args.part_type:
        codes = [args.part_type]
    print(f"Scanning {len(codes)} part types for unlisted titles ...", flush=True)
    rows = collect(client, codes)

    def attach(parts):
        if parts:
            with connect() as conn:
                InterchangeResolver(conn).attach(parts)

    result = run_batch(client, rows, catalogue, store, state,
                       batch_size=args.batch_size, cap=args.cap, apply=args.apply,
                       attach=attach, checkpoint=lambda value: _write_json(state_path, value),
                       renderer_revision=revision(store))
    _write_json(Path(args.out), result)
    return 1 if result["failed"] else 0


def add_arguments(parser):
    parser.add_argument("--batch-size", type=int, default=100,
                        help="maximum listings checked per pass; retries rotate")
    parser.add_argument("--cap", type=int, default=100)
    parser.add_argument("--apply", action="store_true",
                        help="save and verify unlisted titles; never prices or publication")
    parser.add_argument("--state", default=str(out_dir() / "ebay-title-state.json"))
    parser.add_argument("--out", default=str(out_dir() / "ebay-auto-titles.json"))
    parser.add_argument("--portal")
    parser.add_argument("--part-type", help="check only one source part-type code")
    parser.set_defaults(func=run)
    return parser
