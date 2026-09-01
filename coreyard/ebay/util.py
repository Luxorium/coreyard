"""Small, portal-neutral helpers shared by the eBay workflows."""

from __future__ import annotations

from collections import defaultdict


TITLE_MAX = 80


def title_length(value: str) -> int:
    """The portal's effective length after HTML entity escaping."""
    return len(value) + 4 * value.count("&") + 5 * value.count('"')


def groups_by_interchange(rows: list[dict]) -> dict[str, list[dict]]:
    """Group normalized portal rows by interchange, with listing ID as fallback."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = str(row.get("interchange_number") or "").strip()
        if not key:
            key = f"_listing_{row['listing_id']}"
        groups[key].append(row)
    return dict(groups)


def chunks(values, size: int):
    if size < 1:
        raise ValueError("batch size must be at least 1")
    items = list(values)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def truthy(value) -> bool:
    """Interpret common portal boolean representations conservatively."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}
