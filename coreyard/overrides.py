"""Per-part merchandising decisions supplied by an external catalogue workflow.

These are deliberate title and price decisions keyed by CoreYard's stable R#, not a
second renderer.  The canonical renderer consumes them before it creates the one
``RenderedProduct`` fingerprinted and serialized by both Shopify sinks, so a sync cannot
silently revert a researched price or disagree with the stored fingerprint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional


class OverrideError(ValueError):
    """A catalogue override file is unsafe or malformed."""


@dataclass(frozen=True)
class PartOverride:
    title: str = ""
    price: Optional[Decimal] = None


@dataclass(frozen=True)
class CatalogOverrides:
    parts: dict[str, PartOverride]

    def for_r_number(self, value) -> PartOverride:
        return self.parts.get(str(value).strip(), PartOverride())


EMPTY = CatalogOverrides({})


def load(path: str | Path | None) -> CatalogOverrides:
    if not path:
        return EMPTY
    source = Path(path)
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
        if not isinstance(raw, dict) or set(raw) - {"title", "price"}:
            raise OverrideError(f"override {key!r} may contain only title and price")
        title = raw.get("title", "")
        if not isinstance(title, str):
            raise OverrideError(f"override {key!r} title must be text")
        title = " ".join(title.split())
        if len(title) > 255:
            raise OverrideError(f"override {key!r} title exceeds Shopify's 255 characters")
        price = raw.get("price")
        if price in (None, ""):
            amount = None
        else:
            try:
                amount = Decimal(str(price))
            except InvalidOperation as exc:
                raise OverrideError(f"override {key!r} price is not money") from exc
            if not amount.is_finite() or amount <= 0:
                raise OverrideError(f"override {key!r} price must be positive")
            amount = amount.quantize(Decimal("0.01"))
        if not title and amount is None:
            raise OverrideError(f"override {key!r} changes neither title nor price")
        parts[key] = PartOverride(title=title, price=amount)
    return CatalogOverrides(parts)

