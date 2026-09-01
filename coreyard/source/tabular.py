"""A yard's inventory as a CSV file or a SQLite table.

The point of this source is that it needs nothing: no SQL Server, no SMB share, no
``schema.json``, no credentials. A yard on a different management system exports a CSV; a
contributor runs the demo; the test suite exercises real orchestration instead of only pure
functions. All three were impossible while the only way to get a :class:`Part` was the
named pipe.

Columns are named after ``Part`` fields, so the file *is* the mapping and there is nothing
to configure. Unknown columns are ignored, missing ones fall back to the field's default,
and a handful of common spellings are accepted because the whole point is that an export
from somewhere else should work without being rewritten first:

    r_number,part_type,make,model,year,price,quantity,description
    51,TAIL LAMP,Ford,F150,2012,89.00,1,Right side, LED

Photographs follow the same convention as the SMB share: files whose name begins
``<R#>_``. Point ``images_dir`` at a directory (or set ``COREYARD_SOURCE_IMAGES``) and each
part picks up its own, in sorted order. The match is anchored on the underscore so R# 51
cannot collect R# 510's photographs.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

from coreyard.models import Part

_INTS = {"year", "quantity", "part_type_code", "mileage", "weight_grams"}
_DECIMALS = {"price"}
_TEXT = {"r_number", "part_type", "stock_number", "interchange_number",
         "interchange_code", "side", "make", "model", "status", "description",
         "grade", "location", "vin"}
_FIELDS = _INTS | _DECIMALS | _TEXT

# A header is matched with punctuation and case removed, so "R Number", "r_number" and
# "RNumber" all reach the same field without any of them being written down here. That
# matters beyond tidiness: the underscore-free spelling of several of these *is* a real
# system's column name, and CoreYard does not carry another vendor's schema in its tree.
_COMPACT = {field.replace("_", ""): field for field in _FIELDS}

# Genuinely different words an export might use. Keyed by the same compact form.
ALIASES = {
    "sku": "r_number", "id": "r_number", "type": "part_type",
    "stock": "stock_number", "qty": "quantity", "cost": "price",
    "amount": "price", "notes": "description", "note": "description",
    "miles": "mileage", "odometer": "mileage", "vehicleyear": "year",
    "vehiclemake": "make", "vehiclemodel": "model", "condition": "grade",
}


def _canonical(name: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return _COMPACT.get(key) or ALIASES.get(key, key)


def _int(value: Any) -> Optional[int]:
    text = str(value if value is not None else "").strip()
    if not text or text.upper() == "NULL":
        return None
    try:
        return int(float(text.replace(",", "")))
    except ValueError:
        return None


def _decimal(value: Any) -> Optional[Decimal]:
    text = str(value if value is not None else "").strip().replace("$", "").replace(",", "")
    if not text or text.upper() == "NULL":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _text(value: Any) -> Optional[str]:
    text = str(value if value is not None else "").strip()
    return None if not text or text.upper() == "NULL" else text


def row_to_part(row: dict[str, Any], images: Optional[list[str]] = None) -> Optional[Part]:
    """One row as a :class:`Part`, or None when it carries no usable identity."""
    values: dict[str, Any] = {}
    for name, raw in row.items():
        field = _canonical(str(name))
        if field not in _FIELDS:
            continue
        if field in _INTS:
            values[field] = _int(raw)
        elif field in _DECIMALS:
            values[field] = _decimal(raw)
        else:
            values[field] = _text(raw)
    r_number = values.pop("r_number", None)
    part_type = values.pop("part_type", None)
    if not r_number:
        return None
    values = {k: v for k, v in values.items() if v is not None}
    return Part(r_number=str(r_number), part_type=part_type or "", images=images or [],
                **values)


class TabularSource:
    """Parts from a CSV file or a SQLite database, with photographs from a directory."""

    def __init__(self, path: str | Path, *, images_dir: str | Path | None = None,
                 table: str = "parts") -> None:
        self.path = Path(path).expanduser()
        self.table = table
        if images_dir is None:
            from coreyard.config import _get
            configured = _get("COREYARD_SOURCE_IMAGES", "")
            images_dir = configured or None
        self.images_dir = Path(images_dir).expanduser() if images_dir else None

    # -- reading -----------------------------------------------------------------

    def _rows(self) -> Iterable[dict[str, Any]]:
        if not self.path.is_file():
            raise FileNotFoundError(f"source file not found: {self.path}")
        if self.path.suffix.lower() in {".sqlite3", ".sqlite", ".db"}:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                for row in connection.execute(f"SELECT * FROM {self.table}"):
                    yield dict(row)
            finally:
                connection.close()
            return
        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            yield from csv.DictReader(handle)

    def _images_for(self, r_number: str) -> list[str]:
        """Photographs whose name begins ``<R#>_``, sorted.

        Anchored on the underscore for the same reason the SMB reader is: an unanchored
        match lets R# 51 collect R# 510's photographs, and the wrong picture on a listing
        is indistinguishable from a wrong part.
        """
        if not self.images_dir or not self.images_dir.is_dir():
            return []
        return sorted(str(found) for found in self.images_dir.glob(f"{r_number}_*")
                      if found.is_file())

    def _parts(self) -> Iterable[Part]:
        for row in self._rows():
            listed = _text(row.get("images")) if "images" in row else None
            explicit = [piece.strip() for piece in listed.replace("|", ";").split(";")
                        if piece.strip()] if listed else None
            row_id = _text(row.get("r_number")) or _text(row.get("R#")) or ""
            part = row_to_part(row, explicit)
            if part is None:
                continue
            if explicit is None:
                part.images = self._images_for(part.r_number or row_id)
            yield part

    # -- the Source protocol -----------------------------------------------------

    def parts(self, limit: Optional[int] = None,
              images_only: Optional[bool] = None) -> list[Part]:
        found: list[Part] = []
        for part in self._parts():
            if not part.is_listable():
                continue
            if images_only and not part.images:
                continue
            found.append(part)
            if limit and len(found) >= limit:
                break
        return found

    def parts_by_r_number(self, r_numbers: list[str]) -> dict[str, Part]:
        wanted = {str(value).strip() for value in r_numbers}
        return {part.r_number: part for part in self._parts()
                if part.r_number in wanted}

    def listable_r_numbers(self, images_only: Optional[bool] = None) -> set[str]:
        return {part.r_number for part in self.parts(images_only=images_only)}

    def server_now(self) -> datetime:
        """The file's own modification time — the only "source clock" a file has."""
        return datetime.fromtimestamp(self.path.stat().st_mtime)

    def ping(self) -> str:
        count = sum(1 for _ in self._parts())
        return f"{self.path} - {count} rows"
