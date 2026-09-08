"""Shipping classification: which group a part ships in, and the tag that says so.

A storefront has to tell a shopper *before* checkout that a door is pickup-only, and it
reads that from a tag on the product. Whoever writes that tag owns a promise: get it wrong
and a freight-only engine quotes free ground, or a shippable alternator refuses to ship.

That promise cannot be made twice. It used to be: a storefront script classified parts by
part type from its own table, while CoreYard published the product — so a part was live and
sellable for however long it took the script to run again, wearing whatever tag it had, or
none. Now CoreYard classifies during the publish that creates the product, from the same
file the storefront's delivery profiles are built from, and the tag is part of the rendered
product like any other shopper-visible field.

What CoreYard knows is generic: an ordered list of groups, each with a tag, a rate and a
list of part-type substrings that fall into it, plus one default. What the groups *are* —
which parts are too big to ship, what freight costs — is the site's commercial policy and
lives in the site's own file:

    STORE_SHIPPING_POLICY_FILE=/path/to/freight.json

    {
      "groups": {
        "PICKUP": {"tag": "ship:pickup-only", "price": null, "label": "Pickup only",
                   "handle": "pickup-only", "match": ["door assembly", "glass"],
                   "fulfillment": "yard"},
        "A":      {"tag": "ship:freight-299", "price": "299.99", "label": "Freight",
                   "handle": "freight-oversize", "match": ["engine assembly"]},
        "GROUND": {"tag": "ship:free", "price": "0.00", "label": "Free ground",
                   "handle": "free-ground", "default": true, "fulfillment": "external"}
      },
      "match_order": ["PICKUP", "A"]
    }

Groups are tried in ``match_order`` (or declaration order), first substring hit wins, so a
specific pattern must precede a general one. Exactly one group carries ``default: true`` and
catches everything else.

``fulfillment`` is optional and describes who ships it: ``external`` means another system
buys the label and will close the order out, ``yard`` means nobody else will. It is read by
the order lifecycle sync so that the two cannot disagree about the same group.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

# Who ships a group's parcels. Empty means the site has not said.
EXTERNAL = "external"
YARD = "yard"
_FULFILLMENT = {"", EXTERNAL, YARD}


class ShippingPolicyError(RuntimeError):
    """The configured shipping policy is missing, unreadable, or the wrong shape."""


@dataclass(frozen=True)
class ShippingGroup:
    """One shipping class: how a part in it ships, and the tag that announces it."""

    id: str
    tag: str
    label: str = ""
    handle: str = ""
    price: Optional[str] = None          # None = not shipped at any price
    note: str = ""
    match: tuple[str, ...] = ()
    # Names a pattern would otherwise swallow. Substring matching cannot say "battery but
    # not battery tray", and the two belong in different groups: one is freight, the other
    # goes in a envelope. Ordering cannot settle it either, because the group that should
    # win is the default, and a default carrying patterns is refused for good reason.
    exclude: tuple[str, ...] = ()
    default: bool = False
    fulfillment: str = ""

    @property
    def shippable(self) -> bool:
        return self.price is not None

    def matches(self, *part_types: Optional[str]) -> bool:
        """Whether any of the given part-type spellings falls in this group."""
        candidates = [str(t).lower() for t in part_types if t]
        if any(word in candidate for word in self.exclude for candidate in candidates):
            return False
        return any(pattern in candidate
                   for pattern in self.match for candidate in candidates)


@dataclass(frozen=True)
class ShippingPolicy:
    """An ordered classification table plus the default that catches the rest."""

    groups: tuple[ShippingGroup, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.groups)

    @property
    def default_group(self) -> Optional[ShippingGroup]:
        return next((g for g in self.groups if g.default), None)

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(g.tag for g in self.groups)

    @property
    def owned_prefixes(self) -> tuple[str, ...]:
        """Tag prefixes this policy manages, e.g. ``("ship:",)``.

        Publishing a corrected classification has to *replace* the old one, not sit beside
        it — two ``ship:`` tags on one product and the storefront reads whichever it tests
        for first. So the namespace of the configured tags is declared owned, and the tag
        merge drops stale members of it. See :mod:`coreyard.transform.tags`.
        """
        prefixes = {tag.split(":", 1)[0] + ":" for tag in self.tags if ":" in tag}
        return tuple(sorted(prefixes))

    def by_id(self, group_id: str) -> Optional[ShippingGroup]:
        key = str(group_id).strip().lower()
        return next((g for g in self.groups if g.id.lower() == key), None)

    def by_tag(self, tag: str) -> Optional[ShippingGroup]:
        key = str(tag).strip().lower()
        return next((g for g in self.groups if g.tag.lower() == key), None)

    def classify(self, *part_types: Optional[str]) -> Optional[ShippingGroup]:
        """The group a part belongs to, or None when no policy is configured.

        Both the published product type and the source system's own wording are offered, so
        a site's table keeps working whichever it was written against — the same courtesy
        the weight table gets, and for the same reason.
        """
        if not self.groups:
            return None
        for group in self.groups:
            if group.match and group.matches(*part_types):
                return group
        return self.default_group

    def tag_for(self, *part_types: Optional[str]) -> Optional[str]:
        group = self.classify(*part_types)
        return group.tag if group else None

    @classmethod
    def from_dict(cls, data: Any) -> "ShippingPolicy":
        if not isinstance(data, Mapping):
            raise ShippingPolicyError("a shipping policy must be a JSON object")
        raw_groups = data.get("groups")
        if not isinstance(raw_groups, Mapping) or not raw_groups:
            raise ShippingPolicyError("a shipping policy needs a non-empty 'groups' object")

        order: Sequence[str] = data.get("match_order") or list(raw_groups)
        unknown = [g for g in order if g not in raw_groups]
        if unknown:
            raise ShippingPolicyError(
                "'match_order' names group(s) that do not exist: " + ", ".join(unknown))
        # Anything left out of match_order still exists; it simply cannot win by pattern.
        ordered = list(order) + [g for g in raw_groups if g not in order]

        groups: list[ShippingGroup] = []
        for group_id in ordered:
            body = raw_groups[group_id]
            if not isinstance(body, Mapping):
                raise ShippingPolicyError(f"group {group_id!r} must be an object")
            tag = str(body.get("tag") or "").strip()
            if not tag:
                raise ShippingPolicyError(
                    f"group {group_id!r} has no 'tag'; the tag is what the storefront reads")
            fulfillment = str(body.get("fulfillment") or "").strip().lower()
            if fulfillment not in _FULFILLMENT:
                raise ShippingPolicyError(
                    f"group {group_id!r} has fulfillment {fulfillment!r}; expected "
                    f"{EXTERNAL!r}, {YARD!r}, or omitted")
            price = body.get("price", None)
            if price is not None:
                price = str(price).strip()
                if not price:
                    raise ShippingPolicyError(
                        f"group {group_id!r} has an empty 'price'; use null for "
                        f"'not shipped at any price'")
            # Patterns live on the group. A legacy layout kept them in a top-level array
            # named for the group, which is still read so an existing file keeps working.
            raw_match = body.get("match")
            if raw_match is None:
                raw_match = data.get(group_id) if isinstance(data.get(group_id), list) else []
            match = tuple(str(m).strip().lower() for m in raw_match if str(m).strip())
            exclude = tuple(str(m).strip().lower()
                            for m in (body.get("exclude") or []) if str(m).strip())
            groups.append(ShippingGroup(
                id=str(group_id),
                tag=tag,
                label=str(body.get("label") or "").strip(),
                handle=str(body.get("handle") or "").strip(),
                price=price,
                note=str(body.get("note") or "").strip(),
                match=match,
                exclude=exclude,
                default=bool(body.get("default", False)),
                fulfillment=fulfillment,
            ))

        seen_tags: dict[str, str] = {}
        for group in groups:
            key = group.tag.lower()
            if key in seen_tags:
                raise ShippingPolicyError(
                    f"groups {seen_tags[key]!r} and {group.id!r} share the tag "
                    f"{group.tag!r}; a product would carry two classifications at once")
            seen_tags[key] = group.id
        defaults = [g.id for g in groups if g.default]
        if len(defaults) != 1:
            raise ShippingPolicyError(
                "exactly one group must set \"default\": true (the one that catches "
                f"everything unmatched); found {len(defaults)}"
                + (": " + ", ".join(defaults) if defaults else ""))
        if any(g.default and g.match for g in groups):
            raise ShippingPolicyError(
                "the default group must not also carry 'match' patterns: it already "
                "catches everything the others did not")
        return cls(groups=tuple(groups))


EMPTY = ShippingPolicy()


def load(path: "str | Path | None") -> ShippingPolicy:
    """Read the shipping policy at ``path``; with no path, no classification at all.

    A configured-but-missing file is an error. Silently publishing an unclassified catalogue
    would let a freight-only engine quote free ground at checkout, which is the exact
    failure this file exists to prevent.
    """
    if not path:
        return EMPTY
    # Relative paths name this installation's own files, so they resolve against the
    # data root rather than whatever directory the caller started in.
    from coreyard.config import data_path

    target = data_path(path) or Path(path).expanduser()
    if not target.exists():
        raise ShippingPolicyError(
            f"STORE_SHIPPING_POLICY_FILE points at {target}, which does not exist. "
            f"Create it or unset the variable."
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ShippingPolicyError(f"{target} is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if not str(k).startswith("_")}
    return ShippingPolicy.from_dict(data)
