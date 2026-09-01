"""One unattended pass over the listing portal, for the parts two workers added today.

The channel already had every step this needs, as fifteen commands with JSON files between
them. That is the right shape for a person working through a backlog by part type, and the
wrong shape for keeping up: fresh inventory arrives every day, and a chain that needs an
operator to name a part type, run six commands in order and remember which output feeds
which is a chain that runs when someone has an afternoon, not when the yard has new parts.

So this is a driver, not a new pipeline. Every decision it makes is made by the module that
already owned it — :mod:`coreyard.ebay.link` for which part a listing is,
:mod:`coreyard.ebay.titles` for what it is called, :mod:`coreyard.ebay.comps` for what it is
worth, :mod:`coreyard.ebay.engines` for whether that price is safe to write, and
:mod:`coreyard.ebay.workflow` for the write itself. Nothing here decides anything those
cannot already be tested deciding on their own.

**It stops at the portal.** It saves titles and prices where a person can review them; it
never calls submit, so nothing it does reaches a buyer until someone runs ``ebay push``.
That is the review window the channel is built around, and an unattended job is exactly the
thing that must not close it.

The unlisted tab is the work queue, so there is no cursor to keep and none to corrupt: a
part that needs listing is in the tab because the portal put it there, and it leaves when it
is pushed. A run that dies re-reads the same queue and picks up where the page cache left
off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Optional

from coreyard.config import StoreProfile
from coreyard.ebay import comps, engines, link, titles
from coreyard.models import Part


@dataclass
class Plan:
    """What one pass decided, and everything it refused to decide."""

    titles: list[dict] = field(default_factory=list)        # grouped for the portal
    prices: list[dict] = field(default_factory=list)        # per listing
    held: list[dict] = field(default_factory=list)          # with a reason, always
    resolved: int = 0
    listings: int = 0

    @property
    def title_writes(self) -> int:
        return sum(len(item["listing_ids"]) for item in self.titles)

    def summary(self) -> str:
        return (f"{self.listings} listings, {self.resolved} resolved to a yard part; "
                f"{self.title_writes} title(s) and {len(self.prices)} price(s) ready, "
                f"{len(self.held)} held")


def research_by_listing(resolved: dict[str, Part],
                        research: dict[str, dict]) -> list[dict]:
    """Retarget per-part research onto the listings those parts are.

    Comparables are researched per *part*, because that is what a comparable is about, while
    the portal is written per *listing*. One part can hold several listings, so this is a
    fan-out rather than a rename, and doing it here keeps
    :func:`coreyard.ebay.engines.price_decisions` — which already guards researched prices
    for the engine flow — usable unchanged.
    """
    records = []
    for listing_id, part in resolved.items():
        found = research.get(str(part.r_number).strip())
        if not found:
            continue
        records.append({**found, "listing_id": str(listing_id),
                        "r_number": str(part.r_number)})
    return records


def plan(rows: list[dict], parts: Iterable[Part], store: StoreProfile,
         research: Optional[dict[str, dict]] = None, *,
         details: Optional[dict[str, dict]] = None,
         title_limit: int = 80, min_comps: int = 3,
         max_swing: Decimal | None = Decimal("1.5")) -> Plan:
    """Decide titles and prices for a tab's worth of listings. Pure.

    ``research`` is keyed by R#; absent, the pass plans titles only, which is a useful run
    in its own right — a title costs nothing to be wrong about in the unlisted tab and is
    the cheaper half of the work.
    """
    resolved, unresolved = link.resolve(rows, parts)
    if details:
        resolved, unresolved = link.merge_details(
            resolved, unresolved, details,
            {str(part.r_number).strip(): part for part in parts},
        )
    result = Plan(listings=len(rows), resolved=len(resolved))
    result.held.extend({**item, "stage": "link"} for item in unresolved)

    accepted, held = titles.decisions_for(rows, resolved, store, limit=title_limit)
    result.titles = titles.group_for_portal(accepted)
    result.held.extend({**item, "stage": "title"} for item in held)

    if research:
        priced, price_held = engines.plan_prices(
            [row for row in rows if str(row["listing_id"]) in resolved],
            research_by_listing(resolved, research),
            min_comps=min_comps, max_swing=max_swing,
        )
        result.prices = priced
        result.held.extend({**item, "stage": "price"} for item in price_held)
    return result


def overrides_for(plan_result: Plan, resolved: dict[str, Part],
                  existing: Optional[dict] = None) -> dict:
    """The per-R# file the canonical Shopify renderer reads, merged over what it holds.

    The channel's one meeting point with Shopify, and it stays that: a researched price
    reaches the store through the renderer and the fingerprint, never as a product patch a
    later sync would revert.

    The merge is the part that matters for a nightly run. This file is a *replacement*, not
    a patch — the renderer reads whatever it contains and nothing else. A run that wrote
    only the parts it touched tonight would silently drop every decision reviewed before
    tonight, and the next sync would revert those products to their unreviewed titles and
    prices. So a pass adds to the file and updates its own R#s; it never shortens it.
    """
    details = {listing_id: {"r_number": str(part.r_number)}
               for listing_id, part in resolved.items()}
    fresh = engines.build_overrides(details, plan_result.titles, plan_result.prices)
    return engines.merge_overrides(existing or {}, fresh)


# ------------------------------------------------------------------ comparables ---
def _page_name(r_number: str, suffix: str = "") -> str:
    return f"{re.sub(r'[^A-Za-z0-9._-]', '_', str(r_number))}{suffix}.html"


def research_parts(parts: Iterable[Part], store: StoreProfile, pages: Path,
                   fetcher: Callable[[str, Path], bool], *,
                   done: Optional[dict[str, dict]] = None,
                   retry_thin: bool = True,
                   checkpoint: Optional[Callable[[dict], None]] = None,
                   every: int = 20, log=None) -> dict[str, dict]:
    """Price each part from a public index page, keyed by R#.

    ``fetcher`` is injected so the whole loop is testable without a network, and because
    the page cache it writes to is what makes a killed run cheap to resume: a page already
    on disk is not fetched again.
    """
    results = dict(done or {})
    todo = [part for part in parts if str(part.r_number).strip() not in results]
    for number, part in enumerate(todo, 1):
        r_number = str(part.r_number).strip()
        query = comps.query_for(part, store)
        page = pages / _page_name(r_number)
        record = None
        if fetcher(query, page):
            record = comps.research_part(part, store, page)
        if (record is None or not record.comps) and retry_thin:
            # The narrow query found nothing, so ask the broader one. A part with no
            # comparable is held, not guessed at, so this is the last chance to find one.
            broad = comps.query_for(part, store, broad=True)
            if broad and broad != query:
                second = pages / _page_name(r_number, "_2")
                if fetcher(broad, second):
                    sources = [page, second] if page.is_file() else [second]
                    record = comps.research_part(part, store, sources)
                    record.query = query
        if record is None:
            record = comps.Research(r_number=r_number, query=query)
        results[r_number] = record.as_record()
        if checkpoint and (number % every == 0 or number == len(todo)):
            checkpoint(results)
        if log and (number % every == 0 or number == len(todo)):
            log(f"  researched {number}/{len(todo)}")
    return results
