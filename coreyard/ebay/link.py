"""Resolve a portal listing to the yard part it is, without reading its edit form.

The portal's grid carries a ``RNumber`` column and returns it empty, so the R# — the
identity everything else in CoreYard keys on — appeared to be available only from each
listing's edit form. That is one request per listing. Measured on this installation it runs
at roughly forty seconds a form, which is about sixty-six hours for the whole unlisted tab:
not a cost you pay inside a scheduled window, and not a cost worth paying at all to learn
something the grid already implies.

The grid does carry the donor's stock number and the part-type code, and the yard is keyed
by the same two values. Together they identify a part whenever the donor yielded only one
part of that type — which is most of them, because a car has one engine and one
transmission even though it has four wheels and two mirrors.

That key alone is not enough for a part a car carries two of. Measured on the unlisted
tab, every tail lamp, mirror and headlamp listing was ambiguous under it — correctly so,
since the donor yielded a left and a right and picking either would be a coin flip.

There is a second signal for exactly those. The portal ends its own generated title with
the R#, so the identity is in the string after all. It is not *trusted* there: a number
lifted from a title is only accepted when the part it names agrees with the donor stock
number and part type the grid reported independently. Two systems agreeing on three values
is not a guess. Measured across 192 unlisted listings in five part types, 150 carried a
trailing R# and every one of them was confirmed by the donor key, with no contradiction;
the remaining 42 were engines, whose titles carry no R# and which the donor key resolves on
its own because a car has one engine.

So the two rules cover different halves of the catalogue and neither has to be trusted
alone. What survives from the original design is the refusal: a listing this cannot
identify to one part returns nothing rather than a candidate, because a listing retitled
and repriced as the wrong part is a worse outcome than a listing left alone.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable, Optional

from coreyard.models import Part

# The portal's own generated title ends with the R#, after the fitment: "... Fits 10-12
# FUSION 52507". Anchored to the end so a year span, a size or a trim code earlier in the
# string can never be read as an identity.
_TRAILING_R_NUMBER = re.compile(r"(\d{3,9})\s*$")


def key_for(stock_number, part_type_code) -> Optional[tuple[str, str]]:
    stock = str(stock_number or "").strip()
    code = str(part_type_code or "").strip()
    return (stock, code) if stock and code else None


def r_number_in_title(title) -> Optional[str]:
    """The R# the portal put at the end of its own title, if there is one."""
    found = _TRAILING_R_NUMBER.search(str(title or "").strip())
    return found.group(1) if found else None


def confirms(part: Part, row: dict) -> bool:
    """Whether a part agrees with what the grid independently said about the listing.

    Both values or nothing: a part that matches on donor but not on type is not a weaker
    match, it is a different part.
    """
    return (str(part.stock_number or "").strip() == str(row.get("stock_number") or "").strip()
            and str(part.part_type_code or "").strip() == str(row.get("part_type") or "").strip())


def index_parts(parts: Iterable[Part]) -> dict[tuple[str, str], list[Part]]:
    """Group yard parts by (donor stock number, part-type code)."""
    grouped: dict[tuple[str, str], list[Part]] = defaultdict(list)
    for part in parts:
        key = key_for(part.stock_number, part.part_type_code)
        if key:
            grouped[key].append(part)
    return dict(grouped)


def resolve(rows: list[dict], parts: Iterable[Part]) -> tuple[dict[str, Part], list[dict]]:
    """Map ``listing_id -> Part`` for every listing that resolves to exactly one part.

    Returns the resolved map and a record per unresolved listing saying why, so a caller
    can report the gap or fall back to reading those few edit forms.
    """
    parts = list(parts)
    index = index_parts(parts)
    by_r = {str(part.r_number).strip(): part for part in parts}
    resolved: dict[str, Part] = {}
    unresolved: list[dict] = []
    for row in rows:
        listing_id = str(row["listing_id"])
        key = key_for(row.get("stock_number"), row.get("part_type"))
        record = {
            "listing_id": listing_id,
            "stock_number": str(row.get("stock_number") or ""),
            "part_type": str(row.get("part_type") or ""),
            "title": row.get("title") or "",
        }
        # The strongest evidence first: an R# in the title that the grid's own donor and
        # part type confirm. A number that contradicts them is not treated as an R# at all
        # and the donor key decides, which is the same answer as if it had never been there.
        stated = r_number_in_title(row.get("title"))
        candidate = by_r.get(stated) if stated else None
        if candidate is not None and confirms(candidate, row):
            resolved[listing_id] = candidate
            continue
        if key is None:
            unresolved.append({**record, "reason": "listing carries no stock number"})
            continue
        candidates = index.get(key) or []
        if not candidates:
            unresolved.append({**record, "reason": "no yard part for this donor and type"})
        elif len(candidates) > 1:
            unresolved.append({
                **record,
                "reason": f"{len(candidates)} parts share this donor and type",
                "candidates": [str(part.r_number) for part in candidates],
            })
        else:
            resolved[listing_id] = candidates[0]
    return resolved, unresolved


def merge_details(resolved: dict[str, Part], unresolved: list[dict],
                  details: dict[str, dict],
                  parts_by_r: dict[str, Part]) -> tuple[dict[str, Part], list[dict]]:
    """Rescue unresolved listings using edit forms already on hand.

    The forms are never fetched for this — only read if some earlier run cached them — so
    the cheap path stays cheap and an installation that has them loses nothing.
    """
    still: list[dict] = []
    for record in unresolved:
        r_number = str((details.get(record["listing_id"]) or {}).get("r_number") or "").strip()
        part = parts_by_r.get(r_number) if r_number else None
        if part is not None:
            resolved[record["listing_id"]] = part
        else:
            still.append(record)
    return resolved, still
