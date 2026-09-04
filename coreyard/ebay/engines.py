"""Pure planning for reviewed listing-portal titles."""

from __future__ import annotations

from collections import defaultdict


class ApplyGuard(RuntimeError):
    """A marketplace decision could not be mapped safely to a source part."""


def _groups(listings: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in listings:
        key = str(row.get("interchange_number") or "").strip()
        if not key:
            key = f"_listing_{row['listing_id']}"
        grouped[key].append(row)
    return dict(grouped)


def title_decisions(
    listings: list[dict], proposals: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Retarget every safe per-interchange title onto current listing IDs."""
    groups = _groups(listings)
    proposed = {str(item["interchange"]): item for item in proposals}
    ready, held = [], []
    for key, rows in sorted(groups.items()):
        suggestion = proposed.get(key)
        if not suggestion:
            held.append({
                "interchange": key,
                "listing_ids": [r["listing_id"] for r in rows],
                "reason": "no accepted title proposal",
            })
            continue
        title = " ".join(str(suggestion.get("new_title") or "").split())
        if not title or len(title) > 80 or "&" in title or '"' in title:
            held.append({
                "interchange": key,
                "listing_ids": [r["listing_id"] for r in rows],
                "reason": "proposal fails the 80-character portal title guard",
            })
            continue
        ready.append({
            "interchange": key,
            "listing_ids": [str(row["listing_id"]) for row in rows],
            "old_titles": sorted({str(row.get("title") or "") for row in rows}),
            "new_title": title,
        })
    return ready, held


def plan_titles(
    listings: list[dict], proposals: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Return only title decisions that differ from the current portal title."""
    decisions, held = title_decisions(listings, proposals)
    ready = [
        item for item in decisions
        if any(old.strip() != item["new_title"] for old in item["old_titles"])
    ]
    return ready, held


def merge_overrides(existing: dict, fresh: dict) -> dict:
    """Merge title decisions and discard obsolete non-title fields.

    The override file is a replacement, not a patch. Keeping earlier reviewed titles is
    necessary, but historical price keys must not survive now that the source is the sole
    price authority.
    """
    parts: dict[str, dict[str, str]] = {}
    for source in (existing or {}, fresh or {}):
        for r_number, override in (source.get("parts") or {}).items():
            title = " ".join(str((override or {}).get("title") or "").split())
            if title:
                parts[str(r_number)] = {"title": title}
    return {"version": 1, "parts": dict(sorted(parts.items()))}


def build_title_overrides(details: dict[str, dict], title_plan: list[dict]) -> dict:
    """Build the title-only per-R# file consumed by the canonical renderer."""
    by_listing: dict[str, str] = {}
    for item in title_plan:
        for listing_id in item["listing_ids"]:
            by_listing[str(listing_id)] = item["new_title"]

    parts: dict[str, dict[str, str]] = {}
    for listing_id, title in by_listing.items():
        detail = details.get(listing_id) or {}
        r_number = str(detail.get("r_number") or "").strip()
        if not r_number:
            raise ApplyGuard(
                f"listing {listing_id} has no mapped R#; refusing title override"
            )
        prior = parts.setdefault(r_number, {"title": title})
        if prior["title"] != title:
            raise ApplyGuard(f"R# {r_number} received conflicting title decisions")
    return {"version": 1, "parts": dict(sorted(parts.items()))}
