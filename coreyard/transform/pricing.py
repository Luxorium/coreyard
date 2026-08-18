"""Retail price presentation.

The yard system holds whole-dollar retail prices. Some storefronts want charm
pricing (everything ending in .99) instead. That is a presentation choice, not
source data, so it belongs here in transform rather than in extract — the
database value is never modified.

Enabled per installation with ``STORE_CHARM_PRICES=true``. Off by default, so a
site that has never asked for it sees no change.
"""
from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Optional

from coreyard.config import _get

CENT = Decimal("0.01")
MIN_PRICE = Decimal("0.99")

_TRUE = {"1", "true", "yes", "on"}


def charm_enabled() -> bool:
    """Whether charm pricing is switched on for this installation."""
    return (_get("STORE_CHARM_PRICES", "") or "").strip().lower() in _TRUE


def charm(value: Decimal) -> Decimal:
    """Round to the nearest x.99; ties go up.

    $50.00 -> $49.99 (a penny down), $50.50 -> $50.99 (49c up), $8.00 -> $7.99.
    A value already ending in .99 is returned unchanged. Never returns less
    than $0.99, so a cheap part cannot be rounded down to nothing.
    """
    if value <= MIN_PRICE:
        return MIN_PRICE
    base = value.to_integral_value(rounding=ROUND_FLOOR)
    low = base - CENT
    high = base + Decimal("0.99")
    if low < MIN_PRICE:
        return MIN_PRICE
    return low if (value - low) < (high - value) else high


def retail(value: Optional[Decimal]) -> Optional[Decimal]:
    """The price to publish: charm-rounded when enabled, otherwise untouched."""
    if value is None:
        return None
    return charm(value) if charm_enabled() else value


def retail_str(value: Optional[Decimal], default: str = "") -> str:
    """Formatted price for the CSV and the API, or ``default`` when unpriced."""
    out = retail(value)
    return f"{out:.2f}" if out is not None else default
