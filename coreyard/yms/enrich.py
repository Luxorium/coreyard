"""Optional catalogue enrichment: part-type synonyms and donor-vehicle specifics.

Two things the source database already knows that the generated listing does not say.

* **Part-type aliases.** The yard's own part-type table usually carries the alternate names
  the trade uses for the same thing. A shopper searching "taillamp" should find the part
  filed as "Tail Light".
* **Donor-vehicle detail.** Where the source system decoded the donor's VIN it holds the
  engine, transmission, drivetrain, body and trim — far more specific than the
  year/make/model the listing is otherwise built from.

Both are **off by default and must stay that way unless asked for**, because both feed the
rendered product and therefore the stored fingerprint: switching either on re-renders the
catalogue and makes the next sync re-publish every product it touches. That is a deliberate,
scheduled operation, not something an upgrade should trigger on its own. See
``COREYARD_ENRICH`` in ``.env.example``.

Both lookups load their whole (small) table once per run rather than querying per part.
"""

from __future__ import annotations

from typing import Any, Iterable

from coreyard.config import _get
from coreyard.models import VEHICLE_FIELDS, Part
from coreyard.yms import schema
from coreyard.yms.db import query


# An alias no longer than this with no space in it is an internal shorthand, not something
# a shopper would ever type. Real alternate names observed in these tables are phrases
# ("ANTI-LOCK BRAKE PARTS", "HEADLAMP ASSY"); the short entries alongside them are operator
# codes like "ABK", "VIS", "HULK". Publishing those as storefront tags is pure noise, and
# unlike a missing tag it is visible to customers.
_CODE_LIKE_MAX = 4

# Fields where a bare number is a raw source code rather than a fact about the vehicle.
# "Doors: 4" is meaningful; "Drivetrain: 2" is a lookup key nobody outside the yard can read,
# and guessing at its meaning on a customer-facing page is worse than omitting it.
_NUMERIC_IS_A_CODE = tuple(f for f in VEHICLE_FIELDS if f != "doors")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in ("", "NULL", "None", "0") else text


def _is_code_like(alias: str) -> bool:
    return len(alias) <= _CODE_LIKE_MAX and not any(c.isspace() for c in alias)


def enabled() -> bool:
    """Whether this installation has opted into enriched rendering.

    Reads the already-loaded environment rather than loading ``.env`` itself, matching
    ``pricing.charm_enabled``. A render-path gate that pulled in ``.env`` on its own would
    also pull it into every unit test that happens to render a product.
    """
    return (_get("COREYARD_ENRICH", "") or "").strip().lower() in ("1", "true", "yes", "on")


class CatalogEnricher:
    """Attaches aliases and donor-vehicle detail to parts, if the mapping supplies them."""

    def __init__(self, conn: Any, mapping: "schema.SourceSchema | None" = None) -> None:
        self.conn = conn
        self.mapping = mapping or schema.load()
        self._aliases: dict[str, list[str]] | None = None
        self._vehicles: dict[str, dict[str, str]] | None = None

    def _load_aliases(self) -> dict[str, list[str]]:
        if self._aliases is not None:
            return self._aliases
        out: dict[str, list[str]] = {}
        if self.mapping.part_type_aliases:
            for row in query(self.conn, self.mapping.part_type_aliases):
                code = _clean(row.get("part_type_code"))
                alias = _clean(row.get("alias"))
                if not code or not alias or _is_code_like(alias):
                    continue
                bucket = out.setdefault(code, [])
                # Aliases include short internal codes and exact repeats of the description;
                # de-duplicate case-insensitively and keep the source order stable so the
                # fingerprint does not move on its own.
                if alias.lower() not in {a.lower() for a in bucket}:
                    bucket.append(alias)
        self._aliases = out
        return out

    def _load_vehicles(self) -> dict[str, dict[str, str]]:
        if self._vehicles is not None:
            return self._vehicles
        out: dict[str, dict[str, str]] = {}
        if self.mapping.vehicle_details:
            for row in query(self.conn, self.mapping.vehicle_details):
                stock = _clean(row.get("stock_number"))
                if not stock:
                    continue
                detail = {f: _clean(row.get(f)) for f in VEHICLE_FIELDS}
                detail = {
                    k: v for k, v in detail.items()
                    if v and not (k in _NUMERIC_IS_A_CODE and v.isdigit())
                }
                if detail:
                    out[stock] = detail
        self._vehicles = out
        return out

    def attach(self, parts: Iterable[Part]) -> None:
        aliases = self._load_aliases()
        vehicles = self._load_vehicles()
        if not aliases and not vehicles:
            return
        for part in parts:
            if aliases and part.part_type_code is not None:
                found = aliases.get(str(part.part_type_code), [])
                # The part type itself is already a tag; an alias that only repeats it adds
                # nothing but does move the fingerprint.
                part.aliases = [a for a in found
                                if a.lower() != (part.part_type or "").strip().lower()]
            if vehicles and part.stock_number:
                part.vehicle = dict(vehicles.get(str(part.stock_number).strip(), {}))
