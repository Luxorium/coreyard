"""Shipping weight resolution from an externally supplied rules table.

A yard system rarely records what a part weighs, and Shopify needs a weight before it can
quote a carrier rate: a 240 lb transmission with no weight prices like an empty box. The
estimates themselves are a site's own work — they depend on how that yard crates and packs
— so CoreYard does not ship a table. It reads one:

    STORE_WEIGHT_RULES_FILE=/path/to/weights.json

The file's shape, all keys optional except ``rules``:

    {
      "unit": "POUNDS",              // GRAMS | KILOGRAMS | OUNCES | POUNDS
      "default": 15,                 // used when no rule matches; omit for "no weight"
      "safety_multiplier": 1.1,      // applied to every match, then rounded up
      "rules": [["engine assembly", 550], ["alternator", 18]]
    }

Rules are matched **in order**, first hit wins, case-insensitively, as a substring of the
part type — so specific patterns must come before general ones ("engine assembly" before
"engine"). Matching is on the part type CoreYard publishes, which is the expanded
storefront wording, falling back to the raw source part type; either spelling can be
written in the table.

Weights are estimates for rate calculation, not scale readings. Biasing them heavy is
deliberate: a carrier that reweighs an under-declared shipment rebills the shipper, which
costs more than the extra postage would.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

# Shopify's WeightUnit enum, plus how many grams one of each is.
GRAMS_PER_UNIT = {
    "GRAMS": 1.0,
    "KILOGRAMS": 1000.0,
    "OUNCES": 28.349523125,
    "POUNDS": 453.59237,
}
DEFAULT_UNIT = "POUNDS"


class WeightRulesError(RuntimeError):
    """The configured weight table is missing, unreadable, or the wrong shape."""


@dataclass(frozen=True)
class Weight:
    """One resolved weight: the value in its own unit, plus the rule that produced it."""

    value: float
    unit: str
    rule: str = ""

    @property
    def grams(self) -> int:
        return int(round(self.value * GRAMS_PER_UNIT[self.unit]))


@dataclass(frozen=True)
class WeightRules:
    """An ordered substring-match table of packed shipping weights."""

    rules: tuple[tuple[str, float], ...] = ()
    unit: str = DEFAULT_UNIT
    default: Optional[float] = None
    safety_multiplier: float = 1.0

    def _pad(self, value: float) -> float:
        """Apply the safety margin and round up: carriers bill whole units, not fractions."""
        return float(math.ceil(value * self.safety_multiplier))

    def lookup(self, *part_types: Optional[str]) -> Optional[Weight]:
        """First rule matching any of the given part-type spellings, else the default.

        Several spellings are accepted because the published product type is the expanded
        storefront wording while the source system's own wording is what a site's table was
        most likely written against. Trying both means a table keeps working whether it was
        built from the catalogue or from the database.
        """
        candidates = [str(t).lower() for t in part_types if t]
        for pattern, value in self.rules:
            if any(pattern in candidate for candidate in candidates):
                return Weight(self._pad(value), self.unit, pattern)
        if self.default is None:
            return None
        return Weight(self._pad(self.default), self.unit, "(default)")

    @classmethod
    def from_dict(cls, data: Any) -> "WeightRules":
        if not isinstance(data, dict):
            raise WeightRulesError("a weight table must be a JSON object")
        unit = str(data.get("unit", DEFAULT_UNIT)).strip().upper()
        if unit not in GRAMS_PER_UNIT:
            raise WeightRulesError(
                f"weight unit {unit!r} is not one of: " + ", ".join(sorted(GRAMS_PER_UNIT))
            )
        raw_rules: Sequence = data.get("rules") or []
        rules: list[tuple[str, float]] = []
        for entry in raw_rules:
            if isinstance(entry, dict):
                pattern, value = entry.get("match"), entry.get("weight")
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                pattern, value = entry
            else:
                raise WeightRulesError(
                    'each rule must be ["substring", weight] or {"match": ..., "weight": ...}'
                )
            pattern = str(pattern or "").strip().lower()
            if not pattern:
                raise WeightRulesError("a weight rule needs a non-empty match string")
            try:
                weight = float(value)
            except (TypeError, ValueError):
                raise WeightRulesError(
                    f"weight for rule {pattern!r} is not a number: {value!r}") from None
            if weight <= 0:
                raise WeightRulesError(f"weight for rule {pattern!r} must be positive")
            rules.append((pattern, weight))
        default = data.get("default")
        if default is not None:
            try:
                default = float(default)
            except (TypeError, ValueError):
                raise WeightRulesError(f"'default' is not a number: {default!r}") from None
            if default <= 0:
                raise WeightRulesError("'default' must be positive when present")
        try:
            safety = float(data.get("safety_multiplier", 1.0))
        except (TypeError, ValueError):
            raise WeightRulesError("'safety_multiplier' must be a number") from None
        if safety <= 0:
            raise WeightRulesError("'safety_multiplier' must be positive")
        return cls(rules=tuple(rules), unit=unit, default=default, safety_multiplier=safety)


EMPTY = WeightRules()


def load(path: "str | Path | None") -> WeightRules:
    """Read the weight table at ``path``; with no path, an empty table that never matches.

    A configured-but-missing file is an error. Falling back to "no weights" would publish a
    catalogue of zero-weight parts and quote every shopper the wrong shipping, which is the
    exact failure the table exists to prevent.
    """
    if not path:
        return EMPTY
    target = Path(path).expanduser()
    if not target.exists():
        raise WeightRulesError(
            f"STORE_WEIGHT_RULES_FILE points at {target}, which does not exist. "
            f"Create it or unset the variable."
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WeightRulesError(f"{target} is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if not str(k).startswith("_")}
    return WeightRules.from_dict(data)
