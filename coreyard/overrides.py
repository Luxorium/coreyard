"""Reviewed per-part decisions keyed by CoreYard's stable R#.

Titles still pass through the canonical renderer, so both Shopify sinks publish and
fingerprint the same decision. Prices are deliberately absent: the source database is the
only authority for a part's price.

``ship`` names a shipping group for one part, overriding what its part type would classify
it as. Shipping is otherwise decided per *part type*, which is the right default because
that is what the freight table measures — but a type is broad, and a few parts do not ship
like their neighbours: a small car's radiator core support goes UPS while a truck's needs a
pallet. Without this the only way to reprice one part is to reprice its whole type. The
named group must exist in the site's shipping policy; one that does not is refused loudly
rather than quietly falling back, because a wrong shipping tag is a promise broken at
checkout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class OverrideError(ValueError):
    """A catalogue override file is unsafe or malformed."""


@dataclass(frozen=True)
class PartOverride:
    title: str = ""
    # A shipping group id from the site's freight policy, e.g. "GROUND49". Empty means the
    # part is classified by its part type like everything else.
    ship: str = ""


@dataclass(frozen=True)
class CatalogOverrides:
    parts: dict[str, PartOverride]

    def for_r_number(self, value) -> PartOverride:
        return self.parts.get(str(value).strip(), PartOverride())


EMPTY = CatalogOverrides({})


def load(path: str | Path | None) -> CatalogOverrides:
    if not path:
        return EMPTY
    # Resolved against the data root: a relative path in `.env` names one of this
    # installation's files, not one relative to whatever directory the caller happened to
    # start in. See `config.data_path`.
    from coreyard.config import data_path

    source = data_path(path) or Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OverrideError(f"catalog override file not found: {source}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise OverrideError(f"cannot read catalog override file {source}: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"version", "parts"}:
        raise OverrideError("catalog overrides must contain only 'version' and 'parts'")
    if data.get("version") != 1:
        raise OverrideError("catalog overrides version must be 1")
    raw_parts = data.get("parts")
    if not isinstance(raw_parts, dict):
        raise OverrideError("catalog overrides 'parts' must be an object keyed by R#")
    parts: dict[str, PartOverride] = {}
    for raw_key, raw in raw_parts.items():
        key = str(raw_key).strip()
        if not key:
            raise OverrideError("catalog override contains an empty R#")
        if not isinstance(raw, dict) or set(raw) - {"title", "ship"}:
            raise OverrideError(f"override {key!r} may contain only title and ship")
        title = raw.get("title", "")
        if not isinstance(title, str):
            raise OverrideError(f"override {key!r} title must be text")
        title = " ".join(title.split())
        if len(title) > 255:
            raise OverrideError(f"override {key!r} title exceeds Shopify's 255 characters")
        ship = raw.get("ship", "")
        if not isinstance(ship, str):
            raise OverrideError(f"override {key!r} ship must be text")
        ship = ship.strip()
        # A field that was set but is blank names that field; an entry that sets nothing at
        # all gets the general message. Both are mistakes worth naming — each reads as a
        # decision somebody made, and each would silently do nothing.
        if "title" in raw and not title:
            raise OverrideError(f"override {key!r} title must not be empty")
        if "ship" in raw and not ship:
            raise OverrideError(f"override {key!r} ship must not be empty")
        if not title and not ship:
            raise OverrideError(f"override {key!r} must set title or ship")
        parts[key] = PartOverride(title=title, ship=ship)
    return CatalogOverrides(parts)
