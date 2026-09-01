"""Pure planning and guarded application for engine titles and researched prices."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from coreyard.transform.pricing import money_str, parse_money
from coreyard.ebay.util import truthy


class ApplyGuard(RuntimeError):
    """A marketplace write exceeded a reviewed safety boundary."""


def _groups(listings: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in listings:
        key = str(row.get("interchange_number") or "").strip()
        if not key:
            key = f"_listing_{row['listing_id']}"
        grouped[key].append(row)
    return dict(grouped)


def title_decisions(listings: list[dict], proposals: list[dict]) -> tuple[list[dict], list[dict]]:
    """Retarget every safe per-interchange title onto current live listing IDs."""
    groups = _groups(listings)
    proposed = {str(item["interchange"]): item for item in proposals}
    ready, held = [], []
    for key, rows in sorted(groups.items()):
        suggestion = proposed.get(key)
        if not suggestion:
            held.append({"interchange": key, "listing_ids": [r["listing_id"] for r in rows],
                         "reason": "no accepted title proposal"})
            continue
        title = " ".join(str(suggestion.get("new_title") or "").split())
        if not title or len(title) > 80 or "&" in title or '"' in title:
            held.append({"interchange": key, "listing_ids": [r["listing_id"] for r in rows],
                         "reason": "proposal fails the 80-character portal title guard"})
            continue
        ready.append({
            "interchange": key,
            "listing_ids": [str(row["listing_id"]) for row in rows],
            "old_titles": sorted({str(row.get("title") or "") for row in rows}),
            "new_title": title,
        })
    return ready, held


def plan_titles(listings: list[dict], proposals: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return only decisions that differ from the current portal title."""
    decisions, held = title_decisions(listings, proposals)
    ready = [item for item in decisions
             if any(old.strip() != item["new_title"] for old in item["old_titles"])]
    return ready, held


def price_decisions(
    listings: list[dict],
    research: list[dict],
    *,
    min_comps: int = 1,
    max_swing: Decimal | None = Decimal("1.5"),
    include_disclosure: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Select every safe researched price for current IDs; return ``(ready, held)``."""
    current = {str(row["listing_id"]): row for row in listings}
    researched = {str(item["listing_id"]): item for item in research}
    ready, held = [], []
    for listing_id, row in sorted(current.items()):
        item = researched.get(listing_id)
        reason = ""
        if not item:
            reason = "not researched yet"
        new = parse_money(item.get("suggested_price")) if item else None
        old = parse_money(row.get("price"))
        if item and (item.get("confidence") == "none" or new is None or new <= 0):
            reason = "no usable researched price"
        elif item and int(item.get("comp_count") or 0) < min_comps:
            reason = f"only {item.get('comp_count') or 0} comparables"
        elif item and item.get("needs_disclosure") and not include_disclosure:
            reason = "condition caveat must be disclosed before publishing"
        elif item and old and max_swing is not None and abs(new - old) / old > max_swing:
            reason = f"price move exceeds {max_swing:.0%} safety ceiling"
        record = {
            **(item or {}),
            "listing_id": listing_id,
            "old_price": money_str(old) if old is not None else "",
            "new_price": money_str(new) if new is not None else "",
        }
        if reason:
            held.append({**record, "reason": reason})
        else:
            ready.append(record)
    return ready, held


def plan_prices(
    listings: list[dict],
    research: list[dict],
    *,
    min_comps: int = 1,
    max_swing: Decimal | None = Decimal("1.5"),
    include_disclosure: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Return only safe decisions that differ from the current portal price."""
    decisions, held = price_decisions(
        listings, research, min_comps=min_comps, max_swing=max_swing,
        include_disclosure=include_disclosure
    )
    return ([item for item in decisions if item["old_price"] != item["new_price"]], held)


def merge_overrides(existing: dict, fresh: dict) -> dict:
    """Layer fresh per-R# decisions over the file the renderer already reads.

    The override file is a replacement, not a patch: the renderer publishes what it
    contains and nothing else. Any writer that emitted only its own decisions would
    silently revert every product decided by a different pass, so every writer merges.
    """
    parts = dict((existing or {}).get("parts") or {})
    for r_number, override in ((fresh or {}).get("parts") or {}).items():
        parts[str(r_number)] = {**parts.get(str(r_number), {}), **override}
    return {"version": (fresh or {}).get("version", 1),
            "parts": dict(sorted(parts.items()))}


def build_overrides(
    details: dict[str, dict],
    title_plan: list[dict],
    price_plan: list[dict],
) -> dict:
    """Build the per-R# file consumed by CoreYard's canonical Shopify renderer."""
    by_listing: dict[str, dict] = {}
    for item in title_plan:
        for listing_id in item["listing_ids"]:
            by_listing.setdefault(str(listing_id), {})["title"] = item["new_title"]
    for item in price_plan:
        by_listing.setdefault(str(item["listing_id"]), {})["price"] = item["new_price"]

    parts: dict[str, dict] = {}
    for listing_id, override in by_listing.items():
        detail = details.get(listing_id) or {}
        r_number = str(detail.get("r_number") or "").strip()
        if not r_number:
            raise ApplyGuard(f"listing {listing_id} has no mapped R#; refusing Shopify override")
        prior = parts.setdefault(r_number, {})
        for field, value in override.items():
            if field in prior and prior[field] != value:
                raise ApplyGuard(f"R# {r_number} received conflicting {field} decisions")
            prior[field] = value
    return {"version": 1, "parts": dict(sorted(parts.items()))}


def post_plan(
    listings: list[dict],
    details: dict[str, dict],
    titles: list[dict],
    prices: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Build an explicit safe-to-submit manifest and explain every held listing."""
    by_title: dict[str, dict] = {}
    for item in titles:
        for listing_id in item["listing_ids"]:
            by_title[str(listing_id)] = item
    by_price = {str(item["listing_id"]): item for item in prices}
    ready, held = [], []
    for row in listings:
        listing_id = str(row["listing_id"])
        reasons = []
        title = by_title.get(listing_id)
        price = by_price.get(listing_id)
        if not title:
            reasons.append("no safe title decision")
        if not price:
            reasons.append("no safe researched price")
        submit_value = row.get("valid_for_submit")
        if submit_value not in (None, "") and not truthy(submit_value):
            reasons.append("portal reports listing is not valid for submission")
        detail = details.get(listing_id) or {}
        record = {
            "listing_id": listing_id,
            "interchange": str(row.get("interchange_number") or ""),
            "r_number": str(detail.get("r_number") or row.get("r_number") or ""),
            "title": title.get("new_title", "") if title else "",
            "price": price.get("new_price", "") if price else "",
        }
        (held if reasons else ready).append(
            {**record, "reasons": reasons} if reasons else record
        )
    return ready, held


def _check_cap(total: int, cap: int, force: bool) -> None:
    if total > cap and not force:
        raise ApplyGuard(
            f"{total} listings exceeds the safety cap of {cap}; review the dry run and "
            "pass --yes-i-mean-it"
        )


def apply_titles(client, plan: list[dict], *, dry_run: bool = True, cap: int = 25,
                 force: bool = False) -> list[dict]:
    total = sum(len(item["listing_ids"]) for item in plan)
    if not dry_run:
        _check_cap(total, cap, force)
    results = []
    for item in plan:
        status = "DRY-RUN"
        if not dry_run:
            error = client.bulk_update(
                [{"field": "title", "action": "change_to", "value": item["new_title"]}],
                item["listing_ids"], tab="unlisted"
            )
            status = f"ERROR: {error}" if error else "ok"
        results.append({"interchange": item["interchange"],
                        "n": len(item["listing_ids"]), "status": status})
    return results


def apply_prices(client, plan: list[dict], *, dry_run: bool = True, cap: int = 25,
                 force: bool = False) -> list[dict]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for item in plan:
        buckets[item["new_price"]].append(str(item["listing_id"]))
    total = sum(map(len, buckets.values()))
    if not dry_run:
        _check_cap(total, cap, force)
    results = []
    for price, listing_ids in sorted(buckets.items(), key=lambda item: Decimal(item[0])):
        status = "DRY-RUN"
        if not dry_run:
            error = client.bulk_update(
                [{"field": "fixed_price", "action": "change_to", "value": price}],
                listing_ids, tab="unlisted"
            )
            status = f"ERROR: {error}" if error else "ok"
        results.append({"price": price, "n": len(listing_ids), "status": status})
    return results
