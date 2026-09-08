"""Reviewed per-part title decisions keyed by CoreYard's stable R#.

Titles still pass through the canonical renderer, so both Shopify sinks publish and
fingerprint the same decision. Prices are deliberately absent: the source database is the
only authority for a part's price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class OverrideError(ValueError):
    """A catalogue override file is unsafe or malformed."""


@dataclass(frozen=True)
class PartOverride:
    title: str = ""


@dataclass(frozen=True)
class CatalogOverrides:
    parts: dict[str, PartOverride]

    def for_r_number(self, value) -> PartOverride:
        return self.parts.get(str(value).strip(), PartOverride())


EMPTY = CatalogOverrides({})


def load(path: str | Path | None) -> CatalogOverrides:
    if not path:
        return EMPTY
    # Resolved against the data root: a relative path in `.env` names one of this
    # installation's files, not one relative to whatever directory the caller happened to
    # start in. See `config.data_path`.
    from coreyard.config import data_path

    source = data_path(path) or Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OverrideError(f"catalog override file not found: {source}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise OverrideError(f"cannot read catalog override file {source}: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {"version", "parts"}:
        raise OverrideError("catalog overrides must contain only 'version' and 'parts'")
    if data.get("version") != 1:
        raise OverrideError("catalog overrides version must be 1")
    raw_parts = data.get("parts")
    if not isinstance(raw_parts, dict):
        raise OverrideError("catalog overrides 'parts' must be an object keyed by R#")
    parts: dict[str, PartOverride] = {}
    for raw_key, raw in raw_parts.items():
        key = str(raw_key).strip()
        if not key:
            raise OverrideError("catalog override contains an empty R#")
        if not isinstance(raw, dict) or set(raw) - {"title"}:
            raise OverrideError(f"override {key!r} may contain only title")
        title = raw.get("title", "")
        if not isinstance(title, str):
            raise OverrideError(f"override {key!r} title must be text")
        title = " ".join(title.split())
        if len(title) > 255:
            raise OverrideError(f"override {key!r} title exceeds Shopify's 255 characters")
        if not title:
            raise OverrideError(f"override {key!r} title must not be empty")
        parts[key] = PartOverride(title=title)
    return CatalogOverrides(parts)
