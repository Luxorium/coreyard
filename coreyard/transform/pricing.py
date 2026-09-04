"""Parse and format money without changing the source-system amount."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

_NOT_MONEY = re.compile(r"[^0-9.\-]")


def parse_money(value) -> Optional[Decimal]:
    """Read a price out of whatever an external system called a price.

    Portal APIs return money as display strings — ``"$57.50"``,
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
    """Format source-system money with the two decimal places storefronts require.

    Always ``45.00``, never ``45``: a validator that rejects a whole number is common
    enough, and the failure it produces is a rejected write rather than a wrong price, so
    it is worth being unconditional about. This performs no merchandising adjustment.
    """
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return f"{amount.quantize(Decimal('0.01')):.2f}"
