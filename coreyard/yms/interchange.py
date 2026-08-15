"""Resolve full fitment for a part from the source system's application catalogue.

A part's part-type code + fitment key look up rows describing every vehicle the part fits,
each a line like::

    ACADIA 08 w/variable assist power steering (opt NV7), valve ID 25840323
    ENCLAVE 09-11 w/variable assist power steering (opt NV7)

We parse each into (model, year range, qualifier), map the model to its make, and
consolidate by make+model. The queries themselves come from the site's ``schema.json`` —
this project ships no vendor catalogue names. Sites without a fitment catalogue simply get
no fitment, and listings fall back to the donor vehicle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from coreyard.yms import schema as schema_module
from coreyard.yms.db import query

_YEAR_RANGE = re.compile(r"\b((?:19|20)?\d{2})\s*-\s*((?:19|20)?\d{2})\b")
_YEAR_ONE = re.compile(r"\b((?:19|20)?\d{2})\b")


def _to_year(tok: str) -> Optional[int]:
    n = int(tok)
    if n >= 1900:
        return n
    return 2000 + n if n <= 30 else 1900 + n  # 2-digit pivot: 00-30 -> 2000s


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return None if s in ("", "NULL", "None") else s


def _tidy_note(note: str) -> str:
    """Normalize an application's qualifier text ("4x4, thru 12/82, Federal").

    These qualifiers are what make a fitment claim precise — engine size, drivetrain,
    body style, emissions market, production date splits — so they are kept verbatim
    apart from whitespace/punctuation cleanup and a length cap.
    """
    cleaned = re.sub(r"\s+", " ", (note or "").strip()).strip(" ,;-")
    return cleaned[:90].rstrip(" ,;-")


@dataclass(frozen=True)
class Application:
    """One catalogue application row: a year span plus the options that qualify it."""

    year_start: Optional[int]
    year_end: Optional[int]
    note: str = ""

    def year_label(self) -> str:
        if not self.year_start:
            return ""
        if self.year_end and self.year_end != self.year_start:
            return f"{self.year_start}-{self.year_end}"
        return str(self.year_start)

    def label(self) -> str:  # "1986-1987 — 2.3L, California"
        years = self.year_label()
        if years and self.note:
            return f"{years} — {self.note}"
        return years or self.note


@dataclass
class Fitment:
    make: Optional[str]
    model: str
    year_start: Optional[int]
    year_end: Optional[int]
    # Every application that merged into this make/model, each keeping its own year span
    # and qualifiers. The merged span above stays the headline used by titles and tags.
    applications: list = field(default_factory=list)

    def year_label(self) -> str:
        if not self.year_start:
            return ""
        if self.year_end and self.year_end != self.year_start:
            return f"{self.year_start}-{self.year_end}"
        return str(self.year_start)

    def label(self) -> str:  # "2008-2011 GMC Acadia"
        bits = [self.year_label(), self.make, self.model]
        return " ".join(b for b in bits if b).strip()

    def qualifiers(self) -> list:
        """Applications that actually carry qualifier text, in source order."""
        return [a for a in self.applications if getattr(a, "note", "")]


def parse_application(app: str) -> Optional[dict]:
    """Split a raw Application string into {model, y1, y2, note}. Model is the text before
    the first year token; a 2- or 4-digit year (optionally a range) follows; rest is a note."""
    app = app.strip()
    m = _YEAR_RANGE.search(app) or _YEAR_ONE.search(app)
    if not m:
        return {"model": app.rstrip(" ,;"), "y1": None, "y2": None, "note": ""}
    model = app[: m.start()].strip().rstrip(" ,;")
    if not model:
        return None
    y1 = _to_year(m.group(1))
    y2 = _to_year(m.group(2)) if m.lastindex and m.lastindex >= 2 and m.group(2) else y1
    if y1 and y2 and y2 < y1:
        y1, y2 = y2, y1
    return {"model": model, "y1": y1, "y2": y2, "note": app[m.end():].strip(" ,;")}


class InterchangeResolver:
    """Loads the model->make map once, then resolves fitment per part on the same connection."""

    def __init__(self, conn: Any, source=None) -> None:
        self.conn = conn
        self.schema = source or schema_module.load()
        self._make_of = self._load_make_map()
        self._fitment_cache: dict[tuple[int, str], list[Fitment]] = {}

    def _load_make_map(self) -> dict[str, str]:
        if not self.schema.interchange_makes:
            return {}
        rows = query(self.conn, self.schema.interchange_makes)
        out: dict[str, str] = {}
        for r in rows:
            model, make = _clean(r["model"]), _clean(r["make"])
            if model and make and model not in out:
                out[model] = make
        return out

    def _applications(self, part_type_code: int, intch_nbr: str) -> list[str]:
        if not self.schema.interchange_applications:
            return []
        # Values are interpolated into SQL, so constrain them: the part-type code is an int
        # and the fitment key is restricted to the characters these keys actually use.
        key = re.sub(r"[^A-Za-z0-9_-]", "", str(intch_nbr))
        sql = self.schema.interchange_applications.format(
            part_type_code=int(part_type_code), interchange_code=key
        )
        rows = query(self.conn, sql)
        return [a for a in (_clean(r["app"]) for r in rows) if a]

    def fitment_for(self, part) -> list[Fitment]:
        if not part.part_type_code or not part.interchange_code:
            return []
        cache_key = (part.part_type_code, part.interchange_code)
        if cache_key in self._fitment_cache:
            return self._fitment_cache[cache_key]
        merged: dict[tuple, dict] = {}
        for app in self._applications(part.part_type_code, part.interchange_code):
            parsed = parse_application(app)
            if not parsed or not parsed["model"]:
                continue
            model_raw = parsed["model"]
            make = self._make_of.get(model_raw.upper())
            key = (make, model_raw.upper())  # merge case-insensitively
            y1, y2 = parsed["y1"], parsed["y2"]
            entry = merged.get(key)
            if entry is None:
                # keep raw model text for display cleanup
                entry = merged[key] = {"y1": y1, "y2": y2, "model": model_raw, "apps": []}
            else:
                if y1 and (entry["y1"] is None or y1 < entry["y1"]):
                    entry["y1"] = y1
                if y2 and (entry["y2"] is None or y2 > entry["y2"]):
                    entry["y2"] = y2
            # The merged span is the headline; each application keeps its own years and
            # qualifiers so the listing can state exactly which variant fits.
            application = Application(
                year_start=y1, year_end=y2, note=_tidy_note(parsed["note"])
            )
            if application not in entry["apps"]:
                entry["apps"].append(application)
        fits = [
            Fitment(
                make=k[0],
                model=v["model"],
                year_start=v["y1"],
                year_end=v["y2"],
                applications=v["apps"],
            )
            for k, v in merged.items()
        ]
        fits.sort(key=lambda f: (-((f.year_end or 0) - (f.year_start or 0)), f.year_start or 0, f.make or "", f.model))
        self._fitment_cache[cache_key] = fits
        return fits

    def attach(self, parts) -> None:
        """Populate ``part.fitment`` for each part (in place)."""
        for part in parts:
            part.fitment = self.fitment_for(part)
