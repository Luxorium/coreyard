"""Marketplace titles, from the one renderer.

This module deliberately contains no title-building logic. It joins a portal listing to the
part it actually is — by R#, the same identity Shopify keys on — and asks
``transform/seo.py`` what that part is called, inside eBay's character budget.

It replaces a builder that reverse-engineered facts out of the portal's own title string.
That approach existed only because the listing tool could not reach the yard database; once
the two projects became one, it was both unnecessary and worse. It was unnecessary because
every fact it recovered by parsing — year span, vehicle, displacement, VIN code — is a
column the database already holds. It was worse in three ways that a parser cannot fix:

  * It was not idempotent. Run against a title it had itself written, it produced a
    *different* title: "… Assembly OEM" became "… Assembly OEM OEM", an engine code was
    dropped, and one listing's vehicle changed from the Chevrolet the fitment names to the
    Cadillac its donor happened to be.
  * It could only ever recover what the source string happened to mention, so a fact the
    yard knows but the old title omitted was lost for good.
  * It made the two storefronts drift. The same part had one name on Shopify and another
    on eBay, and neither was reviewable against the other.

The remaining difference between the two storefronts is a number: eBay allows 80 characters
where Shopify allows 255, and its portal escapes the title before eBay counts it. Both are
arguments to :func:`coreyard.transform.seo.build_title`, not reasons for a second builder.
"""

from __future__ import annotations

from coreyard.config import StoreProfile
from coreyard.ebay.util import TITLE_MAX, title_length
from coreyard.models import Part
from coreyard.transform import seo


def render_title(part: Part, store: StoreProfile,
                 limit: int = TITLE_MAX) -> tuple[str, list[str]]:
    """The part's canonical title, inside the marketplace's budget."""
    return seo.fit_title(part, store, limit=limit, measure=title_length,
                         compact=True)


def validate(title: str, current: str, limit: int = TITLE_MAX) -> tuple[bool, str]:
    """Whether this title may be written to the portal.

    The checks are about the *destination*, not about the part: the renderer is trusted for
    what a part is called, so nothing here second-guesses its wording.
    """
    if not title.strip():
        return False, "renderer produced no title"
    if title_length(title) > limit:
        return False, f"too long ({title_length(title)} > {limit})"
    if "&" in title or '"' in title:
        return False, "contains & or quote"
    if title.strip() == (current or "").strip():
        return False, "unchanged"
    return True, ""


def decisions_for(
    rows: list[dict],
    resolved: dict[str, Part],
    store: StoreProfile,
    *,
    limit: int = TITLE_MAX,
) -> tuple[list[dict], list[dict]]:
    """Decide titles for listings already resolved to their parts (see ``ebay/link.py``)."""
    details = {listing_id: {"r_number": str(part.r_number)}
               for listing_id, part in resolved.items()}
    parts = {str(part.r_number): part for part in resolved.values()}
    keep = [row for row in rows if str(row["listing_id"]) in resolved]
    return decisions(keep, details, parts, store, limit=limit)


def decisions(
    rows: list[dict],
    details: dict[str, dict],
    parts: dict[str, Part],
    store: StoreProfile,
    *,
    limit: int = TITLE_MAX,
) -> tuple[list[dict], list[dict]]:
    """Decide one title per listing; return ``(accepted, held)``.

    ``parts`` is passed in rather than fetched here so that this stays a pure function over
    already-extracted data — the same reason every other planning step in CoreYard takes its
    inputs. The caller decides whether that map came from the database or a fixture.
    """
    proposed: dict[str, dict] = {}
    held: list[dict] = []
    for row in rows:
        listing_id = str(row["listing_id"])
        detail = details.get(listing_id) or {}
        r_number = str(detail.get("r_number") or row.get("r_number") or "").strip()
        current = str(row.get("title") or "")
        record = {
            "listing_id": listing_id,
            "r_number": r_number,
            "interchange": str(row.get("interchange_number") or ""),
            "old_title": current,
            "old_length": title_length(current),
        }
        if not r_number:
            held.append({**record, "reason": "listing carries no R#"})
            continue
        part = parts.get(r_number)
        if part is None:
            # The yard no longer has it, or never did. Renaming a listing whose part cannot
            # be identified is exactly the case where a wrong title is most likely.
            held.append({**record, "reason": "no yard part for this R#"})
            continue
        title, dropped = render_title(part, store, limit=limit)
        record.update({"new_title": title, "length": title_length(title),
                       "dropped": dropped})
        ok, reason = validate(title, current, limit=limit)
        if not ok:
            held.append({**record, "reason": reason})
            continue
        proposed[listing_id] = record

    # Two listings that are genuinely different parts must not go out under one name. This
    # can only happen where the yard itself describes them identically, so it is reported
    # rather than resolved: a disambiguator invented here would not match Shopify's.
    by_title: dict[str, list[str]] = {}
    for listing_id, record in proposed.items():
        by_title.setdefault(record["new_title"], []).append(listing_id)
    accepted = []
    for listing_id, record in sorted(proposed.items()):
        clash = by_title[record["new_title"]]
        groups = {proposed[other]["interchange"] for other in clash}
        if len(clash) > 1 and len(groups) > 1:
            held.append({**record,
                         "reason": "two interchange groups would share one title"})
            continue
        accepted.append(record)
    return accepted, held


def group_for_portal(accepted: list[dict]) -> list[dict]:
    """Collapse per-listing decisions into the one-write-per-identical-title shape.

    The portal charges one bulk update per distinct value, so listings that resolved to the
    same title are written together. Grouping by the title itself rather than by interchange
    keeps that true even where one interchange group's listings resolve differently.
    """
    grouped: dict[str, dict] = {}
    for record in accepted:
        entry = grouped.setdefault(record["new_title"], {
            "interchange": record["interchange"],
            "new_title": record["new_title"],
            "listing_ids": [],
            "old_titles": [],
        })
        entry["listing_ids"].append(record["listing_id"])
        if record["old_title"] not in entry["old_titles"]:
            entry["old_titles"].append(record["old_title"])
    return [grouped[key] for key in sorted(grouped)]
