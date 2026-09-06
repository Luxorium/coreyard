"""Site merchandising policy: the claims and wording a storefront is willing to make.

CoreYard renders product copy, but it cannot know what is true at your yard. Whether every
part is tested, whether the word "Genuine" is honest, whether "OEM" belongs in a title —
those are business decisions that differ per installation, and stating them for everybody
would put a claim in a shopper's face that the seller never made.

So the render path reads them from here. The defaults are deliberately minimal and
factually safe: they say what CoreYard can know from the data (a used OEM part, removed
from an inventoried donor vehicle) and assert nothing about inspection, testing, or
condition beyond the grade the source system supplied.

An installation supplies its own by pointing ``STORE_PROFILE_FILE`` at a JSON file:

    STORE_PROFILE_FILE=/path/to/catalog-profile.json

Every key is optional; anything absent keeps the neutral default. The file carries text,
never HTML — the renderer escapes what it is given and owns the markup itself, so a
profile can change wording without being able to inject markup into the storefront.

    {
      "condition":        "Used, tested",
      "condition_graded": "Grade {grade}",
      "body_lead":        "Genuine OEM {part_type}, removed from a donor vehicle.",
      "sold_by":          "Sold by {origin}.",
      "availability":     "In stock and tested at {origin}.",
      "availability_plain": "In stock and tested.",
      "fitment_note":     "Verify fitment by year, options, and part numbers.",
      "part_type_notices": {"air bag": "Have this safety component inspected ..."},
      "title_include_oem": false,
      "title_max_models":  4,
      "tags":              ["Used OEM"],
      "tag_vendor":        true,
      "preserved_tag_prefixes": ["ship:"],
      "preserve_namespaced_tags": true,
      "part_types":        {"tail lamp": "Tail Light Lamp Assembly"},
      "metafield_namespace": "abm",
      "audit":             {"min_price": 5, "min_description_chars": 120}
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping


class ProfileError(RuntimeError):
    """The configured profile file is missing, unreadable, or the wrong shape."""


@dataclass(frozen=True)
class AuditPolicy:
    """Thresholds for the catalog audit.

    Expectations, not laws: a yard selling $3 clips wants a different ``min_price`` than one
    selling engines, and an audit that shouts about every one of them gets ignored. All of
    them are numbers or switches so a site can tune the report without editing code.
    """

    min_price: float = 1.0
    min_description_chars: int = 120
    min_tags: int = 3
    min_title_chars: int = 25
    max_title_chars: int = 255
    require_photos: bool = True
    require_alt_text: bool = True
    require_weight: bool = True
    require_seo: bool = True
    require_product_type: bool = True
    require_vendor: bool = True
    require_sku: bool = True
    flag_active_zero_inventory: bool = True
    suspicious_title_words: tuple[str, ...] = ("example product", "test product")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AuditPolicy":
        known = {f for f in cls.__dataclass_fields__}
        unknown = [k for k in data if k not in known]
        if unknown:
            raise ProfileError(
                "unknown key(s) in profile 'audit': " + ", ".join(sorted(unknown))
            )
        values: dict[str, Any] = dict(data)
        if "suspicious_title_words" in values:
            values["suspicious_title_words"] = tuple(
                str(w).strip().lower() for w in values["suspicious_title_words"] if str(w).strip()
            )
        return cls(**values)


@dataclass(frozen=True)
class CatalogProfile:
    """One installation's merchandising policy.

    Only strings, switches and lookup tables. Nothing here reaches the database or the API;
    it decides what the generated copy is allowed to claim.
    """

    # Condition wording. The default states the grade when the source system supplies one and
    # says nothing more when it does not — "Used, tested" asserts a test nobody performed.
    condition: str = "Used"
    condition_graded: str = "Grade {grade}"

    # Opening line of the description. ``{part_type}`` is emphasized by the renderer;
    # ``{origin}`` expands to "Vendor, City" when both are configured.
    body_lead: str = "Used OEM {part_type}, removed from an inventoried donor vehicle."
    sold_by: str = "Sold by {origin}."

    # The meta description's availability clause, with and without a known origin.
    availability: str = "In stock at {origin}."
    availability_plain: str = "In stock."

    fitment_note: str = ("Verify fitment by year, options, and part/casting numbers where "
                         "shown in the photos.")

    # Optional disclosures keyed by the exact shopper-facing product type. These belong to
    # the site rather than CoreYard: whether a safety-critical used component may be sold,
    # and what the seller can honestly say about its history, are business decisions. Exact
    # matching keeps a notice for an "Air Bag" from leaking onto an air-bag sensor or an air
    # suspension part merely because its title contains one of the same words.
    part_type_notices: Mapping[str, str] = field(default_factory=dict)

    # Shown only when a part is published with photographs of the vehicle it came off rather
    # than of itself. ``{donor}`` expands to the donor's year, make, model and stock number.
    # A site that photographs every part never renders this; a site that does must not let a
    # shopper believe the picture is of the part, so the default states it plainly.
    donor_photo_note: str = ("Photographs show the donor vehicle ({donor}). This part was "
                             "not photographed individually.")

    # "OEM" is a strong search term but it eats title characters, and some sellers prefer it
    # out of the customer-facing title. It stays in the tags and description either way.
    title_include_oem: bool = False

    # How many vehicles a title may name, or **0 for "as many as the character budget
    # holds"**. The budget is the destination's: 255 on Shopify, 80 on the listing portal,
    # so one setting fills a long title and still fits a short one.
    #
    # The default is a count rather than the budget because raising it rewrites every
    # multi-vehicle title a site has published, and that is an installation's decision to
    # take deliberately, not something an upgrade should do to a live catalogue. A yard
    # whose fitment catalogue is rich enough to be worth it should set 0: the names it
    # suppresses are the exact long-tail searches ("2009 Saturn Outlook ABS pump") that a
    # salvage listing wins on.
    title_max_models: int = 4

    # The condition grade in the *title*, as a template over ``{grade}`` — "{grade} Grade"
    # renders "A Grade". Empty keeps the grade out of the title; it is published as a
    # metafield and stated in the description either way. Off by default because a grade
    # vocabulary is the yard's own (A/B/C, 1/2/3, or words), and a title is the wrong place
    # to publish a code shoppers at that yard have no key to.
    title_grade: str = ""

    # Donor mileage in the title, for the part types where mileage is the thing a buyer
    # judges the part by. An engine's mileage is most of its value; a door glass's is noise,
    # and the source system records one for both. Source part types, lower-cased; empty
    # means the mileage never reaches a title.
    title_mileage_part_types: tuple[str, ...] = ()

    # The mileage at or below which stating it helps. Above it the number argues against the
    # part, and the yard would rather say nothing than lead with it.
    title_mileage_max: int = 200_000

    # Tags appended to every product, and whether the yard's own name is one of them.
    tags: tuple[str, ...] = ("Used OEM",)
    tag_vendor: bool = True

    # Tags another system owns. See :mod:`coreyard.transform.tags` for the merge rules.
    preserved_tag_prefixes: tuple[str, ...] = ()
    preserve_namespaced_tags: bool = True

    # Part-type expansions layered over the built-in table in ``transform.seo``.
    part_types: Mapping[str, str] = field(default_factory=dict)

    # Namespace for the structured metafields CoreYard publishes (grade, mileage,
    # condition, fitment). A theme reads them as ``product.metafields.<namespace>.<key>``,
    # so it is the site's choice; the default is neutral because "abm" is somebody's
    # initials, not a generic name.
    metafield_namespace: str = "coreyard"

    audit: AuditPolicy = field(default_factory=AuditPolicy)

    def condition_text(self, grade: str | None) -> str:
        """The condition to show a shopper: the graded phrase, or the plain one."""
        if grade:
            return self.condition_graded.format(grade=grade)
        return self.condition

    def lead_text(self, part_type: str, origin: str = "") -> str:
        return self.body_lead.replace("{origin}", origin).replace("{part_type}", part_type)

    def sold_by_text(self, origin: str) -> str:
        return self.sold_by.format(origin=origin) if origin else ""

    def availability_text(self, origin: str) -> str:
        return (self.availability.format(origin=origin) if origin
                else self.availability_plain)

    def part_type_notice(self, part_type: str) -> str:
        """The site's disclosure for this exact rendered product type, if any."""
        wanted = str(part_type or "").strip().casefold()
        for name, notice in self.part_type_notices.items():
            if str(name).strip().casefold() == wanted:
                return str(notice).strip()
        return ""


DEFAULT_PROFILE = CatalogProfile()

# Keys accepted in the JSON file, mapped to the dataclass field they set. Spelled out so an
# unknown key is an error the operator sees now rather than a policy that silently never
# applied.
_TUPLE_KEYS = {"tags", "preserved_tag_prefixes"}
# Tuples matched against source data, so they are folded to the case the matcher uses.
_LOWER_TUPLE_KEYS = {"title_mileage_part_types"}


def from_dict(data: Mapping[str, Any]) -> CatalogProfile:
    """Build a profile from parsed JSON, keeping the neutral default for absent keys."""
    if not isinstance(data, Mapping):
        raise ProfileError("a catalog profile must be a JSON object")
    # "_"-prefixed keys are comments; JSON has none.
    values = {k: v for k, v in data.items() if not str(k).startswith("_")}
    known = set(CatalogProfile.__dataclass_fields__)
    unknown = [k for k in values if k not in known]
    if unknown:
        raise ProfileError(
            "unknown key(s) in catalog profile: " + ", ".join(sorted(unknown)) +
            ". Known keys: " + ", ".join(sorted(known))
        )
    kwargs: dict[str, Any] = {}
    for key, value in values.items():
        if key == "audit":
            kwargs[key] = AuditPolicy.from_dict(value or {})
        elif key in _TUPLE_KEYS:
            kwargs[key] = tuple(str(v).strip() for v in value if str(v).strip())
        elif key in _LOWER_TUPLE_KEYS:
            kwargs[key] = tuple(str(v).strip().lower() for v in value if str(v).strip())
        elif key in {"part_types", "part_type_notices"}:
            kwargs[key] = {str(k).strip().lower(): str(v).strip()
                           for k, v in (value or {}).items() if str(k).strip()}
        else:
            kwargs[key] = value
    return replace(DEFAULT_PROFILE, **kwargs)


def load(path: "str | Path | None") -> CatalogProfile:
    """Read the profile at ``path``; with no path, the neutral defaults.

    A configured-but-missing file is an error rather than a silent fallback: an installation
    that asked for its own wording and got CoreYard's would publish claims it did not write,
    and the difference is invisible in a product listing.
    """
    if not path:
        return DEFAULT_PROFILE
    target = Path(path).expanduser()
    if not target.exists():
        raise ProfileError(
            f"STORE_PROFILE_FILE points at {target}, which does not exist. "
            f"Create it or unset the variable to use CoreYard's neutral defaults."
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{target} is not valid JSON: {exc}") from exc
    return from_dict(data)
