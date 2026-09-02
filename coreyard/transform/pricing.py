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


ONE_DOLLAR = Decimal("1")


def charm(value: Decimal, *, step: Decimal = ONE_DOLLAR,
          round_down: bool = False) -> Decimal:
    """Land a price a penny under a round number.

    Every storefront CoreYard publishes to wants this, and each used to compute it for
    itself — the catalogue rounded to the nearest dollar, marketplace research rounded down
    to a five-dollar grid, and its offline fallback rounded down to the dollar. Three
    spellings of one idea meant the same decision could reach two storefronts as two
    different numbers, so the idea lives here once and the differences are arguments.

    ``step`` is the grid to sit under: $1 gives $49.99, $5 gives $134.99. ``round_down``
    never rounds a price up, which is what a price derived from *comparables* wants — the
    market cleared at that number, and rounding past it invents evidence. Presenting a
    retail price the yard already set has no such constraint, so it rounds to the nearest.

    $50.00 -> $49.99 (a penny down), $50.50 -> $50.99 (49c up), $8.00 -> $7.99.
    A value already ending in .99 on the grid is returned unchanged. Never returns less
    than $0.99, so a cheap part cannot be rounded down to nothing.
    """
    if value <= MIN_PRICE:
        return MIN_PRICE
    grid = step if step > 0 else ONE_DOLLAR
    base = (value / grid).to_integral_value(rounding=ROUND_FLOOR) * grid
    low = base - CENT
    high = base + grid - CENT
    if low < MIN_PRICE:
        return MIN_PRICE if round_down else min(high, max(MIN_PRICE, high))
    if round_down:
        return low
    return low if (value - low) < (high - value) else high


def retail_step() -> Decimal:
    """The grid a published price sits under. ``STORE_PRICE_STEP`` overrides.

    A dollar grid gives $49.99 but also $67.99 and $12.99. A site that wants every price to
    read as a considered number rather than a converted one sets five, and gets $64.99 and
    $14.99 instead. One is not tidier than the other in principle — it is a merchandising
    choice, so it is configuration, and the default stays a dollar so no existing
    installation's prices move on upgrade.
    """
    raw = (_get("STORE_PRICE_STEP", "") or "").strip()
    if not raw:
        return ONE_DOLLAR
    try:
        step = Decimal(raw)
    except InvalidOperation:
        return ONE_DOLLAR
    return step if step > 0 else ONE_DOLLAR


def retail(value: Optional[Decimal]) -> Optional[Decimal]:
    """The price to publish: charm-rounded when enabled, otherwise untouched."""
    if value is None:
        return None
    return charm(value, step=retail_step()) if charm_enabled() else value


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
