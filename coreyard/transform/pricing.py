"""Retail price presentation.

The yard system holds whole-dollar retail prices. Some storefronts want charm
pricing (everything ending in .99) instead. That is a presentation choice, not
source data, so it belongs here in transform rather than in extract — the
database value is never modified.

Enabled per installation with ``STORE_CHARM_PRICES=true``. Off by default, so a
site that has never asked for it sees no change.
"""
from __future__ import annotations

import re
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
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


# --- Reading and writing prices that came from somewhere else ----------------
#
# The two functions below are deliberately *not* part of the charm-pricing path above.
# `retail_str` renders a price for publication and applies this site's presentation policy;
# these render a price for an external system that gave it to us in the first place, and
# applying a storefront rounding rule to a marketplace's own number would silently change
# what a listing costs. Keep the two uses distinct: `retail_str` publishes, `money_str`
# round-trips.

_NOT_MONEY = re.compile(r"[^0-9.\-]")


def parse_money(value) -> Optional[Decimal]:
    """Read a price out of whatever an external system called a price.

    Marketplace and portal APIs return money as display strings — ``"$57.50"``,
    ``"1,299.00"``, an empty cell for "unpriced". Returns None rather than raising for
    anything unreadable, because a single unparseable row must not stop a run over
    thousands of them; the caller decides whether a missing price is fatal.
    """
    if value is None:
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(str(value))
    if isinstance(value, float):
        return Decimal(str(value))
    text = _NOT_MONEY.sub("", str(value))
    if not text or text in {"-", "."}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def money_str(value) -> str:
    """Format a price for a system that insists on two decimal places.

    Always ``45.00``, never ``45``: a validator that rejects a whole number is common
    enough, and the failure it produces is a rejected write rather than a wrong price, so
    it is worth being unconditional about. Does **no** rounding to .99 — see the note
    above.
    """
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"{amount.quantize(Decimal('0.01')):.2f}"
