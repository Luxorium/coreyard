"""Comparable research for any part type, from the facts the renderer already has.

Engines and wheels each got their own research module, because each was written against
the only thing available at the time: a portal title string, reverse-engineered with rules
that only made sense for that one kind of part. Extending that pattern to the yard's other
141 part types would mean 141 more modules, each with its own notion of what a match is,
and 141 chances for a listing to be priced against the wrong thing.

There is a better source now. The renderer decomposes a part into ``seo.title_segments`` —
its fitment years, the vehicles it fits, the part type in shopper vocabulary, and the spec
that identifies the physical item. That is exactly the description a buyer types into a
search box, and exactly the set of facts a candidate has to agree with to be a comparable.
So the query and the match come from the same decomposition for every part type, and a
type with unusual matching rules adds a :class:`Rule`, not a module.

What stays per-type is genuinely per-type: whether a spec mismatch is fatal (a 5.0L engine
is not a comparable for a 3.5L one, but a mirror's trim code is a weaker signal), and the
price band a part of that kind plausibly sells in.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from functools import lru_cache
from decimal import Decimal
from pathlib import Path
from typing import Optional

from coreyard.config import StoreProfile
from coreyard.ebay.index import EXCLUDED, parse_page, percentile
from coreyard.models import Part
from coreyard.transform import seo
from coreyard.transform.pricing import charm

# Words that appear in almost every salvage listing and so distinguish nothing.
_NOISE = {
    "oem", "used", "genuine", "original", "factory", "assembly", "assy", "complete",
    "fits", "fit", "for", "and", "the", "with", "without", "from", "off", "part",
    "auto", "car", "truck", "vehicle", "replacement", "aftermarket",
}

# A candidate that is a *piece* of the part being sold, or a manual about it, is not a
# comparable at any price — this is the single most common way a search goes wrong.
_NOT_THE_PART = re.compile(
    r"\b(manuals?|brochures?|catalogs?|posters?|stickers?|decals?|repair\s*kits?|"
    r"rebuild\s*kits?|gasket\s*sets?|seal\s*kits?|bracket\s*only|bolts?|screws?|"
    r"clips?|connectors?|pigtails?|harness\s*only|cover\s*only|lens(?:es)?\s*only|"
    r"bulbs?|sockets?|combo\s*kits?)\b", re.I
)

# A broken part sells for what a broken part is worth, which says nothing about the price
# of a working one. Sellers state it plainly and prominently, so this is cheap to honour —
# and expensive not to: a damaged listing sits at the bottom of the comparable set and
# drags the median down for every part priced against it.
_DAMAGED = re.compile(
    r"\b(broken|cracked|damaged|as[-\s]?is|for\s*parts|parts\s*only|not\s*working|"
    r"does\s*not\s*work|doesn.?t\s*work|non[-\s]?working|defective|faulty|"
    r"read\s*description)\b", re.I
)

# Words shared by the shopper vocabulary of many different part types, so agreeing on one
# of them is not agreement about the kind of part: a headlamp and a tail lamp share "light"
# and "lamp", and a candidate matching only on those was pricing one against the other.
_GENERIC_TYPE_WORDS = {
    "light", "lamp", "motor", "module", "control", "unit", "switch", "sensor",
    "panel", "cover", "box", "pump", "valve", "arm", "kit", "set", "housing",
    "front", "rear", "left", "right", "side", "upper", "lower", "inner", "outer",
}

_YEAR = re.compile(r"\b((?:19|20)\d{2})\b")
_WORD = re.compile(r"[A-Za-z0-9.]+")

# A stated physical size, as "17x7", "17x7.5" or "17 x 7-1/2". Where a part type comes
# in sizes, this *is* the part's identity: an 18x8 wheel is not a cheap 17x7 one, it is
# a different product, and a comparable set that mixes them prices neither.
_SIZE = re.compile(r"\b(\d{2})\s*[xX]\s*(\d(?:\.\d+|\s*(?:-\s*)?1/2)?)\b")
# ...and a bare diameter, for candidates that state one without a width.
_DIAMETER = re.compile(r'(?<!\d)(1[4-9]|2[0-2])(?=\s*(?:inch|in\b|["\u201d]))')


@dataclass(frozen=True)
class Rule:
    """How strictly one part type's comparables must agree with the part.

    ``spec_required`` is the important one. For an engine the displacement is the identity
    of the thing — pricing a 3.5L against 5.0L comparables is simply wrong — so a candidate
    that states a *different* spec is rejected. For most parts the spec is a refinement
    that improves the match without being decisive, and rejecting on it would throw away
    the whole comparable set.

    ``dimension_required`` is the same idea for part types sold by size: when the part
    and the candidate both state one, they must agree.

    ``exclude`` is a regex of extra rejections this type needs and no other does — the
    replacement for a per-type research module, and the reason there is only one.

    ``variants`` names attributes that split one part type into products that do not
    price against each other. An alloy wheel is not a dear steel one and an HID lamp is
    not a dear halogen one; each is a different thing a buyer searches for by name.
    Each entry is a group of mutually exclusive regexes, and only a stated
    *disagreement* rejects — a candidate naming no variant is still evidence.
    """

    spec_required: bool = False
    dimension_required: bool = False
    exclude: str = ""
    variants: tuple[tuple[str, ...], ...] = ()
    min_price: float = 15.0
    max_price: float = 12_000.0
    floor_fraction: Decimal = Decimal("0.25")


DEFAULT_RULE = Rule()

# Only where the default is genuinely wrong. Keyed by the part-type code, because the code
# is stable where the yard's display name is not.
RULES: dict[str, Rule] = {
    # Engines: a rebuilt, crated or imported engine sells in a different market than a
    # used one pulled from a donor car, whatever its displacement.
    "300": Rule(spec_required=True, min_price=150.0, max_price=12_000.0,
                exclude=r"\b(jdm|rebuilt|crate\s*engine|core\s*only|"
                        r"non[\s-]?running|lot\s*of|\d\s*pcs?)\b|"
                        r"\bimported?\s+from\s+japan\b"),
    "400": Rule(spec_required=True, min_price=150.0, max_price=8_000.0,
                exclude=r"\b(rebuilt|core\s*only|lot\s*of|\d\s*pcs?)\b"),
    # Wheels are sold by size and by what they are made of.
    "560": Rule(dimension_required=True, min_price=25.0, max_price=900.0,
                variants=((r"alloy|alumin|machined|polished|chrome", r"steel"),)),
}


def rule_for(part: Part) -> Rule:
    return RULES.get(str(part.part_type_code or "").strip(), DEFAULT_RULE)


def _segments(part: Part, store: StoreProfile) -> dict[str, str]:
    return {name: text for name, text, _ in seo.title_segments(part, store, compact=True)}


def words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")
            if len(w) > 1 and w.lower() not in _NOISE}


def years_in(text: str) -> set[int]:
    return {int(y) for y in _YEAR.findall(text or "")}


@lru_cache(maxsize=64)
def _exclusion(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


def variant_of(text: str, group: tuple[str, ...]) -> Optional[int]:
    """Which of a group of mutually exclusive vocabularies this text states, if any."""
    for index, pattern in enumerate(group):
        if _exclusion(pattern).search(text or ""):
            return index
    return None


def norm_size(text: str) -> Optional[str]:
    """A stated size as a canonical "17x7", or None. Handles "7-1/2" as 7.5."""
    match = _SIZE.search(text or "")
    if not match:
        return None
    raw = match.group(2)
    width = f"{raw.strip()[0]}.5" if "1/2" in raw else raw
    try:
        return f"{int(match.group(1))}x{float(width):g}"
    except ValueError:
        return None


def diameters(text: str) -> set[str]:
    """Every diameter the text states, from a full size or a bare measurement."""
    size = norm_size(text)
    if size:
        return {size.split("x")[0]}
    return set(_DIAMETER.findall((text or "").lower()))


def query_for(part: Part, store: StoreProfile, *, broad: bool = False) -> str:
    """The search a buyer would type: year, vehicle, part type, and what identifies it.

    ``broad`` drops the spec and the extra vehicles, for the second pass over parts whose
    specific query matched nothing — the same two-stage shape the engine search uses.
    """
    seg = _segments(part, store)
    pieces = [seg.get("years", ""), seg.get("models", "").split(",")[0].strip(),
              seg.get("part_type", "")]
    if not broad:
        pieces.append(seg.get("spec", ""))
    seen: set[str] = set()
    out: list[str] = []
    for piece in pieces:
        for word in (piece or "").split():
            if word.lower() not in seen:
                seen.add(word.lower())
                out.append(word)
    return " ".join(out)


def score(part: Part, store: StoreProfile, candidate: dict,
          rule: Optional[Rule] = None) -> Optional[tuple[int, dict]]:
    """Rate one candidate, or reject it. Returns ``(points, record)`` or None.

    A comparable has to be the same *kind* of part, for an overlapping *vehicle*, and — for
    part types where the spec is identity rather than detail — the same spec. Everything
    else only moves the score.
    """
    rule = rule or rule_for(part)
    title = str(candidate.get("title") or "")
    price = float(candidate.get("price") or 0)
    if not rule.min_price <= price <= rule.max_price:
        return None
    low = title.lower()
    if EXCLUDED.search(low) or _NOT_THE_PART.search(low) or _DAMAGED.search(low):
        return None
    if rule.exclude and _exclusion(rule.exclude).search(low):
        return None

    seg = _segments(part, store)
    candidate_words = words(title)

    # The kind of part. Without this a search for a tail lamp prices against headlamps.
    type_words = words(seg.get("part_type", ""))
    if type_words:
        if not (type_words & candidate_words):
            return None
        # ...and agreeing only on the words half the catalogue shares is not agreement.
        # "Head Light Headlight Lamp" overlaps a tail lamp on {light, lamp} and is a
        # different part; requiring one of the type's own distinctive words settles it.
        distinctive = type_words - _GENERIC_TYPE_WORDS
        if distinctive and not (distinctive & candidate_words):
            return None
    points = 30

    # The size, where the type has one. Only a stated *disagreement* rejects: a
    # candidate that names no size is weak evidence, not wrong evidence.
    if rule.dimension_required:
        # From the part's own source note, which is where the yard records a size and
        # where :func:`seo.part_spec` reads its other spec terms from. Deliberately not
        # from the rendered segments: the renderer does not put a wheel size in the title
        # today, and teaching it to would re-fingerprint and republish every wheel.
        source_text = str(part.description or "")
        wanted_size = norm_size(source_text)
        wanted_diameters = diameters(source_text)
        candidate_size = norm_size(title)
        if wanted_size and candidate_size and wanted_size != candidate_size:
            return None
        candidate_diameters = diameters(title)
        if wanted_diameters and candidate_diameters and not (wanted_diameters & candidate_diameters):
            return None
        if wanted_diameters & candidate_diameters:
            points += 15

    # The variant, where the type has one. Same rule as the size: only a contradiction
    # rejects, so a candidate that names no variant remains usable evidence.
    for group in rule.variants:
        source_text = str(part.description or "")
        wanted_variant = variant_of(source_text, group)
        candidate_variant = variant_of(title, group)
        if wanted_variant is not None and candidate_variant is not None:
            if wanted_variant != candidate_variant:
                return None
            points += 10

    # The vehicle. A part that fits none of the same years is not evidence.
    wanted_years = years_in(seg.get("years", ""))
    candidate_years = years_in(title)
    if wanted_years and candidate_years:
        if not (wanted_years & candidate_years):
            return None
        points += 20

    vehicle_words = words(seg.get("models", ""))
    overlap = vehicle_words & candidate_words
    if vehicle_words and not overlap:
        return None
    points += min(25, 8 * len(overlap))

    # The spec: decisive for some part types, a bonus for the rest.
    spec_words = words(seg.get("spec", ""))
    if spec_words:
        matched = spec_words & candidate_words
        if rule.spec_required:
            # Only a *contradiction* rejects. A candidate that states no displacement at
            # all is weak evidence, not wrong evidence.
            stated = {w for w in candidate_words if re.fullmatch(r"\d\.\d?l?", w)}
            wanted = {w for w in spec_words if re.fullmatch(r"\d\.\d?l?", w)}
            if wanted and stated and not (wanted & stated):
                return None
        points += min(25, 12 * len(matched))

    if points < 45:
        return None
    return points, {"title": title, "price": round(price, 2), "score": points,
                    "item": candidate.get("item")}


def select(part: Part, store: StoreProfile, candidates: list[dict], *,
           rule: Optional[Rule] = None, limit: int = 14) -> list[dict]:
    """The best distinct comparables for one part, strongest first."""
    rule = rule or rule_for(part)
    scored = []
    for candidate in candidates:
        found = score(part, store, candidate, rule)
        if found:
            scored.append(found[1])
    scored.sort(key=lambda item: (-item["score"], item["price"]))
    seen, unique = set(), []
    for item in scored:
        key = (round(item["price"], 2), item["title"][:40].lower())
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:limit]


def floor_for(part: Part, rule: Optional[Rule] = None) -> Decimal:
    """The lowest price research may propose for this part.

    Anchored to the yard's own retail price rather than a per-part-type table. A table
    would need 143 rows maintained by hand and would still say nothing about *this* part;
    the yard already priced it, and that price is the one number always available for every
    part type. The floor is a fraction of it, so research can find a part is worth less
    than the yard hoped without being able to give it away.
    """
    rule = rule or rule_for(part)
    if part.price and part.price > 0:
        return (Decimal(part.price) * rule.floor_fraction).quantize(Decimal("0.01"))
    return Decimal(str(rule.min_price))


def price_for(part: Part, comps: list[dict], rule: Optional[Rule] = None) -> Decimal:
    """A defensible asking price from the comparables, or the floor when there are none."""
    rule = rule or rule_for(part)
    floor = floor_for(part, rule)
    prices = sorted(float(item["price"]) for item in comps)
    if not prices:
        return floor
    median = statistics.median(prices)
    core = [value for value in prices if median * 0.5 <= value <= median * 1.75]
    target = (percentile(core, 0.30) * 0.97 if len(core) >= 4
              else statistics.median(core or prices) * 0.95)
    landed = charm(Decimal(str(target)), step=Decimal("5"), round_down=True)
    return max(floor, landed)


def confidence(count: int) -> str:
    return "high" if count >= 5 else "medium" if count >= 3 else "low" if count else "none"


@dataclass
class Research:
    r_number: str
    query: str
    comps: list[dict] = field(default_factory=list)
    price: Optional[Decimal] = None
    confidence: str = "none"
    floored: bool = False

    def as_record(self) -> dict:
        return {
            "r_number": self.r_number, "query": self.query,
            "comp_count": len(self.comps),
            "comp_median": (round(statistics.median([c["price"] for c in self.comps]), 2)
                            if self.comps else None),
            "suggested_price": str(self.price) if self.price is not None else "",
            "confidence": self.confidence, "floored": self.floored,
            "evidence": "\n".join(f'${c["price"]:.2f} - {c["title"][:100]}'
                                  for c in self.comps[:4]),
        }


def research_part(part: Part, store: StoreProfile, page: Path | list[Path],
                  rule: Optional[Rule] = None) -> Research:
    """Score an already-fetched index page for one part and price it."""
    rule = rule or rule_for(part)
    pages = [page] if isinstance(page, Path) else list(page)
    candidates = [item for source in pages for item in parse_page(source)]
    comps = select(part, store, candidates, rule=rule)
    price = price_for(part, comps, rule)
    return Research(
        r_number=str(part.r_number), query=query_for(part, store), comps=comps,
        price=price, confidence=confidence(len(comps)),
        floored=price <= floor_for(part, rule),
    )
