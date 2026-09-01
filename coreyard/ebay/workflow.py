"""Guarded portal writes shared by part-specific eBay workflows."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal

from coreyard.ebay import aspects
from coreyard.transform.pricing import money_str


class ApplyGuard(RuntimeError):
    """A marketplace write crossed an unreviewed safety boundary."""


def check_cap(total: int, cap: int, force: bool) -> None:
    if total > cap and not force:
        raise ApplyGuard(
            f"{total} listings exceeds the safety cap of {cap}; review the dry run and "
            "pass --yes-i-mean-it"
        )


def apply_titles(client, accepted: list[dict], *, tab: str = "unlisted",
                 dry_run: bool = True, cap: int = 25,
                 force: bool = False) -> list[dict]:
    total = sum(len(item["listing_ids"]) for item in accepted)
    if not dry_run:
        check_cap(total, cap, force)
    results = []
    for item in accepted:
        status = "DRY-RUN"
        if not dry_run:
            error = client.bulk_update(
                [{"field": "title", "action": "change_to",
                  "value": item["new_title"]}],
                [str(value) for value in item["listing_ids"]], tab=tab,
            )
            status = f"ERROR: {error}" if error else "ok"
        results.append({
            "interchange": item.get("interchange", ""),
            "n": len(item["listing_ids"]),
            "new": item["new_title"],
            "status": status,
        })
    return results


def apply_prices(client, priced: list[dict], *, tab: str = "unlisted",
                 dry_run: bool = True, cap: int = 25, force: bool = False,
                 also_starting_price: bool = False) -> list[dict]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for item in priced:
        ids = item.get("listing_ids") or [item.get("listing_id")]
        buckets[money_str(item["new_price"])].extend(str(value) for value in ids if value)
    total = sum(map(len, buckets.values()))
    if not dry_run:
        check_cap(total, cap, force)
    results = []
    for price, listing_ids in sorted(buckets.items(), key=lambda item: Decimal(item[0])):
        changes = [{"field": "fixed_price", "action": "change_to", "value": price}]
        if also_starting_price:
            changes.append({
                "field": "starting_price", "action": "change_to", "value": price
            })
        status = "DRY-RUN"
        if not dry_run:
            error = client.bulk_update(changes, listing_ids, tab=tab)
            status = f"ERROR: {error}" if error else "ok"
        results.append({"price": price, "n": len(listing_ids), "status": status})
    return results


def aspect_plan(rows: list[dict], details: dict[str, dict] | None = None,
                warranty: str = "", metadata: dict | None = None) -> list[dict]:
    """Bucket listings with identical derived item-specific patches."""
    details = details or {}
    buckets: dict[str, list[str]] = defaultdict(list)
    patches: dict[str, dict] = {}
    for row in rows:
        listing_id = str(row["listing_id"])
        derived = aspects.derive(row, details.get(listing_id), metadata)
        patch = aspects.to_patch(derived, warranty=warranty)
        key = json.dumps(patch, sort_keys=True)
        patches[key] = patch
        buckets[key].append(listing_id)
    return [
        {"n": len(ids), "listing_ids": ids, "patch": patches[key]}
        for key, ids in sorted(buckets.items(), key=lambda item: -len(item[1]))
    ]


def apply_aspects(client, rows: list[dict], *, details: dict[str, dict] | None = None,
                  warranty: str = "", metadata: dict | None = None,
                  tab: str = "unlisted",
                  dry_run: bool = True, cap: int = 25,
                  force: bool = False) -> list[dict]:
    plan = aspect_plan(rows, details, warranty, metadata)
    total = sum(item["n"] for item in plan)
    if not dry_run:
        check_cap(total, cap, force)
    results = []
    for item in plan:
        status = "DRY-RUN"
        if not dry_run:
            error = client.bulk_update(
                [], item["listing_ids"], tab=tab, item_specifics=item["patch"]
            )
            status = f"ERROR: {error}" if error else "ok"
        results.append({**item, "status": status})
    return results


def reset_titles(client, listing_ids: list[str], *, tab: str) -> str:
    return client.bulk_update([], listing_ids, tab=tab, reset_titles=True)


def reset_prices(client, listing_ids: list[str], *, tab: str) -> str:
    changes = [
        {"field": "fixed_price", "action": "reset_to_retail", "value": ""},
        {"field": "starting_price", "action": "reset_to_retail", "value": ""},
    ]
    return client.bulk_update(changes, listing_ids, tab=tab)
