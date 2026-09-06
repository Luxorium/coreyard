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
    "radiators": "Radiator",
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
    "VAN", "CAB", "CAR", "BUS", "WGN", "TRK", "PUP",
    "CAP", "SUN", "BOX", "PAN", "ARM", "FAN", "BAR", "KIT", "SET", "LID",
    "ROD", "PIN", "NUT", "OIL", "GAS", "AIR", "HUB", "TOP", "JAR", "MOD",
    "BAG", "KEY", "CAM", "TIE", "RIM", "JACK",
    "PUMP", "DOOR",
}

# Abbreviations the source part-type table uses, expanded to the word a shopper types.
# This is vocabulary, not merchandising: "CYL" means cylinder at every yard that files a
# brake master cylinder, so expanding it is a fact. *Which* wording sells the part —
# "Brake Master Cylinder" against "Master Cylinder Brake Booster" — is a decision, and
# stays in the profile's `part_types`.
#
# Some of these are the yard's own abbreviations and some are the column truncating:
# "BACK GLASS REGULATO" is a 20-character varchar cutting "REGULATOR" short, which no
# amount of title-casing repairs.
_ABBREV = {
    "assy": "Assembly", "assm": "Assembly", "asy": "Assembly", "pts": "Parts",
    "reg": "Regulator", "regulato": "Regulator", "cyl": "Cylinder",
    "cntrl": "Control", "contr": "Control", "mod": "Module",
    "wdsh": "Windshield", "dsh": "Dash", "rad": "Radiator", "cond": "Condenser",
    "supp": "Support", "rein": "Reinforcement", "res": "Reservoir",
    "ext": "Extension", "mtd": "Mounted", "int": "Interior", "dr": "Door",
    "susp": "Suspension", "crossm": "Crossmember", "trans": "Transmission",
    "eng": "Engine", "misc": "Miscellaneous", "elec": "Electrical",
    "fr": "Front", "rr": "Rear", "qtr": "Quarter",
    "temp": "Temperature", "spkr": "Speaker",
}

# Short all-caps tokens that really are acronyms a shopper types, so a residue check must
# not report them as an abbreviation nobody expanded.
_REAL_ACRONYMS = {
    "AC", "ABS", "PS", "GPS", "TV", "EGR", "VIN", "LED", "HID", "AWD", "4WD",
    "RH", "LH", "OEM", "ECU", "ECM", "PCM", "SRS", "MAF", "AT", "MT", "DVD",
    "CD", "USB", "TPMS", "EVAP", "FOB", "SUV", "ABC",
}


# A token's own characters, without whatever punctuation the catalogue wrapped it in.
# The model column annotates itself in parentheses — "SAFARI (GMC)", "BLAZER/JIMMY (full
# size)" — and casing the raw token upper-cases the bracket and lower-cases the word
# inside it, so "(GMC)" came out "(gmc)" and stayed that way into the title. Length is
# measured on the core too, or "(GT)" is five characters and misses the acronym rule.
_TOKEN_CORE = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)


def _fix_token(tok: str) -> str:
    if not tok:
        return tok
    lead, core, trail = _TOKEN_CORE.match(tok).groups()
    if not core:
        return tok
    return lead + _fix_word(core) + trail


def _fix_word(tok: str) -> str:
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
    # Generic cleanup: expand the yard's abbreviations, then title-case what is left.
    # Expansion runs first because it turns three-letter tokens the caser would shout
    # ("CYL", "REG") into ordinary words before it ever sees them.
    # The trailing period is consumed with the token so "Misc. Parts" expands to
    # "Miscellaneous Parts" rather than "Miscellaneous. Parts".
    base = re.sub(r"[A-Za-z]+\.?",
                  lambda m: _ABBREV.get(m.group(0).lower().rstrip("."), m.group(0)),
                  pt.strip())
    return _smart_title(base)


def residual_abbreviations(text: str) -> list[str]:
    """Short shouted tokens left in an expanded part-type name.

    What the coverage report actually wants to count. "Caliper" needs no curated wording —
    it is already the word a shopper types — whereas "Wiper Motor, WDSH" is a title nobody
    searches for, and only the second is worth anyone's attention.
    """
    # Tokenised with digits included so "4WD" stays one token rather than yielding a
    # bare "WD" that looks like an abbreviation nobody expanded. A token carrying a digit
    # is a spec ("4WD", "V6"), never a truncated word, so it is never reported.
    return [token for token in re.findall(r"[A-Za-z0-9]{2,6}", text or "")
            if token.isupper() and not any(c.isdigit() for c in token)
            and token not in _REAL_ACRONYMS]


def has_expansion(pt: str, store: "StoreProfile | CatalogProfile | None" = None) -> bool:
    """Whether a table names this part type, rather than the generic cleanup guessing.

    The difference is invisible in a rendered title and expensive in search: a named type
    lists as "Wheel Cylinder", an unnamed one as whatever abbreviation the yard typed,
    title-cased. Counting them is how :mod:`coreyard.yms.part_types` reports where the
    catalogue's vocabulary actually runs out.
    """
    key = re.sub(r"\s+", " ", pt.strip().lower())
    return key in _policy(store).part_types or key in _PART_TYPE


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


def model_without_make(make: Optional[str], model: Optional[str]) -> Optional[str]:
    """A model suitable for a table column that already has a separate make.

    Interchange data sometimes stores ``Lexus ES350`` or ``Lincoln & Town Car`` in the
    model column.  ``_vehicle_label`` prevents that from doubling a title, but a structured
    row has separate make and model cells and therefore needs the prefix removed explicitly.
    A shorter first word also covers ``Mercedes-Benz`` / ``Mercedes 450``.
    """
    cleaned_make = clean_make(make)
    cleaned_model = clean_model(model)
    if not (cleaned_make and cleaned_model):
        return cleaned_model
    prefixes = [cleaned_make, re.split(r"[^A-Za-z0-9]+", cleaned_make, maxsplit=1)[0]]
    for prefix in prefixes:
        if not prefix:
            continue
        match = re.match(rf"^{re.escape(prefix)}(?:\b|(?=[^A-Za-z0-9]))",
                         cleaned_model, flags=re.I)
        if match:
            remainder = cleaned_model[match.end():].lstrip(" &/-")
            return remainder or None
    return cleaned_model


def _labelled_fitments(part: Part) -> list[tuple[str, list]]:
    """Deduped 'Make Model' labels, each with the catalogue rows it was built from.

    A title names some of these labels and states one year span beside them. The span has
    to be measured over the rows behind the labels it actually named, so the two are kept
    together here rather than derived twice from the part — see :func:`named_year_span`.
    """
    grouped: dict[str, list] = {}
    labels: list[str] = []
    for f in display_fitments(part):
        lab = _vehicle_label(clean_make(f.make), clean_model(f.model))
        if not lab:
            continue
        if lab.lower() not in grouped:
            grouped[lab.lower()] = []
            labels.append(lab)
        grouped[lab.lower()].append(f)
    if not labels:
        # No catalogue fitment: the donor vehicle is the only thing this part is known to
        # fit, and it carries no catalogue row — its year comes off the part itself.
        lab = _vehicle_label(clean_make(part.make), clean_model(part.model))
        return [(lab, [])] if lab else []
    # A fitment row with no model yields the bare make, which says nothing next to the
    # specific labels beside it — "Volvo, Volvo 70 Series, Volvo 60 Series" spends title
    # space to repeat what the next label already says.
    specific = {l for l in labels if " " in l}
    if specific:
        labels = [l for l in labels
                  if " " in l or not any(s.lower().startswith(l.lower() + " ") for s in specific)]
    return [(l, grouped[l.lower()]) for l in labels]


def _model_labels(part: Part) -> list[str]:
    """Deduped 'Make Model' labels from fitment (falls back to the part's own vehicle)."""
    return [label for label, _ in _labelled_fitments(part)]


# Some interchange catalogues mark an open-ended run with a placeholder year rather than a
# null. This installation has both 1940 and 1950 start markers and a 2030 end marker. Treat
# any pre-1951 start and any year beyond the next model year as an unknown boundary. The
# latter is deliberately relative to today so this fix does not itself go stale in 2027.
_SENTINEL_START = 1950


def display_fitments(part: Part) -> list:
    """Catalogue rows credible enough to show to a shopper.

    A missing make together with placeholder-only years identifies the corrupt/truncated
    cross-reference rows seen in the live catalogue (for example ``1950 CX-`` and ``2030
    Mazda CX-``). Omit those from every customer-facing output, but retain model-only rows
    that carry a credible year span: some catalogues genuinely do not provide a make map.
    """
    rows = list(part.fitment or [])
    return [
        f for f in rows
        if clean_make(getattr(f, "make", None)) or fitment_year_span(f) != (None, None)
    ]


def fitment_year_span(entry) -> tuple[Optional[int], Optional[int]]:
    """A display-safe year span for one Fitment or Application row.

    Unknown open boundaries collapse to the boundary that is known. An open-ended current
    run is capped at the next model year, matching the title policy, while a row made only
    of placeholder years renders no year at all.
    """
    maximum = date.today().year + 1
    raw_start = getattr(entry, "year_start", None)
    raw_end = getattr(entry, "year_end", None)
    start = (int(raw_start) if raw_start and _SENTINEL_START < int(raw_start) <= maximum
             else None)
    if raw_end and _SENTINEL_START < int(raw_end) <= maximum:
        end = int(raw_end)
    elif start and raw_end and int(raw_end) > maximum:
        end = maximum
    else:
        end = None
    if start:
        return start, max(start, end or start)
    if end:
        return end, end
    return None, None


def fitment_year_label(entry) -> str:
    """A compact, placeholder-free year label for one fitment/application row."""
    return _year_label(*fitment_year_span(entry))


# How many applications one fitment row lists before the rest are summarised. Across this
# installation's catalogue 71.6% of split rows have four or fewer and 89.7% eight or fewer,
# while the longest has 65 — a cap keeps the Impala's five runs whole without letting one
# row bury the twenty vehicles beneath it.
APPLICATION_MAX = 8


def application_label(application) -> str:
    """A qualifier label whose year prefix cannot expose catalogue sentinels."""
    years = fitment_year_label(application)
    note = str(getattr(application, "note", "") or "").strip()
    return f"{years} — {note}" if years and note else years or note


def year_specific(entry) -> bool:
    """Whether one fitment row's applications split its span into narrower runs.

    The merged span is the headline — 2008-2017 Chevrolet Equinox — but underneath it the
    catalogue frequently assigns different options to different years: the Equinox took the
    3.6L in 08-09, the 3.0L in 10-12 and the 3.6L again in 13-17. 41.5% of the rows in this
    installation's catalogue are split this way, so a qualifier stated without its years is
    a restriction the shopper cannot apply to their own car.
    """
    span = fitment_year_span(entry)
    return any(fitment_year_span(a) != span for a in getattr(entry, "applications", ()))


def application_rows(entry) -> list:
    """The applications worth listing under one fitment row.

    A row whose applications all cover its whole span says everything in its qualifiers, so
    only those are shown — an application with no qualifier text would add an empty bullet
    repeating the year span above it.

    A *split* row is different: there the unqualified application is itself the fact. A
    2012-2013 Impala takes this starter with no restriction at all, while 2014-2016 needs a
    VIN code and 2017-2019 the 3.6L. Listing only the qualified runs left the 2012 owner
    reading four restrictions, none of which mentions their year, and concluding it does not
    fit theirs.
    """
    applications = list(getattr(entry, "applications", ()) or [])
    if year_specific(entry):
        return [a for a in applications if application_label(a)]
    return [a for a in applications if getattr(a, "note", "")]


def _span_of(rows) -> tuple[Optional[int], Optional[int]]:
    """The outer year span of some catalogue rows, or ``(None, None)`` for none of them.

    A row that names no make is a catalogue artifact rather than a vehicle application:
    it carries a placeholder span beside a bare or truncated model string ("1960-1970
    VOLVO", "1950-1950 CX-"). The sentinels above catch the 1940 and 2030 markers but
    not these. ``_model_labels`` already drops such rows, so counting their years made
    the two halves of one title disagree with each other — "1960-2008 Volvo 70 Series
    60 80 XC90", where every model it names comes from a row starting in 2001.
    """
    maximum = date.today().year + 1
    starts = [int(f.year_start) for f in rows
              if f.year_start and _SENTINEL_START < int(f.year_start) <= maximum]
    # A real catalogue endpoint is better evidence than another row's open-ended marker.
    # Only cap an open run at the next model year when no row supplies a concrete endpoint.
    ends = [int(f.year_end) for f in rows
            if f.year_end and _SENTINEL_START < int(f.year_end) <= maximum]
    has_open_end = any(f.year_end and int(f.year_end) > maximum for f in rows)
    if starts:
        return min(starts), max(ends or ([maximum] if has_open_end else starts))
    if ends:
        return min(ends), max(ends)
    return None, None


def _year_span(part: Part) -> tuple[Optional[int], Optional[int]]:
    """Every year this part is known to fit, across the whole catalogue entry."""
    span = _span_of(display_fitments(part))
    # Every row was a placeholder — the part's own vehicle is the only real year left.
    return span if span != (None, None) else (part.year, part.year)


def named_year_span(part: Part, rows) -> tuple[Optional[int], Optional[int]]:
    """The span for a sentence that names exactly ``rows`` and nothing else.

    A title states one span for the list of vehicles beside it, and a shopper reads it as
    belonging to the first one. Starter 604-00122 fits nineteen vehicles; the widest run
    among them is the 2012-2020 Impala, and the title had room for four names — so it
    published "2007-2020 GMC Acadia Buick Enclave Chevrolet Equinox Traverse and 15 more"
    when the Acadia stops in 2017. That is not a caveat a shopper has to go looking for
    further down the page, it is a claim about a vehicle the title itself named.

    Measuring the span over the named rows makes the two halves of the title describe the
    same set of cars. The years it gives up belonged to vehicles the title never named, so
    nothing a shopper could have searched on is lost; those vehicles keep their own years
    in the fitment table and the description.
    """
    span = _span_of(rows)
    return span if span != (None, None) else (part.year, part.year)


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

# Words that only join other words. A qualifier is filtered against what the title has
# already said, and dropping the duplicate half of "w/ mirror and lock" — where the part
# type already carries both Mirror and Lock — used to leave the bare connector stranded at
# the end of the title: "Front Door Window Master Switch Mirror And". That reads as a
# sentence cut off mid-thought, so a phrase is trimmed of connectors at both ends after the
# filter, and dropped entirely when nothing but connectors is left.
_CONNECTORS = {
    "and", "or", "with", "w/", "w", "for", "to", "of", "the", "a", "an",
    "plus", "from", "in", "on", "by", "&",
}


def _trim_connectors(words: list[str]) -> list[str]:
    """``words`` with any leading and trailing connector removed."""
    start, end = 0, len(words)
    while start < end and words[start].lower().strip(",") in _CONNECTORS:
        start += 1
    while end > start and words[end - 1].lower().strip(",") in _CONNECTORS:
        end -= 1
    return words[start:end]

# The interchange catalogue is written for a parts counter, and it shows: body styles are
# abbreviated ("Sdn"), sides are bare letters ("L"), and some notes say what a part does
# *not* fit ("exc electric vehicle"). All three read badly on a results page, so a title
# rewrites them. The description is untouched and still carries every note in full,
# attributed to the years it belongs to — this drops nothing, it only stops the title
# leading with counter shorthand.
_QUALIFIER_EXCLUSION = re.compile(r"^(?:exc|except|w/?o|without)\b", re.I)
_QUALIFIER_EXPAND = {
    "sdn": "Sedan", "cpe": "Coupe", "conv": "Convertible", "wgn": "Wagon",
    "hbk": "Hatchback", "at": "Automatic", "mt": "Manual", "dr": "Door",
    "lh": "Left", "rh": "Right", "auto": "Automatic",
}
_SIDE_LETTER = {"l": "Left", "r": "Right"}


# Where in the VIN a character sits is bookkeeping for a parts counter, not something a
# shopper can act on: "(VIN B, 8th digit)" tells them the code is B. The position survives
# on its own only when the note has no comma before it, which is why _NOISE_PHRASE alone
# never caught these — it matches a phrase that is *entirely* an ordinal.
_VIN_POSITION = re.compile(
    r"\b\d+(?:st|nd|rd|th)(?:\s+and\s+\d+(?:st|nd|rd|th))*\s+digits?\b", re.I)


def _drop_orphan_vin(words: list[str]) -> list[str]:
    """Remove a "VIN" that ends up naming no code.

    "VIN" is a label for the character after it. Once the position text is gone, a note that
    never spelled the character out leaves the label pointing at nothing — which published
    as "Alternator Generator Gasoline 1.0L VIN" and "Modulator Assembly 2.4L VIN 8th Digit".
    A trailing label says less than no label at all, so it goes; "VIN B" and "VIN FP" keep
    theirs, because there the label is doing its job.
    """
    while words and words[-1].upper().strip(",") == "VIN":
        words = words[:-1]
    return words


def _title_phrase(phrase: str, has_side: bool) -> str:
    """One catalogue qualifier rewritten for a title, or "" to leave it out."""
    if _QUALIFIER_EXCLUSION.match(phrase):
        return ""
    phrase = _VIN_POSITION.sub(" ", phrase)
    words = phrase.split()
    if len(words) == 1 and words[0].lower() in _SIDE_LETTER:
        # A lone "L"/"R" is the catalogue's side code. It repeats the side the part already
        # states, and where the part states none it is too cryptic to publish as-is.
        return "" if has_side else _SIDE_LETTER[words[0].lower()]
    out: list[str] = []
    for index, word in enumerate(words):
        lowered = word.lower()
        after_vin = index > 0 and words[index - 1].upper().strip(",") == "VIN"
        if len(word) == 1 and word.isalpha() and not after_vin:
            # A stray initial is how a truncated catalogue note reads ("Driver s"). It
            # carries no meaning on a results page, so it is not worth a character — but the
            # single letter after "VIN" is the engine code, the most useful character in the
            # phrase, and dropping it is what left the label stranded.
            continue
        if lowered in _QUALIFIER_EXPAND:
            out.append(_QUALIFIER_EXPAND[lowered])
        elif word.isupper() or any(ch.isdigit() for ch in word):
            out.append(word)          # "4x2", "2.4L", "BCM", VIN codes keep their casing
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(_drop_orphan_vin(out))



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
# The VIN code, however the counter wrote it. "VIN C" is the tidy form, but the position
# phrase is just as often what introduces the character — "3.0L, 4th digit Z", or
# "4th VIN digit Z" — and requiring the code to sit immediately after the label is what
# published a screen naming the fourth VIN digit while dropping the Z it points at. The
# code is the only part of the phrase a buyer can check against their own vehicle.
#
# The second alternative keeps its character class case-sensitive: after a position phrase
# the next token is very often an ordinary word, and a case-insensitive single letter would
# take its initial. This data writes codes in upper case.
_VIN_CODE = re.compile(
    r"\bVIN\s+([A-Z0-9])\b"
    r"|\b\d+(?:st|nd|rd|th)\s+(?:VIN\s+)?digits?[,\s]+((?-i:[A-Z0-9]))\b",
    re.I)

# The manufacturer's option code for a drivetrain, as the note writes it: "(opt LFW)",
# "Opt LZE", "option LU3". Only the *code* is case-sensitive — it is always upper case in
# this data, and requiring that is what stops "optional" and ordinary prose from being read
# as one. A buyer cross-referencing an engine searches the bare code, so it is a fact worth
# a few characters; the yard records it for only a small share of parts, and a part without
# one simply says nothing.
_OPTION_CODE = re.compile(r"\b(?i:opt(?:ion)?)\.?\s*([A-Z][A-Z0-9]{2,4})\b")

# Finish and material, for the part types where it is the thing a buyer is choosing between
# — a chrome grille and a textured one are not substitutes. Only these exact words are
# recognised, because the note is free text written for a parts counter and a finish guessed
# out of it would be exactly the wrong-aspect problem: worse than saying nothing.
_FINISH_TERMS = ("chrome", "textured", "painted", "polished", "satin", "matte", "primed",
                 "black", "alloy", "steel", "aluminum", "plastic", "mesh", "billet",
                 "body color")
_FINISH = re.compile(
    r"\b(" + "|".join(term.replace(" ", r"\s+") for term in _FINISH_TERMS) + r")\b", re.I
)
# Condition, as the yard itself recorded it. Stating it is not a claim the tool invents:
# "Tested" is in the record for this part, and a flaw the buyer will see in the photographs
# belongs in the title rather than in a not-as-described case. Patterns rather than words
# because the notes are typed at a counter — "faded" is spelled six ways in this data
# (faded, fading, fadding, fadded, faddin, faddi) and all six mean the same thing.
_CONDITION_TERMS = (
    (re.compile(r"\btested\b", re.I), "Tested"),
    (re.compile(r"\brebuilt\b", re.I), "Rebuilt"),
    (re.compile(r"\breman\w*", re.I), "Remanufactured"),
    (re.compile(r"\bfad\w*", re.I), "Faded"),
    (re.compile(r"\bbubbl\w*", re.I), "Bubbled"),
    (re.compile(r"\bcrack\w*", re.I), "Cracked"),
    (re.compile(r"\bscratch\w*", re.I), "Scratched"),
    (re.compile(r"\bchip\w*", re.I), "Chipped"),
    (re.compile(r"\bpeel\w*", re.I), "Peeling"),
    (re.compile(r"\bscuff\w*", re.I), "Scuffed"),
    (re.compile(r"\bdent\w*", re.I), "Dented"),
    (re.compile(r"\brust\w*", re.I), "Rusted"),
    (re.compile(r"\bbent\b", re.I), "Bent"),
    (re.compile(r"\bbroken\b", re.I), "Broken"),
)


# "w/o chrome" says this part is the one *without* it. Reading the word on its own and
# asserting the opposite of the note is the worst failure available here, so a negated term
# is not a finish at all.
_NEGATED = re.compile(r"(?:w/?o|without|non|no|exc|except|not)\s*[-\s]*$", re.I)


def _negated(text: str, start: int) -> bool:
    """Whether the term at ``start`` is preceded by a word that reverses it."""
    return bool(_NEGATED.search(text[max(0, start - 16):start]))


def title_condition(part: Part) -> str:
    """Finish and condition from the part's own note: "Chrome Bubbled", "Tested".

    Both halves come from the same sentence a yard wrote about this one part, so they are
    stated together and in the note's own order. Nothing outside the two vocabularies is
    recognised — a note this cannot read produces no claim at all, which is the only safe
    failure when the alternative is describing a part the seller has not described.
    """
    text = part.description or ""
    found: list[tuple[int, str]] = []
    for match in _FINISH.finditer(text):
        if _negated(text, match.start()):
            continue
        found.append((match.start(),
                      " ".join(w.capitalize() for w in match.group(1).split())))
    for pattern, canonical_word in _CONDITION_TERMS:
        for match in pattern.finditer(text):
            if _negated(text, match.start()):
                continue
            found.append((match.start(), canonical_word))
            break
    words: list[str] = []
    for _, word in sorted(found):
        if word not in words:
            words.append(word)
    return " ".join(words[:3])


def title_grade(part: Part, store: "StoreProfile | CatalogProfile | None" = None) -> str:
    """The yard's own condition grade, worded for a title: "A Grade".

    A fact about the record, not a claim about the part: the grade is whatever the yard
    entered, and the wording around it is the site's (``title_grade``). Sites that grade in
    a private vocabulary leave the template empty and the grade stays where it already is —
    a metafield and a line in the description.
    """
    policy = _policy(store)
    grade = str(part.grade or "").strip()
    if not policy.title_grade or not grade:
        return ""
    return seo_clean(policy.title_grade.format(grade=_smart_title(grade)))


def title_mileage(part: Part, store: "StoreProfile | CatalogProfile | None" = None) -> str:
    """Donor mileage for the title, on the part types mileage actually speaks for.

    The source system records the donor's odometer against every part pulled from it, so the
    number exists for a door glass as much as for the engine. Only the part types named in
    ``title_mileage_part_types`` state it, and only up to ``title_mileage_max``: above that
    the number argues against the part, and a seller may reasonably say nothing rather than
    lead with it. It is published as a metafield either way.

    Rendered in thousands ("142K Miles") because the exact figure needs a comma, and a comma
    is one of the symbols a title strips — "142,684 Miles" would reach a shopper as
    "142 684 Miles". Thousands are floored, never rounded up.
    """
    policy = _policy(store)
    if not policy.title_mileage_part_types or not part.mileage:
        return ""
    if (part.part_type or "").strip().lower() not in policy.title_mileage_part_types:
        return ""
    miles = int(part.mileage)
    if miles < 1000 or miles > policy.title_mileage_max:
        return ""
    return f"{miles // 1000}K Miles"


def part_spec(part: Part) -> list[str]:
    """Spec terms from this part's own source note: "3.0L", "6 Cylinder", "VIN C", "Opt LFW".

    These describe the physical part in hand, so unlike interchange qualifiers they can be
    stated in the title without ambiguity. Engines are the big win — displacement, VIN code
    and option code are exactly what a buyer cross-referencing an engine searches for.
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
        spec.append(f"VIN {(m.group(1) or m.group(2)).upper()}")
    m = _OPTION_CODE.search(text)
    if m:
        spec.append(f"Opt {m.group(1).upper()}")
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


# What a title is made of, and what may go when it will not fit.
#
# Rank 1 is load-bearing: the years, the vehicle and the part type are what the listing
# *is*, and a title missing any of them describes nothing. Everything above 1 is a
# refinement, dropped worst-first when a marketplace's character budget binds. The order
# is the drop order, so raising a rank makes a segment more expendable, never less.
#
# This exists because eBay allows 80 characters where Shopify allows 255. That is a
# difference in budget, not a difference in what the part is, so it is a parameter here
# rather than a second title builder somewhere else.
_TITLE_RANKS = {
    "years": 1, "models": 1, "part_type": 1,
    "side": 2, "spec": 3, "condition": 3, "grade": 3, "mileage": 3,
    "qualifiers": 4, "oem": 5, "more_models": 6,
}


def group_model_labels(labels: list[str]) -> str:
    """Vehicle names for a title: each make said once, no punctuation, no repeated word.

    "Infiniti QX60", "Nissan Pathfinder", "Infiniti JX35" becomes
    "Infiniti QX60 JX35 Nissan Pathfinder" — the make is a search term, but saying it twice
    buys nothing and spends characters a second model name could have used. Commas go for
    the same reason every other symbol did: they fragment the title for search and read as
    clutter in a results row.

    Words are deduplicated inside a make, which is where the real saving is: a truck that
    fits three cab weights lists "Silverado 1500 2500 3500" rather than the model name three
    times over.
    """
    groups: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for label in labels:
        make, _, model = label.partition(" ")
        words = groups.setdefault(make, [])
        # The make itself counts as already said. The catalogue disambiguates a shared
        # model name by naming the make again inside the model — "SAFARI (GMC)" — which
        # under a "GMC" heading is the same word twice.
        used = seen.setdefault(make, {make.lower()})
        for word in model.split():
            if word.lower() not in used:
                used.add(word.lower())
                words.append(word)
    return " ".join(" ".join([make, *models]).strip()
                    for make, models in groups.items()).strip()


def title_segments(
    part: Part,
    store: "StoreProfile | CatalogProfile | None" = None,
    max_models: Optional[int] = None,
    compact: bool = False,
) -> list[tuple[str, str, int]]:
    """The ordered ``(name, text, rank)`` pieces every storefront's title is built from.

    One place decides *what a part is called*. Callers differ in how many characters they
    can spend saying it (see :func:`compose_title`) and in ``compact``, which is a
    difference between search engines rather than a difference of opinion:

      * Web search reads "2010 2011 2012" more reliably than "2010-2012" — a hyphenated
        span tokenizes unpredictably, and shoppers type years out in full.
      * A marketplace listing is read by a person scanning a results page, where the span
        is what they recognise and the characters it saves buy the engine size instead.

    ``compact`` also moves the specification next to the vehicle, so the title reads
    "Chevrolet Malibu 2.4L VIN 0 Engine" rather than "Chevrolet Malibu Engine 2.4L VIN 0".
    """
    policy = _policy(store)
    if max_models is None:
        max_models = policy.title_max_models
    pt = _title_part_type(part, store)
    side = title_side(part, store)
    named = [(cleaned, rows)
             for cleaned, rows in ((seo_clean(label), rows)
                                   for label, rows in _labelled_fitments(part))
             if cleaned]
    labels = [cleaned for cleaned, _ in named]
    spec = part_spec(part)
    condition = title_condition(part)
    grade = title_grade(part, store)
    mileage = title_mileage(part, store)
    seen = {w.lower() for w in pt.split()}
    seen |= {w.lower() for term in spec for w in term.split()}
    seen |= {w.lower() for w in condition.split()}
    seen |= {w.lower() for w in grade.split()}
    seen |= {w.lower() for w in mileage.split()}
    # The side segment already says "Driver Side Left"; a catalogue qualifier that repeats
    # it spends characters on a word the title has used.
    seen |= {w.lower() for w in side.split()}
    extra: list[str] = []
    for phrase in title_qualifiers(part):
        phrase = _title_phrase(phrase, bool(side))
        if not phrase:
            continue
        kept = _trim_connectors([w for w in phrase.split() if w.lower() not in seen])
        if not kept or _NOISE_PHRASE.match(" ".join(kept)):
            continue
        extra.append(" ".join(kept))
        seen |= {w.lower() for w in kept}
    lead = "OEM" if policy.title_include_oem and "oem" not in seen else ""
    models, more_models = "", ""
    # A cap of zero is "no cap": name every vehicle and let the character budget in
    # :func:`fit_title` decide how many survive. See ``title_max_models``.
    shown = named if max_models <= 0 else named[:max_models]
    # The years describe the vehicles the title names, not the ones it had no room for, so
    # they are measured here — after the cap — and narrow with it as the budget binds.
    span = named_year_span(part, [row for _, rows in shown for row in rows])
    years = _year_label(*span) if compact else _year_tokens(*span)
    if labels:
        more = len(labels) - len(shown)
        models = group_model_labels([cleaned for cleaned, _ in shown])
        # Counted separately from the names themselves because it is worth much less than
        # them: on a tight budget "and 7 more" is characters that name no vehicle and carry
        # no search term, and a buyer looking for an engine would rather have its size.
        more_models = f"and {more} more" if more > 0 else ""
    # The written order, which is not the drop order.
    if compact:
        ordered = [
            ("years", years), ("models", models), ("spec", " ".join(spec)),
            ("more_models", more_models), ("side", side), ("part_type", pt),
            ("condition", condition), ("qualifiers", " ".join(extra)),
            # Last, and after the qualifiers: a qualifier is part of what the thing *is*
            # ("Fuel Pump Assembly", "Master Switch Lock"), so a grade placed before it
            # splits the part's own name — "Fuel Pump A Grade Assembly".
            ("grade", grade), ("mileage", mileage), ("oem", lead),
        ]
    else:
        ordered = [
            ("years", years), ("models", models), ("more_models", more_models),
            ("side", side), ("oem", lead),
            ("part_type", pt), ("spec", " ".join(spec)), ("condition", condition),
            ("qualifiers", " ".join(extra)), ("grade", grade), ("mileage", mileage),
        ]
    return [(name, text, _TITLE_RANKS[name]) for name, text in ordered if text]


def compose_title(segments: list[tuple[str, str, int]], limit: int = TITLE_MAX,
                  measure=len) -> tuple[str, list[str]]:
    """Join ``segments`` in written order, dropping the most expendable until it fits.

    ``measure`` is how the destination counts characters: eBay's portal escapes a title
    before eBay sees it, so ``&`` costs five characters there and one here. Returns the
    title and the names of whatever had to go, so a caller can report the loss instead of
    discovering it in a published listing.
    """
    kept = list(segments)
    dropped: list[str] = []
    while True:
        title = " ".join(text for _, text, _ in kept)
        if measure(title) <= limit:
            return title, dropped
        expendable = [item for item in kept if item[2] > 1]
        if not expendable:
            # Nothing left but the load-bearing pieces. Truncating is the honest failure:
            # a title that says what the part is, cut short, beats one that does not.
            # Trimmed against the caller's own measure, not against len(): a destination
            # that counts an escaped "&" as five characters would otherwise be handed a
            # title this function had just declared short enough.
            while title and measure(title) > limit:
                title = title[:-1].rstrip()
            # Truncation is a loss, and it has to be reported as one: a caller trying
            # variants (see fit_title) would otherwise read a title cut off mid-word as
            # having fit perfectly, and stop looking for the one that actually does.
            return title, dropped + ["truncated"]
        worst = max(expendable, key=lambda item: item[2])
        kept.remove(worst)
        dropped.append(worst[0])


def fit_title(part: Part, store: "StoreProfile | CatalogProfile | None" = None,
              max_models: Optional[int] = None, limit: int = TITLE_MAX,
              measure=len, compact: bool = False) -> tuple[str, list[str]]:
    """Compose the title, spending the budget on facts before extra vehicle names.

    When the budget binds, the first thing to give is the *number of vehicles listed*, not
    the engine size. "2009-2010 Ford Explorer 4.0L V6 VIN E Engine" sells a part;
    "2009-2010 Ford Explorer, Mercury Mountaineer and 2 more Engine" is the same characters
    spent listing cars the buyer did not search for. So fewer models are tried before any
    segment is dropped, and only then does rank take over.

    How many vehicles are *offered* to that budget is ``title_max_models``. A positive cap
    is a hard ceiling on the names a title may carry; **zero means the budget is the only
    ceiling**, which is what fills Shopify's 255 characters with the vehicles the fitment
    catalogue actually named rather than four of them and the phrase "and 17 more".
    """
    policy = _policy(store)
    cap = policy.title_max_models if max_models is None else max_models
    available = len([c for c in (seo_clean(l) for l in _model_labels(part)) if c])
    # A cap of zero spends the *character* budget instead of a model count, so the scan
    # starts from every vehicle the catalogue named. Two bounds keep that honest: there is
    # no point trying more labels than exist, and none in trying more than the budget could
    # possibly hold — no label joins the title for under about four characters. Both only
    # remove counts that would have produced a title identical to one still tried, so a
    # capped site's titles are byte-for-byte what they were.
    start = available if cap <= 0 else cap
    start = max(1, min(start, available or 1, max(1, limit // 4)))
    fallback: tuple[str, list[str]] | None = None
    for count in range(start, 0, -1):
        title, dropped = compose_title(
            title_segments(part, store, count, compact), limit, measure
        )
        if not dropped:
            return title, dropped
        if fallback is None or _loss_rank(dropped) < _loss_rank(fallback[1]):
            fallback = (title, dropped)
    return fallback if fallback else ("", [])


def _loss_rank(dropped: list[str]) -> tuple[int, int]:
    """How bad a set of losses is: truncation first, then what was given up.

    Counting losses alone made a truncated title with four vehicle names beat an intact one
    with a single name, because it had "fewer" drops. But truncation cuts from the end, and
    in the compact order the end is where the part type sits — so the cheaper-looking answer
    was the one that stopped saying what the part is: "GMC Acadia, Saturn Outlook, Buick
    Enclave, Chevrolet Traverse ABS Anti". Naming one vehicle and the whole part beats naming
    four vehicles and half the part, every time.

    Counting alone was also blind to *which* segment went. Dropping "and 3 more" and dropping
    "Driver Side Left" both scored one, so a variant that listed another vehicle at the cost
    of the side won on the tie — and a mirror sold without a side is a return, not a sale.
    Losses are weighted by the same table that decides drop order, so giving up a vehicle
    name to keep the side is now the cheaper answer rather than the more expensive one.
    """
    weight = sum(max(1, 7 - _TITLE_RANKS.get(name, 6)) for name in dropped
                 if name != "truncated")
    return (1 if "truncated" in dropped else 0, weight)


def build_title(part: Part, store: "StoreProfile | CatalogProfile | None" = None,
                max_models: Optional[int] = None, limit: int = TITLE_MAX,
                measure=len, compact: bool = False) -> str:
    """e.g. "2008 Ford F150 Left Driver Side View Door Mirror".

    No dashes, slashes or symbols. Whether the word "OEM" appears is the site's call
    (``title_include_oem``): it is a high-intent search term, but it also eats characters
    that could carry another model, and it stays in the tags and description regardless.

    ``limit`` and ``measure`` let a tighter marketplace reuse this exact title rather than
    grow its own builder; at Shopify's 255 characters nothing is ever dropped.
    """
    return fit_title(part, store, max_models, limit, measure, compact)[0]


def extend_override_title(title: str, part: Part,
                          store: "StoreProfile | CatalogProfile | None" = None,
                          limit: int = TITLE_MAX) -> str:
    """A reviewed title, plus the per-part facts it predates.

    A reviewed engine title carries facts the yard database does not hold — displacement,
    VIN code, cylinder configuration — because the part's own note is empty and they were
    read from the listing portal's detail page. The renderer cannot reconstruct those, so it
    must not replace such a title. It is also *narrower* on purpose: a reviewed title names
    the vehicles that took this engine variant, where fitment alone widens to every model
    the interchange group covers.

    But those titles were written before a title stated the yard's grade or the donor's
    mileage, and unlike displacement those are facts about *this* part rather than about the
    engine family — so they are appended rather than lost. Nothing already said is repeated,
    and a title that will not fit the budget is returned untouched: a reviewed decision is
    not worth truncating for an addition.
    """
    extra = [text for text in (title_grade(part, store), title_mileage(part, store)) if text]
    if not title or not extra:
        return title
    said = {word.lower() for word in title.split()}
    keep = [text for text in extra
            if not {word.lower() for word in text.split()} <= said]
    if not keep:
        return title
    extended = " ".join([title.strip(), *keep])
    return extended if len(extended) <= limit else title


def meta_title(part: Part, store: "StoreProfile | CatalogProfile | None" = None) -> str:
    """A short search title that never sacrifices the name of the part.

    A blind 60-character cut used to turn ``Door Window Regulator`` into ``Door`` and
    ``Engine Motor Assembly`` into ``Engine Motor``. Vehicle context is useful metadata,
    but an incomplete part name is actively misleading, so optional context is dropped in
    priority order before truncation becomes the last resort.
    """
    named = [(cleaned, rows)
             for cleaned, rows in ((seo_clean(label), rows)
                                   for label, rows in _labelled_fitments(part))
             if cleaned]
    # This title has room for one vehicle, so its years are that vehicle's — a search
    # result reading "2007-2020 GMC Acadia" is the same overstatement as the full title's.
    span = named_year_span(part, named[0][1]) if named else _year_span(part)
    segments = [
        ("years", _year_tokens(*span), 4),
        ("models", named[0][0] if named else "", 3),
        ("side", title_side(part, store), 2),
        ("part_type", _title_part_type(part, store), 1),
    ]
    title, _dropped = compose_title(
        [(name, text, rank) for name, text, rank in segments if text],
        META_TITLE_MAX,
    )
    return title


def reviewed_meta_title(title: str) -> str:
    """Fit a reviewed listing title into Shopify's shorter SEO-title field."""
    return _cap(title, META_TITLE_MAX)


def meta_description(part: Part, store: Optional[StoreProfile] = None) -> str:
    """Build complete sentences within Shopify's 160-character metadata budget."""
    store = store or StoreProfile()
    policy = _policy(store)
    pt = expand_part_type(part.part_type, store)
    named = _labelled_fitments(part)
    labels = [label for label, _ in named]
    subject = " ".join(x for x in ("Used OEM", side_phrase(part.side), pt) if x)

    # Prefer the richest fitment sentence that fits whole. Dropping extra applications is
    # better than publishing a snippet that ends in half a vehicle or half a policy claim.
    lead = ""
    for count in range(min(3, len(labels)), -1, -1):
        fit = ", ".join(labels[:count])
        if count and len(labels) > count:
            fit += " and more"
        # As in a title, the span belongs to the vehicles this sentence names. A snippet
        # that names none of them is describing the part itself, so it keeps the full span.
        span = (named_year_span(part, [row for _, rows in named[:count] for row in rows])
                if count else _year_span(part))
        years = _year_tokens(*span)
        target = " ".join(x for x in (years, fit) if x)
        candidate = f"{subject} for {target}." if target else f"{subject}."
        if len(candidate) <= META_DESC_MAX:
            lead = candidate
            break
    if not lead:
        # A site-supplied part type can itself exceed the budget. Keep a word-boundary cut
        # as the final fallback, but still finish it as a sentence.
        lead = _cap(subject, META_DESC_MAX - 1).rstrip(". ") + "."

    extras = [policy.availability_text(store.origin())]
    if store.warranty:
        extras.append(str(store.warranty).strip().rstrip(".") + ".")
    if part.stock_number:
        extras.append(f"Stock #{part.stock_number}.")
    description = lead
    for extra in extras:
        sentence = str(extra or "").strip()
        if not sentence:
            continue
        if sentence[-1] not in ".!?":
            sentence += "."
        candidate = f"{description} {sentence}"
        if len(candidate) <= META_DESC_MAX:
            description = candidate
    return description


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
    notice = policy.part_type_notice(pt)
    if notice:
        lines.append(f"<p><strong>Safety disclosure:</strong> {html.escape(notice)}</p>")
    if part.uses_donor_photos and policy.donor_photo_note:
        # Above everything else about the part: a shopper who scrolls no further has still
        # been told what they are looking at.
        note = policy.donor_photo_note.replace("{donor}", donor_label(part) or "donor vehicle")
        lines.append(f"<p><strong>{html.escape(note)}</strong></p>")
    if _looks_like_prose(part.description):
        lines.append(f"<p>{html.escape(part.description)}</p>")

    shown_fitments = display_fitments(part)
    if shown_fitments:
        heading = "Fits"
        if part.interchange_number:
            heading += f" Interchange #{part.interchange_number}"
        lines.append(f"<p><strong>{html.escape(heading)}:</strong></p><ul>")
        for f in shown_fitments[:40]:
            veh = " ".join(x for x in (
                fitment_year_label(f),
                _vehicle_label(clean_make(f.make), clean_model(f.model)),
            ) if x)
            # The catalogue qualifies most applications (engine, drivetrain, body, emissions,
            # production date). Those decide whether a part actually fits, so show them.
            quals = application_rows(f)
            if not quals:
                lines.append(f"  <li>{html.escape(veh)}</li>")
                continue
            lines.append(f"  <li>{html.escape(veh)}")
            lines.append("    <ul>")
            for a in quals[:APPLICATION_MAX]:
                lines.append(f"      <li>{html.escape(application_label(a))}</li>")
            if len(quals) > APPLICATION_MAX:
                # Say that the list was cut. A silent cut on a split row is the same defect
                # this list exists to fix: the year run that was dropped reads as excluded.
                rest = len(quals) - APPLICATION_MAX
                lines.append(f"      <li>…and {rest} more year/option variant"
                             f"{'' if rest == 1 else 's'} — ask us to confirm yours.</li>")
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


def donor_label(part: Part) -> str:
    """"2016 Buick Encore (Stock #259965)" — how a donor photo is attributed.

    Cased through the same helpers as a title, because the source writes "BUICK ENCORE" and
    a shopper should not be shouted at from the alt text.
    """
    vehicle = " ".join(x for x in (
        str(part.year) if part.year else "",
        _vehicle_label(clean_make(part.make), clean_model(part.model)),
    ) if x).strip()
    stock = str(part.stock_number or "").strip()
    if vehicle and stock:
        return f"{vehicle} (Stock #{stock})"
    return vehicle or (f"Stock #{stock}" if stock else "")


def image_alt(part: Part, index: int = 1,
              store: "StoreProfile | CatalogProfile | None" = None) -> str:
    """Alt text for an uploaded photo.

    Staged media were being created with no alt at all, which costs image search results
    and leaves the listing unreadable to a screen reader. Numbering keeps each photo on a
    product distinct.
    """
    # Alt describes the photographed object, not every vehicle the interchange catalogue
    # says it fits. Reusing the SEO title made a screen reader announce a long compatibility
    # string before it reached the words naming the part.
    pt = expand_part_type(part.part_type, store)
    side = title_side(part, store)
    vehicle = " ".join(x for x in (
        str(part.year) if part.year else "",
        _vehicle_label(clean_make(part.make), clean_model(part.model)),
    ) if x).strip()
    base = " ".join(x for x in ("Used OEM", side, pt) if x)
    if vehicle:
        base += f" from {vehicle}"
    base = _cap(base, 125) or "Used auto part"
    if part.uses_donor_photos:
        # Alt text describes the image, and this image is of a car, not of the part.
        label = donor_label(part)
        base = _cap(f"Donor vehicle {label}" if label else f"Donor vehicle for {base}", 125)
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

    shown_fitments = display_fitments(part)
    for f in shown_fitments:
        mk, veh = clean_make(f.make), _vehicle_label(clean_make(f.make), clean_model(f.model))
        add(mk)
        add(veh)
        year_start, year_end = fitment_year_span(f)
        if year_start:
            for y in range(year_start, (year_end or year_start) + 1):
                add(f"{y} {veh}".strip())
    if not shown_fitments:
        # No fitment rows, so the part's own vehicle is all there is. Route it through
        # _vehicle_label like the fitment branch above: joining make and model directly
        # doubles a make the model already carries, which is how 180 parts ended up
        # tagged "2019 Dodge Dodge 1500" (make "DODGE TRUCK", model "DODGE 1500 PICKUP").
        mk = clean_make(part.make)
        add(mk)
        add(" ".join(str(x) for x in (part.year, _vehicle_label(mk, clean_model(part.model))) if x))
    for f in shown_fitments:
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
