"""Deterministic SEO content: clean titles, rich descriptions, and fitment tags.

Turns messy source data (ALL-CAPS makes like "CHEVROLET TRUCK", abbreviated part types,
freeform interchange) plus the resolved interchange fitment into:
  * a multi-model, SEO-packed product title,
  * a meta (SEO) title + description,
  * an HTML description with a per-model "Fits" list, condition, warranty, shipping,
  * year/make/model search tags.

No AI, no external calls — pure string work, so it scales to the whole catalog instantly.

The *engine* is generic; the *claims* are not. What a listing may assert — that a part was
tested, that it is genuine, whether "OEM" belongs in a title, what a part type is called in
shopper language — differs per yard, so every one of those strings comes from the store
profile's :class:`~coreyard.profile.CatalogProfile` rather than being written in here. The
defaults are the minimum CoreYard can know from the data. See :mod:`coreyard.profile`.
"""

from __future__ import annotations

import html
import re
from datetime import date
from typing import Optional

from coreyard.config import StoreProfile
from coreyard.models import VEHICLE_FIELDS, Part
from coreyard.profile import DEFAULT_PROFILE, CatalogProfile

TITLE_MAX = 255
META_TITLE_MAX = 60
META_DESC_MAX = 160

# Shopper-facing labels for the optional donor-vehicle detail carried on ``Part.vehicle``.
_VEHICLE_LABELS = {
    "engine": "Engine",
    "transmission": "Transmission",
    "drivetrain": "Drivetrain",
    "body": "Body",
    "trim": "Trim",
    "doors": "Doors",
}

# Common source part-type abbreviations -> fuller, search-friendly names.
_PART_TYPE = {
    "anti-lock brake pts": "ABS Anti Lock Brake Pump Control Module",
    "chassis cont mod": "Chassis Control Module",
    "center cap": "Wheel Center Cap",
    "tail lamp": "Tail Light Lamp Assembly",
    "starter motor": "Engine Starter Motor",
    "side view mirror": "Side View Door Mirror",
    "transmiss,transaxle": "Transmission Transaxle Assembly",
    "headlamp assembly": "Headlight Headlamp Assembly",
    "speedo head/cluster": "Speedometer Instrument Gauge Cluster",
    "eng/motor cont mod": "Engine Control Module ECU ECM PCM",
    "ac compressor": "AC Air Conditioning Compressor",
    "ps pump/motor": "Power Steering Pump",
    "fuse box, engine": "Engine Bay Fuse Box Relay Panel",
    "radio audio": "Radio Stereo Audio Head Unit",
    "fuel filler door": "Fuel Filler Gas Door Lid",
    "temp control": "AC Heater Temperature Climate Control Unit",
    "front door switch": "Front Door Window Master Switch",
    "wheel": "Wheel Rim",
    "air cleaner": "Air Cleaner Intake Filter Box",
    "sun visor": "Sun Visor",
    "coolant reservoir": "Coolant Overflow Reservoir Tank",
    "engine assembly": "Engine Motor Assembly",
    "glove box": "Glove Box Compartment",
    "trans shift assy": "Transmission Gear Shifter Assembly",
    "alternator": "Alternator Generator",
    "fuse box, cabin": "Cabin Interior Fuse Box Relay Panel",
    "int rr view mirror": "Interior Rear View Mirror",
}


# Short words that are words, not acronyms — the <=3 char rule would shout them.
# Three letters or fewer usually means a trim or acronym (CTS, ESV, GT), so _fix_token
# shouts them. These are ordinary words that happen to be short, and shouting them
# leaves "Center CAP" and "SUN Visor" in customer-facing titles.
_NOT_ACRONYM = {
    "VAN", "CAB", "BUS", "WGN", "TRK", "PUP",
    "CAP", "SUN", "BOX", "PAN", "ARM", "FAN", "BAR", "KIT", "SET", "LID",
    "ROD", "PIN", "NUT", "OIL", "GAS", "AIR", "HUB", "TOP", "JAR", "MOD",
    "PUMP", "DOOR",
}


def _fix_token(tok: str) -> str:
    if not tok:
        return tok
    if tok.upper() in _NOT_ACRONYM:
        return tok[:1].upper() + tok[1:].lower()
    if any(c.isdigit() for c in tok):     # 1500, F150, CK1500
        return tok.upper()
    if len(tok) <= 3:                      # CTS, ESV, XL, SS, GT — trims/acronyms
        return tok.upper()
    return tok[:1].upper() + tok[1:].lower()


def _smart_title(s: str) -> str:
    """Title-case each token, treating "-" and "/" as separators.

    Splitting on "/" matters: "S10/S15/SONOMA" is a single whitespace token, so without it
    the has-a-digit rule upper-cases the whole string and "Sonoma" comes out shouting.
    """
    words = []
    for w in s.split():
        for sep in ("-", "/"):
            if sep in w:
                w = sep.join(_fix_token(p) for p in w.split(sep))
                break
        else:
            w = _fix_token(w)
        words.append(w)
    return " ".join(words)


def clean_make(make: Optional[str]) -> Optional[str]:
    if not make:
        return None
    m = re.sub(r"\s+(TRUCK|PICKUP)$", "", make.strip().upper()).strip()
    if "/" in m:                           # "JEEP/PLYMOUTH" -> "Jeep"
        m = m.split("/")[0].strip()
    return _smart_title(m) or None


def clean_model(model: Optional[str]) -> Optional[str]:
    if not model:
        return None
    m = re.sub(r"\s+(PICKUP|TRUCK)$", "", model.strip(), flags=re.I).strip()
    return _smart_title(m) or None


def _policy(store: "StoreProfile | CatalogProfile | None") -> CatalogProfile:
    """Accept a store, a bare profile, or nothing, and return the policy to render under."""
    if store is None:
        return DEFAULT_PROFILE
    if isinstance(store, CatalogProfile):
        return store
    return store.catalog


def expand_part_type(pt: str, store: "StoreProfile | CatalogProfile | None" = None) -> str:
    """Shopper-facing name for a source part type.

    The built-in table covers abbreviations common to this kind of source data; a site adds
    or overrides entries through its profile's ``part_types``, because which wording sells
    a part is a merchandising decision, not a fact about the database.
    """
    key = re.sub(r"\s+", " ", pt.strip().lower())
    overrides = _policy(store).part_types
    if key in overrides:
        return overrides[key]
    if key in _PART_TYPE:
        return _PART_TYPE[key]
    # generic cleanup: title-case, drop trailing " Pts"/" Assy"
    base = re.sub(r"\bAssy\b", "Assembly", pt.strip(), flags=re.I)
    base = re.sub(r"\bPts\b", "Parts", base, flags=re.I)
    return _smart_title(base)


def _cap(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    return (cut[: cut.rfind(" ")] if " " in cut else cut).strip(" -–/,;")


def _looks_like_prose(s: Optional[str]) -> bool:
    """True for text worth showing a shopper.

    ``inventory._clean_note`` already strips the legacy "Old ID ...||...||...,,," header,
    so what arrives here is the real remark — often spec-dense rather than prose
    ("3.0L, VIN C, 4th digit, VQ30DE") and sometimes a condition disclosure
    ("GOOD BLOCK BAD CRANK"). Both must reach the listing, so this only rejects leftover
    internal junk and fragments too short to mean anything.
    """
    if not s or "||" in s or s.lower().startswith(("old id", "id ")):
        return False
    return len(s.split()) >= 2 and sum(c.isalpha() for c in s) >= 3


def _vehicle_label(make: Optional[str], model: Optional[str]) -> str:
    """'Make Model', but avoid 'Isuzu Isuzu I-290' when the model already carries the make.

    The model often carries a shorter form of the make than the make column does, so an
    exact prefix test misses it: make "Mercedes-Benz" against model "Mercedes 450" reads
    as "Mercedes-Benz Mercedes 450". Comparing the first word of each catches those too.
    """
    if make and model:
        if model.lower().startswith(make.lower()):
            return model
        head = re.split(r"[^A-Za-z0-9]+", make, maxsplit=1)[0].lower()
        if head and model.lower().startswith(head + " "):
            return model
    return " ".join(x for x in (make, model) if x)


def _model_labels(part: Part) -> list[str]:
    """Deduped 'Make Model' labels from fitment (falls back to the part's own vehicle)."""
    labels: list[str] = []
    seen: set[str] = set()
    for f in part.fitment:
        lab = _vehicle_label(clean_make(f.make), clean_model(f.model))
        if lab and lab.lower() not in seen:
            seen.add(lab.lower())
            labels.append(lab)
    if not labels:
        lab = _vehicle_label(clean_make(part.make), clean_model(part.model))
        if lab:
            labels.append(lab)
    # A fitment row with no model yields the bare make, which says nothing next to the
    # specific labels beside it — "Volvo, Volvo 70 Series, Volvo 60 Series" spends title
    # space to repeat what the next label already says.
    specific = {l for l in labels if " " in l}
    if specific:
        labels = [l for l in labels
                  if " " in l or not any(s.lower().startswith(l.lower() + " ") for s in specific)]
    return labels


# The yard database marks an open-ended interchange run with a placeholder year rather
# than a null: one that began before the catalogue did is stamped 1940, and one still in
# production is stamped 2030. Printed literally they reach the customer as "1940 thru
# 2030 …", which reads as a broken listing to anyone who knows the parts.
_SENTINEL_START = 1940
_SENTINEL_END = 2027


def _plausible_end(year: int) -> int:
    """Model years run a year ahead of the calendar; past that it is a marker, not a year."""
    return min(year, date.today().year + 1)


def _year_span(part: Part) -> tuple[Optional[int], Optional[int]]:
    starts = [f.year_start for f in part.fitment
              if f.year_start and f.year_start > _SENTINEL_START]
    ends = [f.year_end for f in part.fitment
            if f.year_end and f.year_end < _SENTINEL_END]
    if starts:
        return min(starts), _plausible_end(max(ends or starts))
    if ends:
        return min(ends), _plausible_end(max(ends))
    # Every row was a placeholder — the part's own vehicle is the only real year left.
    return part.year, part.year


def _year_label(y1: Optional[int], y2: Optional[int]) -> str:
    """Compact span with a dash. Body copy only — never a title."""
    if not y1:
        return ""
    return f"{y1}-{y2}" if y2 and y2 != y1 else str(y1)


def _year_tokens(y1: Optional[int], y2: Optional[int]) -> str:
    """Years for a TITLE, with no dash.

    "2010-2011" tokenizes unpredictably and shoppers type years out in full, so short
    spans are listed and long ones use the word "thru".
    """
    if not y1:
        return ""
    if not y2 or y2 == y1:
        return str(y1)
    if y2 < y1:
        y1, y2 = y2, y1
    if y2 - y1 + 1 <= 4:
        return " ".join(str(y) for y in range(y1, y2 + 1))
    return f"{y1} thru {y2}"


# Punctuation that fragments a title for search engines and reads badly in a snippet.
_TITLE_DROP = re.compile(r"[\"'“”‘’()\[\]{}<>|\\/*#~^_+:;!?•]+")
# A period only survives between digits, so "3.0L" keeps its decimal while "4th digit."
# loses its full stop.
_NON_DECIMAL_DOT = re.compile(r"(?<!\d)\.|\.(?!\d)")
_MODEL_HYPHEN = re.compile(r"(?<=[A-Za-z])-(?=\d)")     # F-150 -> F150


def seo_clean(text: str) -> str:
    """Reduce title text to words, digits and single spaces.

    Dashes and slashes matter most: "Pump / Module" reads as a path, and "F-150" splits the
    token shoppers actually search ("F150"). Letter-to-digit hyphens close up; every other
    symbol becomes a space.
    """
    s = (text or "").replace("&", " and ")
    s = re.sub(r"\bw\s*/\s*o\b", "without", s, flags=re.I)
    s = re.sub(r"\bw\s*/\s*", "with ", s, flags=re.I)
    s = _MODEL_HYPHEN.sub("", s)
    s = _TITLE_DROP.sub(" ", s)
    s = _NON_DECIMAL_DOT.sub(" ", s)
    for dash in ("-", "–", "—", ","):
        s = s.replace(dash, " ")
    return re.sub(r"\s+", " ", s).strip()


# High-signal fitment tokens buried in the catalogue's qualifier text.
_DISPLACEMENT = re.compile(r"\b(\d\.\d)\s*L\b", re.I)
_DRIVETRAIN = re.compile(r"\b(4x4|4x2|4WD|2WD|AWD|FWD|RWD)\b", re.I)

_SIDE_PHRASE = {
    "LEFT": "Driver Side Left",
    "L": "Driver Side Left",
    "RIGHT": "Passenger Side Right",
    "R": "Passenger Side Right",
}


def side_phrase(side: Optional[str]) -> str:
    """"Left" -> "Driver Side Left". Shoppers search both wordings, so carry both."""
    return _SIDE_PHRASE.get((side or "").strip().upper(), "")


_SIDE_SHORT = {"LEFT": "Left Driver", "L": "Left Driver",
               "RIGHT": "Right Passenger", "R": "Right Passenger"}


def title_side(part: Part, store: "StoreProfile | CatalogProfile | None" = None) -> str:
    """Side wording for a title, chosen so the word "Side" appears exactly once.

    "Side View Door Mirror" already supplies it, so the side reads "Left Driver" and the
    whole phrase becomes "Left Driver Side View Door Mirror" — every keyword, no repeat.
    Part types without "Side" get the explicit "Driver Side Left" instead.
    """
    key = (part.side or "").strip().upper()
    if not key:
        return ""
    if "side" in expand_part_type(part.part_type, store).lower():
        return _SIDE_SHORT.get(key, "")
    return _SIDE_PHRASE.get(key, "")


def _title_part_type(part: Part, store=None) -> str:
    return seo_clean(expand_part_type(part.part_type, store))


# "from 11/82" / "thru 10/82" — production splits, useless as title keywords.
_DATE_FRAGMENT = re.compile(r"\b(from|thru|through|to)\s+\d{1,2}\s*[/-]\s*\d{2,4}\b", re.I)


# "4th digit" (where the VIN code sits) and bare number groups such as "4 134" carry no
# search value once separated from their context.
_NOISE_PHRASE = re.compile(r"^(?:\d+(?:st|nd|rd|th)\s+digit|\d[\d\s]*)$", re.I)


def _note_phrases(note: str) -> list[str]:
    """Split one qualifier note into clean phrases ("2.3L", "California emissions")."""
    out: list[str] = []
    for chunk in re.split(r"[,;]", _DATE_FRAGMENT.sub(" ", note or "")):
        phrase = seo_clean(chunk)
        if phrase and not _NOISE_PHRASE.match(phrase):
            out.append(phrase)
    return out


_DISPLACEMENT_L = re.compile(r"\b(\d\.\d)\s*L\b", re.I)
_CYLINDERS = re.compile(r"\b(\d{1,2})\s*cyl\b", re.I)
_VIN_CODE = re.compile(r"\bVIN\s+([A-Z0-9])\b", re.I)


def part_spec(part: Part) -> list[str]:
    """Spec terms taken from this part's own source note: "3.0L", "6 Cylinder", "VIN C".

    These describe the physical part in hand, so unlike interchange qualifiers they can be
    stated in the title without ambiguity. Engines are the big win — displacement and VIN
    code are exactly what a buyer searches for.
    """
    text = part.description or ""
    spec: list[str] = []
    m = _DISPLACEMENT_L.search(text)
    if m:
        spec.append(f"{m.group(1)}L")
    m = _CYLINDERS.search(text)
    if m:
        spec.append(f"{m.group(1)} Cylinder")
    m = _VIN_CODE.search(text)
    if m:
        spec.append(f"VIN {m.group(1).upper()}")
    return spec


def title_qualifiers(part: Part, max_phrases: int = 5, max_chars: int = 55) -> list[str]:
    """Spec qualifiers safe to state in the title: engine size, drivetrain, transmission.

    The catalogue qualifies each application, but an interchange often spans several variants.
    A phrase is only asserted here when it holds for **every** application — if one covers
    2.0L and another 2.6L, neither goes in the title, because the title would then be
    claiming a spec this part may not have. Conflicting qualifiers still appear in the
    description, attributed to the specific years they belong to.
    """
    per_app: list[list[str]] = []
    for f in part.fitment or []:
        for a in getattr(f, "applications", None) or []:
            phrases = _note_phrases(getattr(a, "note", "") or "")
            if phrases:
                per_app.append(phrases)
    if not per_app:
        return []

    common = set(p.lower() for p in per_app[0])
    for phrases in per_app[1:]:
        common &= set(p.lower() for p in phrases)
    if not common:
        return []

    chosen: list[str] = []
    used = 0
    for phrase in per_app[0]:                      # keep the source order
        if phrase.lower() not in common:
            continue
        if any(phrase.lower() == c.lower() for c in chosen):
            continue
        if len(chosen) >= max_phrases or used + len(phrase) + 1 > max_chars:
            break
        chosen.append(phrase)
        used += len(phrase) + 1
    return chosen


def build_title(part: Part, store: "StoreProfile | CatalogProfile | None" = None,
                max_models: Optional[int] = None) -> str:
    """e.g. "2008 Ford F150 Left Driver Side View Door Mirror".

    No dashes, slashes or symbols. Whether the word "OEM" appears is the site's call
    (``title_include_oem``): it is a high-intent search term, but it also eats characters
    that could carry another model, and it stays in the tags and description regardless.
    """
    policy = _policy(store)
    if max_models is None:
        max_models = policy.title_max_models
    pt = _title_part_type(part, store)
    side = title_side(part, store)
    years = _year_tokens(*_year_span(part))
    labels = [c for c in (seo_clean(l) for l in _model_labels(part)) if c]
    spec = part_spec(part)
    seen = {w.lower() for w in pt.split()}
    seen |= {w.lower() for term in spec for w in term.split()}
    extra: list[str] = []
    for phrase in title_qualifiers(part):
        kept = [w for w in phrase.split() if w.lower() not in seen]
        if not kept or _NOISE_PHRASE.match(" ".join(kept)):
            continue
        extra.append(" ".join(kept))
        seen |= {w.lower() for w in kept}
    lead = "OEM" if policy.title_include_oem and "oem" not in seen else ""
    tail = " ".join(x for x in (side, lead, pt, " ".join(spec + extra)) if x)
    if not labels:
        return _cap(" ".join(x for x in (years, tail) if x), TITLE_MAX)
    shown = labels[:max_models]
    more = len(labels) - len(shown)
    models = ", ".join(shown) + (f" and {more} more" if more > 0 else "")
    return _cap(" ".join(x for x in (years, models, tail) if x), TITLE_MAX)


def meta_title(part: Part, store: "StoreProfile | CatalogProfile | None" = None) -> str:
    labels = [c for c in (seo_clean(l) for l in _model_labels(part)) if c]
    bits = (_year_tokens(*_year_span(part)), labels[0] if labels else "",
            title_side(part, store), _title_part_type(part, store))
    return _cap(" ".join(x for x in bits if x), META_TITLE_MAX)


def meta_description(part: Part, store: Optional[StoreProfile] = None) -> str:
    store = store or StoreProfile()
    policy = _policy(store)
    pt = expand_part_type(part.part_type, store)
    labels = _model_labels(part)
    fit = ", ".join(labels[:3]) + (" and more" if len(labels) > 3 else "")
    years = _year_tokens(*_year_span(part))
    stock = f" Stock #{part.stock_number}." if part.stock_number else ""
    # What the site is willing to claim about availability and condition, not what CoreYard
    # assumes: not every yard tests every part, and saying so for them would be a lie the
    # seller never told.
    where = f" {policy.availability_text(store.origin())}"
    warranty = f" {store.warranty}." if store.warranty else ""
    lead = " ".join(x for x in ("Used OEM", side_phrase(part.side), pt) if x)
    txt = f"{lead} for {years} {fit}." + where + warranty + stock
    return _cap(txt, META_DESC_MAX)


def _lead_html(policy: CatalogProfile, part_type: str, origin: str) -> str:
    """The description's opening sentence, from site-supplied text.

    The profile carries *text*, never markup: the template is escaped here and the emphasis
    around the part type is added by this function, so a profile can change what a listing
    claims without being able to inject HTML into the storefront.
    """
    text = policy.lead_text(part_type, origin)
    before, marker, after = text.partition(part_type) if part_type else (text, "", "")
    body = (f"{html.escape(before)}<strong>{html.escape(part_type)}</strong>{html.escape(after)}"
            if marker else html.escape(text))
    sold_by = policy.sold_by_text(origin)
    if sold_by:
        body += " " + html.escape(sold_by)
    return f"<p>{body}</p>"


def build_body_html(part: Part, store: Optional[StoreProfile] = None) -> str:
    store = store or StoreProfile()
    policy = _policy(store)
    pt = expand_part_type(part.part_type, store)
    origin = store.origin()
    lines = [_lead_html(policy, pt, origin)]
    if _looks_like_prose(part.description):
        lines.append(f"<p>{html.escape(part.description)}</p>")

    if part.fitment:
        heading = "Fits"
        if part.interchange_number:
            heading += f" Interchange #{part.interchange_number}"
        lines.append(f"<p><strong>{html.escape(heading)}:</strong></p><ul>")
        for f in part.fitment[:40]:
            veh = " ".join(x for x in (f.year_label(), _vehicle_label(clean_make(f.make), clean_model(f.model))) if x)
            # The catalogue qualifies most applications (engine, drivetrain, body, emissions,
            # production date). Those decide whether a part actually fits, so show them.
            quals = f.qualifiers() if hasattr(f, "qualifiers") else []
            if not quals:
                lines.append(f"  <li>{html.escape(veh)}</li>")
                continue
            lines.append(f"  <li>{html.escape(veh)}")
            lines.append("    <ul>")
            for a in quals[:8]:
                lines.append(f"      <li>{html.escape(a.label())}</li>")
            lines.append("    </ul>")
            lines.append("  </li>")
        lines.append("</ul>")
        if policy.fitment_note:
            lines.append(f"<p><em>{html.escape(policy.fitment_note)}</em></p>")

    details = [
        # Donor specifics first: they describe the vehicle the "Fits" list just named, and
        # a shopper checking whether this is the 5.0L is looking for them, not for the
        # bookkeeping identifiers further down. Every one is absent unless the site opted
        # into enrichment, so the rendered block is byte-identical otherwise.
        *((_VEHICLE_LABELS[f], part.vehicle.get(f)) for f in VEHICLE_FIELDS),
        ("Condition", policy.condition_text(part.grade)),
        ("Side", side_phrase(part.side) or None),
        ("Interchange #", part.interchange_number),
        ("Mileage", f"{part.mileage:,} mi" if part.mileage else None),
        ("Warranty", store.warranty or None),
        ("Shipping", f"Ships from {store.city}" if store.city else None),
        ("Stock #", part.stock_number),
        ("R#", part.r_number),
    ]
    lines.append("<ul>")
    for label, value in details:
        if value:
            lines.append(f"  <li><strong>{html.escape(label)}:</strong> {html.escape(str(value))}</li>")
    lines.append("</ul>")
    return "\n".join(lines)


def image_alt(part: Part, index: int = 1,
              store: "StoreProfile | CatalogProfile | None" = None) -> str:
    """Alt text for an uploaded photo.

    Staged media were being created with no alt at all, which costs image search results
    and leaves the listing unreadable to a screen reader. Numbering keeps each photo on a
    product distinct.
    """
    # Screen readers announce alt text in full, so keep it to roughly one sentence
    # rather than the whole multi-model title.
    base = _cap(build_title(part, store), 125) or expand_part_type(part.part_type, store)
    return f"{base} photo {index}" if index > 1 else base


def build_tags(part: Part, store: Optional[StoreProfile] = None, max_tags: int = 120) -> list[str]:
    store = store or StoreProfile()
    policy = _policy(store)
    tags: list[str] = []
    seen: set[str] = set()

    def add(t: Optional[str]) -> None:
        if not t:
            return
        t = re.sub(r"\s+", " ", t).strip().replace(",", " ")
        if t and t.lower() not in seen and len(tags) < max_tags:
            seen.add(t.lower())
            tags.append(t)

    for f in part.fitment or []:
        mk, veh = clean_make(f.make), _vehicle_label(clean_make(f.make), clean_model(f.model))
        add(mk)
        add(veh)
        if f.year_start:
            for y in range(f.year_start, (f.year_end or f.year_start) + 1):
                add(f"{y} {veh}".strip())
    if not part.fitment:
        # No fitment rows, so the part's own vehicle is all there is. Route it through
        # _vehicle_label like the fitment branch above: joining make and model directly
        # doubles a make the model already carries, which is how 180 parts ended up
        # tagged "2019 Dodge Dodge 1500" (make "DODGE TRUCK", model "DODGE 1500 PICKUP").
        mk = clean_make(part.make)
        add(mk)
        add(" ".join(str(x) for x in (part.year, _vehicle_label(mk, clean_model(part.model))) if x))
    for f in part.fitment or []:
        for a in getattr(f, "applications", None) or []:
            note = getattr(a, "note", "") or ""
            for m in _DISPLACEMENT.finditer(note):
                add(f"{m.group(1)}L")
            for m in _DRIVETRAIN.finditer(note):
                add(m.group(1).upper())
    if part.interchange_number:
        add(f"Interchange {part.interchange_number}")
        add(part.interchange_number)
    for term in part_spec(part):
        add(term)
    add(side_phrase(part.side))
    add(expand_part_type(part.part_type, store))
    # Alternate trade names for the same part, so a shopper searching "taillamp" finds the
    # one filed as "Tail Light". Empty unless the site opted into enrichment, which keeps
    # this a no-op — and the fingerprint stable — for everyone who has not.
    for alias in part.aliases:
        add(alias)
    for extra in policy.tags:
        add(extra)
    if policy.tag_vendor:
        add(store.vendor)
    return tags
