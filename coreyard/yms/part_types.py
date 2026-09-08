"""The catalogue of every part type this yard can inventory, and what it covers.

Two workers add parts all day, and nothing stops them filing one under a type nobody has
published before. The set of types therefore has to come from somewhere rather than from a
list somebody remembered to update.

It comes from the source database. The yard system keeps its own part-type table and the
extract already joins it for each part's display name, so reading the whole catalogue is
one ``SELECT`` — and it lists the types that have no products yet, which is exactly where
the automation is blind.

The second half of this module is the question worth asking of that catalogue: for how
many of these types does the renderer actually know a shopper's word?  ``expand_part_type``
falls back to title-casing the source abbreviation, so an uncovered type still lists — it
just lists as "Whl Cyl" instead of "Wheel Cylinder".  That is a title nobody searches for,
and until now nothing counted them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.transform import seo
from coreyard.yms import schema
from coreyard.yms.db import query


@dataclass(frozen=True)
class PartType:
    """One row of the source system's part-type table."""

    code: str
    name: str


@dataclass(frozen=True)
class Coverage:
    """What CoreYard knows and can say about one part type."""

    code: str
    name: str            # the source system's own description
    shopper_name: str    # what the renderer would put in a title
    parts: int           # parts carrying this code in the extract that was measured
    named: bool          # a curated expansion table names it, rather than generic cleanup
    in_catalogue: bool   # the source part-type table lists this code
    abbreviations: tuple[str, ...] = ()   # shouted tokens left in ``shopper_name``

    @property
    def needs_wording(self) -> bool:
        """A stocked type whose title still carries an abbreviation nobody expanded.

        Deliberately *not* "absent from the curated table". Most part types need no
        curation: "CALIPER" title-cases to "Caliper", which is the word a shopper types.
        Counting those as gaps produced a number too large to act on and mostly wrong —
        116 of 143 types, when the ones that actually list badly were a handful.
        """
        return self.parts > 0 and bool(self.abbreviations)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in ("", "NULL", "None") else text


def catalogue(conn: Any, mapping: Optional[schema.SourceSchema] = None) -> list[PartType]:
    """Every part type the source system defines, ordered by code.

    Read-only, like every path here except the opt-in order write.
    """
    mapping = mapping or schema.load()
    if not mapping.part_types:
        raise schema.SchemaError(
            "this mapping has no 'part_types' query, so CoreYard cannot enumerate the "
            "part types the yard can inventory. Add one (see "
            f"{schema.EXAMPLE_PATH.name}) or pass a catalogue file."
        )
    seen: dict[str, PartType] = {}
    for row in query(conn, mapping.part_types):
        code = _clean(row.get("part_type_code"))
        if code and code not in seen:
            seen[code] = PartType(code=code, name=_clean(row.get("part_type")))
    return sorted(seen.values(), key=lambda item: (len(item.code), item.code))


def coverage(types: Iterable[PartType], parts: Iterable[Part],
             store: StoreProfile | None = None) -> list[Coverage]:
    """Join the catalogue with an extract and the renderer's vocabulary.

    Pure, so the reporting can be tested without a database.  A code the extract carries
    but the catalogue does not name is reported too, rather than dropped: it means the two
    systems disagree about what a part type is, and that is worth seeing.
    """
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for part in parts:
        code = _clean(part.part_type_code)
        if not code:
            continue
        counts[code] = counts.get(code, 0) + 1
        names.setdefault(code, _clean(part.part_type))
    known = {item.code: item for item in types}
    rows: list[Coverage] = []
    for code in sorted(set(known) | set(counts), key=lambda c: (len(c), c)):
        entry = known.get(code)
        # The catalogue's own description is authoritative; the extract's copy of it is
        # the fallback for a code the catalogue has never heard of.
        source_name = (entry.name if entry else "") or names.get(code, "")
        shopper_name = seo.expand_part_type(source_name, store) if source_name else ""
        rows.append(Coverage(
            code=code,
            name=source_name,
            shopper_name=shopper_name,
            parts=counts.get(code, 0),
            named=bool(source_name) and seo.has_expansion(source_name, store),
            in_catalogue=entry is not None,
            abbreviations=tuple(seo.residual_abbreviations(shopper_name)),
        ))
    return rows


# --------------------------------------------------------------------- reporting ---
def render(rows: list[Coverage], *, gaps_only: bool = False) -> list[str]:
    """The report, as lines. Pure, so its shape is a test rather than a screenshot."""
    shown = [row for row in rows if row.needs_wording] if gaps_only else rows
    stocked = [row for row in rows if row.parts]
    lines = [
        f"{'CODE':>6}  {'PARTS':>6}  {'SOURCE NAME':<24}  SHOPPER WORDING",
        f"{'-' * 6}  {'-' * 6}  {'-' * 24}  {'-' * 34}",
    ]
    for row in sorted(shown, key=lambda item: (-item.parts, item.code)):
        mark = "*" if row.needs_wording else " "
        name = (row.name or "(unnamed)")[:24]
        note = "" if row.in_catalogue else "  [not in the part-type table]"
        lines.append(f"{row.code:>6}  {row.parts:>6}  {name:<24}  "
                     f"{mark}{row.shopper_name}{note}")
    gaps = [row for row in rows if row.needs_wording]
    lines.append("")
    lines.append(f"{len(rows)} part types defined; {len(stocked)} with parts in stock.")
    if gaps:
        # The count is the point: it is the number of part types whose listings carry a
        # title no shopper searches for, and it was previously nobody's number.
        covered = sum(row.parts for row in gaps)
        lines.append(
            f"{len(gaps)} stocked type(s), {covered} part(s), still list under an "
            f"unexpanded abbreviation (marked *). Add wording in the profile's "
            f"`part_types`, or an expansion in transform/seo.py if it is vocabulary "
            f"rather than merchandising."
        )
    else:
        lines.append("Every stocked part type renders as words a shopper would type.")
    return lines


def add_arguments(ap):
    ap.add_argument("--catalogue", help="part-type JSON instead of reading the database")
    ap.add_argument("--parts", help="yard rows JSON instead of reading the database")
    ap.add_argument("--gaps", action="store_true",
                    help="list only stocked types with no shopper wording")
    ap.add_argument("--out", help="also write the coverage report as JSON")
    ap.set_defaults(func=run)
    return ap


def run(args) -> int:
    import json
    from pathlib import Path

    from coreyard.config import load_env, load_store

    load_env()
    store = load_store()

    if args.catalogue:
        types = [PartType(code=str(item["code"]), name=str(item.get("name") or ""))
                 for item in json.loads(Path(args.catalogue).read_text(encoding="utf-8"))]
    else:
        from coreyard.yms.db import connect
        with connect() as conn:
            types = catalogue(conn)
    if args.parts:
        from coreyard.yms.inventory import row_to_part
        parts = [row_to_part(item)
                 for item in json.loads(Path(args.parts).read_text(encoding="utf-8"))]
    else:
        from coreyard.yms.inventory import fetch_parts
        parts = fetch_parts()

    rows = coverage(types, parts, store)
    print("\n".join(render(rows, gaps_only=args.gaps)))
    if args.out:
        payload = [{"code": row.code, "name": row.name,
                    "shopper_name": row.shopper_name, "parts": row.parts,
                    "named": row.named, "in_catalogue": row.in_catalogue,
                    "abbreviations": list(row.abbreviations)}
                   for row in rows]
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"  -> {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="coreyard part-types",
        description="Every part type this yard can inventory, and whether the renderer "
                    "has a shopper's word for it. Read-only.",
    )
    args = add_arguments(ap).parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
