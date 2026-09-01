"""Extract listable parts (+ fitment keys) from the source database into `Part` objects.

The one module that knows how a site's database is shaped — and it learns that at runtime
from ``schema.json`` rather than carrying any vendor's table and column names. See
:mod:`coreyard.yms.schema`.

Two hard-won transport details:
* impacket's TDS parser mangles fixed-length **CHAR/NCHAR** columns (returns bytes and can
  truncate a result set), so char columns are wrapped ``RTRIM(CAST(x AS varchar(n)))`` and
  bits ``CAST(x AS int)`` in the schema mapping. Everything comes back as strings and is
  coerced here.
* impacket renders SQL NULL as the literal string ``'NULL'`` — treated as None below.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Optional

from coreyard.config import require_images
from coreyard.models import Part
from coreyard.yms import schema
from coreyard.yms.db import connect, query


# Impacket's parser has quadratic transient allocation within each TDS reply. Some rows
# carry much larger notes/descriptions than average, so keep enough headroom for worst cases.
FETCH_PAGE_SIZE = 250



def is_configured() -> bool:
    """True once this installation has supplied its own ``schema.json``."""
    return schema.is_configured()


def photos_required(mapping: Optional[schema.SourceSchema] = None) -> bool:
    """Whether this installation only lists parts that have at least one photo.

    **The single definition of "listable".** Sync, reconciliation, bulk publishing and the
    catalog audit all ask here rather than each assembling its own SQL condition, because
    when they disagree the storefront ends up in a state no one of them can explain: sync
    publishes an unphotographed part, reconciliation calls it unlistable and archives it,
    and the next sync publishes it again. Which is exactly what happened.

    Off by default, so an upgrade never silently changes which parts an existing
    installation publishes; a site turns it on with ``STORE_REQUIRE_IMAGES=true``.
    """
    if not require_images():
        return False
    mapping = mapping or schema.load()
    if not mapping.supports_images_filter:
        raise schema.SchemaError(
            "STORE_REQUIRE_IMAGES is on, but this mapping has no 'images_filter' "
            "expression, so CoreYard cannot tell which parts have photos. Add one to "
            "schema.json (see schema.example.json) or turn the setting off."
        )
    return True


def build_query(limit: Optional[int] = None, images_only: Optional[bool] = None) -> str:
    mapping = schema.load()
    if images_only is None:
        images_only = photos_required(mapping)
    return mapping.build_query(limit=limit, images_only=images_only)


# --- value coercion (impacket returns everything as str; NULL as the string 'NULL') ---
def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return None if s in ("", "NULL", "None") else s


def _to_int(v: Any) -> Optional[int]:
    s = _clean(v)
    try:
        return int(s) if s is not None else None
    except ValueError:
        return None


def _to_decimal(v: Any) -> Optional[Decimal]:
    s = _clean(v)
    try:
        return Decimal(s) if s is not None else None
    except Exception:
        return None


_LR = {"L": "Left", "R": "Right"}

# The notes column often arrives with a legacy header glued onto the real text, e.g.
#   "Old ID 6565||ENG||2060||10000.001,,,(3.0L, VIN C, 4th digit, VQ30DE)"
# Everything up to the ",,," (or the last "||") is internal bookkeeping; what follows is
# the actual part description — engine size, VIN/option codes, and condition remarks such
# as "GOOD BLOCK BAD CRANK". Dropping the whole field loses all of that.
_NOTE_TAIL = re.compile(r"^.*(?:,,,|\|\|)", re.S)


def _clean_note(value: Any) -> Optional[str]:
    text = _clean(value)
    if not text:
        return None
    if ",,," in text or "||" in text:
        text = _NOTE_TAIL.sub("", text, count=1)
    text = text.strip().strip("()").strip(" ,;|")
    return text or None


def row_to_part(row: dict[str, Any]) -> Part:
    part_type = _clean(row.get("part_type")) or "Auto Part"
    # Side is carried separately, not appended to part_type: it lets the renderer say
    # "Driver Side Left" and keeps part_type matching the expansion table.
    side = _LR.get((_clean(row.get("left_right")) or "").upper())

    part = Part(
        r_number=_clean(row.get("r_number")) or "",
        stock_number=_clean(row.get("stock_number")),
        interchange_number=_clean(row.get("interchange_number")),
        part_type=part_type,
        side=side,
        part_type_code=_to_int(row.get("part_type_code")),
        interchange_code=_clean(row.get("interchange_code")),
        year=_to_int(row.get("year")),
        make=_clean(row.get("make")),
        model=_clean(row.get("model")),
        price=_to_decimal(row.get("price")),
        quantity=_to_int(row.get("quantity")) or 1,
        grade=_clean(row.get("grade")),
        mileage=_to_int(row.get("mileage")),
        location=_clean(row.get("location")),
        description=_clean(row.get("ecom_desc")) or _clean_note(row.get("notes")),
    )
    return part


def _elsewhere():
    """The configured source, when this site's inventory does not come from the database.

    The three entry points below are what the rest of CoreYard has always called, so
    honouring ``COREYARD_SOURCE`` here makes every caller work with a CSV or SQLite export
    without twenty modules learning about sources. Unset — or ``database`` — returns None
    and the original path runs untouched, which is what keeps this change invisible to
    every existing installation.
    """
    from coreyard.config import _get

    spec = str(_get("COREYARD_SOURCE", "") or "").strip()
    if not spec or spec.lower().split(":")[0] == "database":
        return None
    from coreyard import source
    return source.load(spec)


def fetch_parts(limit: Optional[int] = None, images_only: Optional[bool] = None) -> list[Part]:
    """Connect (read-only, over the SMB named pipe) and return listable parts.

    ``images_only`` defaults to this installation's photo policy (see
    :func:`photos_required`); pass a boolean only to override it deliberately.

    Impacket's TDS parser materializes a whole reply and repeatedly slices its remaining
    bytes while decoding rows. A full yard extract therefore has quadratic transient memory
    even though the resulting ``Part`` list is small. Read bounded pages on one connection
    so a large catalogue cannot exhaust the host.
    """
    other = _elsewhere()
    if other is not None:
        return other.parts(limit=limit, images_only=images_only)
    mapping = schema.load()
    if images_only is None:
        images_only = photos_required(mapping)
    maximum = int(limit) if limit else None
    fetched_total = 0
    after: Any = None
    parts: list[Part] = []
    while maximum is None or fetched_total < maximum:
        page_size = (
            min(FETCH_PAGE_SIZE, maximum - fetched_total)
            if maximum else FETCH_PAGE_SIZE
        )
        # A fresh connection per page makes Impacket release the previous reply's token
        # graph and byte buffers. Its named-pipe connection setup is cheap compared with
        # retaining successive 100 MB parser allocations in one long-lived client.
        with connect() as conn:
            rows = query(
                conn,
                mapping.build_page_query(page_size, after, images_only=images_only),
            )
        parts.extend(p for p in (row_to_part(r) for r in rows) if p.is_listable())
        fetched = len(rows)
        if fetched < page_size:
            break
        next_after = rows[-1].get("r_number")
        if next_after is None or str(next_after) == str(after):
            raise RuntimeError("inventory paging did not advance past the last R#")
        after = next_after
        fetched_total += fetched
    return parts


def fetch_parts_by_r_number(r_numbers: list[str]) -> dict[str, Part]:
    """Look up specific parts by R#, whether or not they are still listable.

    ``is_listable`` is deliberately not applied. This exists to answer "where is this part
    and what is it" for a part that has just sold, which is exactly when it stops being
    listable — filtering here would return nothing precisely when it is needed.
    """
    if not r_numbers:
        return {}
    other = _elsewhere()
    if other is not None:
        return other.parts_by_r_number(list(r_numbers))
    sql = schema.load().build_lookup_query(sorted(set(r_numbers)))
    with connect() as conn:
        rows = query(conn, sql)
    parts = (row_to_part(r) for r in rows)
    return {p.uid(): p for p in parts if p.uid()}


def listable_r_numbers(images_only: Optional[bool] = None) -> set[str]:
    """Every R# the yard says may be listed right now — identifiers only.

    The authoritative answer to "what should be on the storefront", and the cheap half of
    reconciliation: it reads one column instead of rendering a catalogue, so asking the yard
    directly costs seconds rather than minutes. Same scope predicate and same photo policy
    as :func:`fetch_parts`, because a reconciliation that used a different definition would
    retire the parts sync had just published.
    """
    other = _elsewhere()
    if other is not None:
        return other.listable_r_numbers(images_only=images_only)
    mapping = schema.load()
    if images_only is None:
        images_only = photos_required(mapping)
    found: set[str] = set()
    after: Any = None
    while True:
        with connect() as conn:
            rows = query(
                conn, mapping.build_identity_page_query(FETCH_PAGE_SIZE, after, images_only)
            )
        for row in rows:
            value = _clean(row.get("r_number"))
            if value:
                found.add(value)
        if len(rows) < FETCH_PAGE_SIZE:
            return found
        next_after = rows[-1].get("r_number")
        if next_after is None or str(next_after) == str(after):
            raise RuntimeError("listable paging did not advance past the last R#")
        after = next_after


if __name__ == "__main__":
    import sys

    lim = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    for p in fetch_parts(limit=lim):
        print(
            f"R#{p.r_number:>10}  Stock #{p.stock_number or '-':<10}  "
            f"Interchange #{p.interchange_number or '-':<12}  ${p.price}  "
            f"{p.fitment_label()} {p.part_type}"
        )
