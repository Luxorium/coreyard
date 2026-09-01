"""One file for a storefront's policy, instead of four.

A site's policy arrived as four separate JSON files, each named by its own environment
variable: what the yard is willing to claim about a part, its packed shipping weights, how
each part ships, and what closes an order. They are read together, validated together and
edited together, and splitting them bought nothing except four more things to get wrong
before CoreYard would run.

``store.json`` holds all four as sections::

    {
      "version": 1,
      "profile":  { ... },
      "weights":  { ... },
      "shipping": { ... },
      "orders":   { ... }
    }

Every section is optional, and an absent one means exactly what an unset file meant: the
neutral default. The section bodies are unchanged, so an existing file can be pasted in
under its section name and nothing else has to move.

**The individual ``STORE_*_FILE`` settings still work and still win.** A site that names one
explicitly gets that file, whatever ``store.json`` says, so upgrading changes nothing until
someone chooses to consolidate. That is deliberate: configuration that silently starts
resolving somewhere else is how a storefront ends up publishing wording nobody reviewed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

SECTIONS = ("profile", "weights", "shipping", "orders")
DEFAULT_NAME = "store.json"

_CACHE: dict[str, dict[str, Any]] = {}


class StoreFileError(RuntimeError):
    """The combined store file is missing, malformed, or not an object."""


def path(explicit: str | Path | None = None) -> Optional[Path]:
    """Where the combined file lives, or None when this site has none.

    A path given in ``STORE_FILE`` must exist — a named file that does not resolve is an
    error rather than a silent fallback, for the same reason the individual settings are.
    The default location is only used when it happens to be there, so an installation that
    has never heard of this file is unaffected.
    """
    from coreyard.config import REPO_ROOT, _get

    named = str(explicit or _get("STORE_FILE", "") or "").strip()
    if named:
        target = Path(named).expanduser()
        if not target.is_file():
            raise StoreFileError(f"STORE_FILE points at {target}, which does not exist.")
        return target
    fallback = REPO_ROOT / DEFAULT_NAME
    return fallback if fallback.is_file() else None


def load(explicit: str | Path | None = None, *, cache: bool = True) -> dict[str, Any]:
    """The file's sections, or an empty mapping when there is no file."""
    target = path(explicit)
    if target is None:
        return {}
    key = str(target.resolve())
    if cache and key in _CACHE:
        return _CACHE[key]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreFileError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StoreFileError(f"{target} must contain a JSON object, not {type(data).__name__}.")
    # "_"-prefixed keys are comments, the same convention schema.example.json uses.
    unknown = {k for k in data if not str(k).startswith("_")} - set(SECTIONS) - {"version"}
    if unknown:
        raise StoreFileError(
            f"{target} has unknown section(s): {', '.join(sorted(unknown))}. "
            f"Expected any of: {', '.join(SECTIONS)}."
        )
    for name in SECTIONS:
        if name in data and not isinstance(data[name], dict):
            raise StoreFileError(f"{target}: section '{name}' must be a JSON object.")
    if cache:
        _CACHE[key] = data
    return data


def body(data: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """A section without its comment keys, or None when nothing is left.

    A section holding only a comment is a placeholder someone left to say where a setting
    would go — it means "not configured", exactly as an absent section does. Treating it as
    configured-but-empty would make the example file fail its own validator.
    """
    if data is None:
        return None
    kept = {k: v for k, v in data.items() if not str(k).startswith("_")}
    return kept or None


def section(name: str, explicit: str | Path | None = None) -> Optional[dict[str, Any]]:
    """One section's body, or None when this site did not supply it."""
    if name not in SECTIONS:
        raise ValueError(f"unknown store section {name!r}; expected {SECTIONS}")
    return body(load(explicit).get(name))


def forget() -> None:
    """Drop the cache. Tests write a file per case and must not see the previous one."""
    _CACHE.clear()


def resolve(name: str, key: str, loader, from_dict):
    """One policy, from its own file if this site names one, else from ``store.json``.

    The explicit file wins, so consolidating is a choice a site makes rather than something
    an upgrade does to it. With neither, ``loader(None)`` supplies the neutral default that
    an unset setting has always meant.
    """
    from coreyard.config import _get

    explicit = _get(key, "") or None
    if explicit:
        return loader(explicit)
    data = section(name)
    return from_dict(data) if data is not None else loader(None)
