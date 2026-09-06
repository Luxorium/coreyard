"""One unattended pass over the listing portal.

The portal queue is joined to source parts, titled with the canonical renderer, and
offered the exact source price. The pass stops at the portal; publication to eBay remains a
separate explicit command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from coreyard.config import StoreProfile
from coreyard.ebay import engines, link, titles
from coreyard.models import Part
from coreyard.ebay.pricing import PricePolicy, quote
from coreyard.transform.pricing import money_str, parse_money


@dataclass
class Plan:
    """What one pass would write, plus every listing it could not identify safely."""

    titles: list[dict] = field(default_factory=list)
    prices: list[dict] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)
    resolved: int = 0
    listings: int = 0

    @property
    def title_writes(self) -> int:
        return sum(len(item["listing_ids"]) for item in self.titles)

    def summary(self) -> str:
        return (
            f"{self.listings} listings, {self.resolved} resolved to a yard part; "
            f"{self.title_writes} title(s) and {len(self.prices)} source-price change(s) "
            f"ready, {len(self.held)} held"
        )


def source_price_decisions(
    rows: list[dict], resolved: dict[str, Part], *, store=None, policy=None,
    freight_policies=None,
) -> tuple[list[dict], list[dict]]:
    """Return portal price changes using the exact source amount for each part."""
    current = {str(row["listing_id"]): row for row in rows}
    ready, held = [], []
    for listing_id, part in sorted(resolved.items()):
        row = current.get(str(listing_id))
        if row is None:
            continue
        if part.price is None or part.price <= 0:
            held.append({
                "listing_id": str(listing_id),
                "r_number": str(part.r_number),
                "reason": "source system has no positive price",
            })
            continue
        try:
            source_price = quote(part, store or StoreProfile(), row.get("shipping_mode"),
                                 policy or PricePolicy(), freight_policies)["new_price"]
        except ValueError as exc:
            held.append({"listing_id": str(listing_id), "reason": str(exc)})
            continue
        old = parse_money(row.get("price"))
        old_price = money_str(old) if old is not None else ""
        if old_price != source_price:
            ready.append({
                "listing_id": str(listing_id),
                "r_number": str(part.r_number),
                "old_price": old_price,
                "new_price": source_price,
            })
    return ready, held


def plan(
    rows: list[dict],
    parts: Iterable[Part],
    store: StoreProfile,
    *,
    details: Optional[dict[str, dict]] = None,
    title_limit: int = 80,
    price_policy: PricePolicy | None = None,
) -> Plan:
    """Plan canonical titles and exact source prices for a portal tab. Pure."""
    catalogue = list(parts)
    resolved, unresolved = link.resolve(rows, catalogue)
    if details:
        resolved, unresolved = link.merge_details(
            resolved,
            unresolved,
            details,
            {str(part.r_number).strip(): part for part in catalogue},
        )
    result = Plan(listings=len(rows), resolved=len(resolved))
    result.held.extend({**item, "stage": "link"} for item in unresolved)

    accepted, held = titles.decisions_for(rows, resolved, store, limit=title_limit)
    result.titles = titles.group_for_portal(accepted)
    result.held.extend({**item, "stage": "title"} for item in held)

    result.prices, price_held = source_price_decisions(
        rows, resolved, store=store, policy=price_policy)
    result.held.extend({**item, "stage": "price"} for item in price_held)
    return result


def overrides_for(
    plan_result: Plan,
    resolved: dict[str, Part],
    existing: Optional[dict] = None,
) -> dict:
    """Merge reviewed titles into the renderer's title-only override file."""
    details = {
        listing_id: {"r_number": str(part.r_number)}
        for listing_id, part in resolved.items()
    }
    fresh = engines.build_title_overrides(details, plan_result.titles)
    return engines.merge_overrides(existing or {}, fresh)
