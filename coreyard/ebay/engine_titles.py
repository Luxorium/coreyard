"""Deterministic, guarded SEO titles for normalized engine listings.

Source-system engine titles follow a stable grammar. Parsing that grammar is cheaper and
more consistent than asking a model to reconstruct facts already present in the listing.
"""

from __future__ import annotations

import re

from coreyard.ebay.util import TITLE_MAX, title_length


MODEL_MAKE = {
    "240SX": "Nissan", "300": "Chrysler", "500X": "Fiat",
    "ACCLAIM": "Plymouth", "ACCORD": "Honda", "ALERO": "Oldsmobile",
    "ALLURE": "Buick", "ALTIMA": "Nissan", "AMANTI": "Kia",
    "AURA": "Saturn", "AVALON": "Toyota", "AVEO": "Chevrolet",
    "BAJA": "Subaru", "BEETLE": "Volkswagen", "BRAVADA": "Oldsmobile",
    "CALIBER": "Dodge", "CAMRY": "Toyota", "CAPRICE": "Chevrolet",
    "CARAVAN": "Dodge", "CIVIC": "Honda", "COBALT": "Chevrolet",
    "COMPASS": "Jeep", "CONTOUR": "Ford", "COROLLA": "Toyota",
    "CR-V": "Honda", "CR-Z": "Honda", "CROWN VICTORIA": "Ford",
    "CTS": "Cadillac", "DAKOTA": "Dodge", "DEVILLE": "Cadillac",
    "ECHO": "Toyota", "ELANTRA": "Hyundai", "ENCLAVE": "Buick",
    "ENDEAVOR": "Mitsubishi", "ENVOY": "GMC", "EQUINOX": "Chevrolet",
    "ESCAPE": "Ford", "ESCORT": "Ford", "EXPLORER": "Ford",
    "FIESTA": "Ford", "FIVE HUNDRED": "Ford", "FOCUS": "Ford",
    "FREESTAR": "Ford", "FUSION": "Ford", "G6": "Pontiac",
    "HHR": "Chevrolet", "HIGHLANDER": "Toyota", "IMPALA": "Chevrolet",
    "IMPREZA": "Subaru", "JETTA": "Volkswagen", "JOURNEY": "Dodge",
    "KICKS": "Nissan", "LEGACY": "Subaru", "LIBERTY": "Jeep",
    "MALIBU": "Chevrolet", "MATRIX": "Toyota", "MAXIMA": "Nissan",
    "MICRA": "Nissan", "MIRAGE": "Mitsubishi", "MUSTANG": "Ford",
    "ODYSSEY": "Honda", "OPTIMA": "Kia", "PRIZM": "Chevrolet",
    "QUEST": "Nissan", "RANGER": "Ford", "RAV4": "Toyota",
    "ROGUE": "Nissan", "SABLE": "Mercury", "SANTA FE": "Hyundai",
    "SENTRA": "Nissan", "SEQUOIA": "Toyota", "SIENNA": "Toyota",
    "SOLARA": "Toyota", "SONOMA": "GMC", "TAURUS": "Ford",
    "TL": "Acura", "TUNDRA": "Toyota", "VERACRUZ": "Hyundai",
    "VERSA": "Nissan", "VILLAGER": "Mercury", "VUE": "Saturn",
    "YARIS": "Toyota",
}

KNOWN_MAKES = {
    "ACURA": "Acura", "AUDI": "Audi", "BMW": "BMW", "BUICK": "Buick",
    "CADILLAC": "Cadillac", "CHEVROLET": "Chevrolet", "CHEVY": "Chevrolet",
    "CHRYSLER": "Chrysler", "DODGE": "Dodge", "FIAT": "Fiat",
    "FORD": "Ford", "GMC": "GMC", "HONDA": "Honda", "HUMMER": "Hummer",
    "HYUNDAI": "Hyundai", "INFINITI": "Infiniti", "ISUZU": "Isuzu",
    "JAGUAR": "Jaguar", "JEEP": "Jeep", "KIA": "Kia",
    "LAND": "Land Rover", "LEXUS": "Lexus", "LINCOLN": "Lincoln",
    "MAZDA": "Mazda", "MERCEDES": "Mercedes-Benz",
    "MERCEDES-BENZ": "Mercedes-Benz", "MERCURY": "Mercury",
    "MINI": "Mini", "MINI COOPER": "Mini", "MITSUBISHI": "Mitsubishi",
    "NISSAN": "Nissan", "OLDSMOBILE": "Oldsmobile", "PLYMOUTH": "Plymouth",
    "PONTIAC": "Pontiac", "PORSCHE": "Porsche", "RAM": "Ram",
    "SAAB": "Saab", "SATURN": "Saturn", "SCION": "Scion",
    "SMART": "Smart", "SUBARU": "Subaru", "SUZUKI": "Suzuki",
    "TOYOTA": "Toyota", "VOLKSWAGEN": "Volkswagen", "VOLVO": "Volvo",
    "VW": "Volkswagen",
}

MODEL_UPPER = {
    "ATS", "BRZ", "CR-V", "CR-Z", "CTS", "CVT", "DX", "ES", "EX",
    "EX-L", "FX", "GS", "GT", "GTI", "GX", "HHR", "ILX", "IS",
    "LE", "LS", "LX", "MDX", "QX", "RAV4", "RDX", "RX", "RX300",
    "S10", "SE", "SI", "SRX", "STS", "SV", "TL", "TLX", "TSX",
    "WRX", "XLE", "XT5", "XTS", "4WD", "ABS", "AWD", "CNG", "DOHC",
    "EGR", "FWD", "LEV", "LPG", "OEM", "PZEV", "RWD", "SOHC",
    "SULEV", "ULEV", "VIN",
}
_MODEL_FIXUPS = {"Deville": "DeVille", "Sat L Sdn": "L Series"}
_MAKE_SUFFIX = re.compile(r"\s+(TRUCK|CAR|VAN)$", re.I)
_CID_CONFIG = {"3": "V6", "4": "4 Cylinder", "5": "Inline 5",
               "6": "V6", "8": "V8"}

_QUALIFIERS = [
    (re.compile(r"\bpower ?stroke\b", re.I), "Power Stroke"),
    (re.compile(r"\bduramax\b", re.I), "Duramax"),
    (re.compile(r"\btdi\b", re.I), "TDI"),
    (re.compile(r"\bdiesel\b", re.I), "Diesel"),
    (re.compile(r"\bhybrid\b", re.I), "Hybrid"),
    (re.compile(r"\bturbo\b", re.I), "Turbo"),
    (re.compile(r"\b(romeo|windsor|duratec|vulcan|triton|modular)\b", re.I), None),
    (re.compile(r"\b([234]V)\b"), None),
    (re.compile(r"\bdohc\b", re.I), "DOHC"),
    (re.compile(r"\bsohc\b", re.I), "SOHC"),
    (re.compile(r"\bohv\b", re.I), "OHV"),
    (re.compile(r"\bflex ?fuel\b", re.I), "Flex Fuel"),
    (re.compile(r"\bcng\b", re.I), "CNG"),
    (re.compile(r"\bcvt\b", re.I), "CVT"),
    (re.compile(r"\b(awd|4wd|fwd|rwd)\b", re.I), None),
    (re.compile(r"\bgasoline\b|\bgas\b", re.I), "Gasoline"),
]
_NEGATED = re.compile(r"(?:without|w/o|less|excluding|non)\s*$", re.I)
_NOTES = [
    re.compile(r"\b(?:with|without|w/o|w/|excluding|less)\b"
               r"(?:\s+[A-Za-z][\w/-]*){1,5}", re.I),
    re.compile(r"\blong block\b", re.I),
    re.compile(r"\bthru\s+[\d/]+", re.I),
    re.compile(r"\bfrom\s+[\d/]+", re.I),
    re.compile(r"\b(new style|old style|classic style)\b", re.I),
    re.compile(r"\b(federal|california|canada|japan built|canada market)"
               r"(?:\s+(?:emissions|market))?\b", re.I),
    re.compile(r"\b(low|standard|ulev|pzev)\s*emissions?\b", re.I),
    re.compile(r"\b(pzev|ulev)\b", re.I),
    re.compile(r"\b(manual|automatic|cvt)(?:\s+\d\s+speed)?\s+transmission\b", re.I),
    re.compile(r"\b\d\s+speed\s+transmission\b", re.I),
    re.compile(r"\belectric cooling fan\b", re.I),
    re.compile(r"\bnaturally aspirated\b", re.I),
    re.compile(r"\btow pkg\b", re.I),
]
_BODY = re.compile(r"\b(coupe|sedan|wagon|hatchback|convertible|4 door|2 door)\b", re.I)
_STOCK_TAIL = re.compile(r"\s+\d{4,6}\s*$")
_VIN_CODE = re.compile(r"\bVIN\s+([A-Z0-9]{1,2})\b", re.I)
_DIGIT_POS = re.compile(r"\b\d(?:st|nd|rd|th)\s+(?:and\s+\d(?:st|nd|rd|th)\s+)?digits?\b", re.I)
_DISP = re.compile(r"\b(\d\.\d)\s?L\b", re.I)
_CID = re.compile(r"\b([3-8])-(\d{3})\b")
_OPT = re.compile(r"\bOpt\s+([A-Za-z0-9]{2,4})\b", re.I)
_ENGINE_ID_EXPLICIT = re.compile(r"\bEngine\s+ID\s+([A-Za-z0-9-]{3,12})\b", re.I)
_ENGINE_ID = re.compile(
    r"\b(?:[A-Z]{3}\d[A-Z]{2}|[A-Z]{2}\d{2}[A-Z]{2,3}|"
    r"\d[A-Z]{2}-?[A-Z]{2,3}|[A-Z]\d[A-Z]{2}[A-Z]?|"
    r"[A-Z]\d{2}[A-Z]|C[A-Z]{3}|B\d{3}[A-Z]?|L[A-Z]{2}|"
    r"L[A-Z]\d|L\d[A-Z]|[A-Z]{2}\d)\b"
)
_FITS = re.compile(r"\bFits\b\s+(.*)$", re.I)
_YEAR_TOKENS = re.compile(r"\b(\d{2})(?:-(\d{2}))?\b")
_LEAD_YEARS = re.compile(r"^((?:19|20)\d{2})(?:-((?:19|20)\d{2}))?\s+")
_CYL_WORD = re.compile(r"\b(V6|V8|V10|Inline[\s-]+[456]|[468]\s+Cylinder)\b", re.I)


def _expand(year: int) -> int:
    return 1900 + year if year >= 50 else 2000 + year


def _clean_make(name: str) -> str:
    value = _MAKE_SUFFIX.sub("", (name or "").strip())
    return KNOWN_MAKES.get(value.upper(), value.title())


def _title_case(value: str) -> str:
    words = []
    for word in value.split():
        if word.upper() in KNOWN_MAKES:
            words.append(KNOWN_MAKES[word.upper()])
        elif word.upper() in MODEL_UPPER:
            words.append(word.upper())
        elif re.search(r"\d", word):
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:].lower())
    result = " ".join(words)
    return _MODEL_FIXUPS.get(result, result)


def _norm_note(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" ,")
    value = re.sub(r"^w/", "With ", value, flags=re.I)
    return _title_case(value)


def parse(title: str, detail: dict | None = None) -> dict:
    """Parse normalized source title/detail fields into title facts."""
    detail = detail or {}
    source = (title or "").strip()
    body = _STOCK_TAIL.sub("", source)
    conditions = str(detail.get("conditions_options") or "")
    out: dict = {"source": source}

    years: list[tuple[int, int]] = []
    model = ""
    match = _FITS.search(body)
    if match:
        tail = match.group(1)
        position = 0
        for token in _YEAR_TOKENS.finditer(tail):
            if token.start() > position + 1:
                break
            start = _expand(int(token.group(1)))
            end = _expand(int(token.group(2))) if token.group(2) else start
            years.append((start, end))
            position = token.end()
        model = tail[position:].strip()
        body = body[:match.start()]
    else:
        lead = _LEAD_YEARS.match(body)
        if lead:
            start = int(lead.group(1))
            years.append((start, int(lead.group(2)) if lead.group(2) else start))
            remaining = body[lead.end():].split()
            taken = 0
            if remaining and remaining[0].upper() in KNOWN_MAKES:
                model, taken = remaining[0], 1
                for word in remaining[1:]:
                    if _DISP.match(word) or word.upper() == "VIN" or re.match(r"^\d\.\d", word):
                        break
                    model += " " + word
                    taken += 1
            body = " ".join(remaining[taken:])
    out["years"] = years

    make = ""
    model_upper = model.upper()
    first = model_upper.split()[0] if model_upper else ""
    if first in KNOWN_MAKES:
        make = KNOWN_MAKES[first]
        model = model[len(first):].strip()
    else:
        for key, value in MODEL_MAKE.items():
            if model_upper == key or model_upper.startswith(key + " "):
                make = value
                break
    if not make:
        make = _clean_make(str(detail.get("make") or ""))
        out["make_from_donor"] = bool(make)
    out["make"] = make
    out["model"] = _title_case(model.strip()) if model else ""

    def take(pattern, text):
        found = pattern.search(text)
        return ((found, text[:found.start()] + " " + text[found.end():])
                if found else (None, text))

    displacement, body = take(_DISP, body)
    condition_displacement = _DISP.search(conditions)
    out["displacement"] = (
        f"{displacement.group(1)}L" if displacement else
        (f"{condition_displacement.group(1)}L" if condition_displacement else "")
    )
    vin, body = take(_VIN_CODE, body)
    out["vin_code"] = vin.group(1).upper() if vin else ""
    body = _DIGIT_POS.sub(" ", body)
    code = ""
    explicit, body = take(_ENGINE_ID_EXPLICIT, body)
    if explicit:
        code = explicit.group(1).upper()
    else:
        option, body = take(_OPT, body)
        if option:
            code = option.group(1).upper()
    out["engine_code"] = code

    configuration = ""
    cid, body = take(_CID, body)
    if cid:
        configuration = _CID_CONFIG.get(cid.group(1), "")
    cylinder, body = take(_CYL_WORD, body)
    if not configuration and cylinder:
        word = re.sub(r"[\s-]+", " ", cylinder.group(1))
        configuration = word.upper() if word[0].lower() == "v" else _title_case(word)
    if not configuration:
        found = re.search(r"\b([468])\s*cyl", conditions, re.I)
        if found:
            configuration = _CID_CONFIG.get(found.group(1), "")
    out["config"] = configuration

    body = re.sub(r"\s+", " ", body).strip()
    notes: list[str] = []
    for pattern in _NOTES:
        while True:
            note = pattern.search(body)
            if not note:
                break
            notes.append(_norm_note(note.group(0)))
            body = body[:note.start()] + " " + body[note.end():]
    out["notes"] = notes

    body = re.sub(r"\s+", " ", body).strip()
    qualifiers: list[str] = []
    for pattern, label in _QUALIFIERS:
        for found in pattern.finditer(body):
            if _NEGATED.search(body[:found.start()]):
                continue
            value = found.group(1) if found.re.groups else found.group(0)
            qualifiers.append(label or (value.upper() if len(value) <= 3 else _title_case(value)))
            body = body[:found.start()] + " " + body[found.end():]
            break
    out["qualifiers"] = qualifiers

    body_style, body = take(_BODY, body)
    style = _title_case(body_style.group(1)) if body_style else ""
    out["body_style"] = "" if style and style.lower() in out["model"].lower() else style
    if not code:
        for candidate in _ENGINE_ID.finditer(body):
            token = candidate.group(0).upper()
            if token == out["vin_code"] or token in MODEL_UPPER:
                continue
            out["engine_code"] = token
            break
    return out


def compose(facts: dict, keep_notes: bool = False,
            limit: int = TITLE_MAX) -> tuple[str, list[str]]:
    years = facts.get("years") or []
    span = " ".join(f"{start}-{end}" if end != start else f"{start}"
                    for start, end in years)
    vehicle = " ".join(value for value in (facts.get("make"), facts.get("model"))
                       if value).strip()
    qualifiers = facts.get("qualifiers") or []
    notes = facts.get("notes") or []
    segments = [
        ("years", span, 1), ("vehicle", vehicle, 1),
        ("displacement", facts.get("displacement", ""), 1),
        ("config", facts.get("config", ""), 4),
        ("qual0", qualifiers[0] if qualifiers else "", 3),
        ("qual1", qualifiers[1] if len(qualifiers) > 1 else "", 7),
        ("qual2", qualifiers[2] if len(qualifiers) > 2 else "", 9),
        ("qual3", qualifiers[3] if len(qualifiers) > 3 else "", 12),
        ("body", facts.get("body_style", ""), 8),
        ("vin", f"VIN {facts['vin_code']}" if facts.get("vin_code") else "", 2),
        ("code", facts.get("engine_code", ""), 5),
        ("engine", "Engine", 1), ("motor", "Motor", 6),
        ("assembly", "Assembly", 10),
        ("note0", notes[0] if notes else "", 1 if keep_notes else 6),
        ("note1", notes[1] if len(notes) > 1 else "", 9),
        ("oem", "OEM", 11),
    ]
    segments = [segment for segment in segments if segment[1]]
    dropped: list[str] = []
    while True:
        title = " ".join(segment[1] for segment in segments)
        if title_length(title) <= limit:
            return title, dropped
        droppable = [segment for segment in segments if segment[2] > 1]
        if not droppable:
            return title, dropped
        worst = max(droppable, key=lambda segment: segment[2])
        segments.remove(worst)
        dropped.append(worst[0])


def validate(new: str, source: str, facts: dict,
             limit: int = TITLE_MAX) -> tuple[bool, str]:
    if not new.strip():
        return False, "empty"
    if title_length(new) > limit:
        return False, f"too long ({title_length(new)} > {limit})"
    if "&" in new or '"' in new:
        return False, "contains & or quote"
    if not facts.get("years"):
        return False, "no fitment years parsed"
    if not facts.get("make"):
        return False, "make unknown"
    if not facts.get("displacement"):
        return False, "no displacement parsed"
    if "Engine" not in new:
        return False, "lost the word Engine"
    if facts.get("vin_code") and f"VIN {facts['vin_code']}" not in new:
        return False, f"dropped VIN {facts['vin_code']}"
    if new.strip() == source.strip():
        return False, "unchanged"
    return True, ""


def build(groups: dict[str, list[dict]],
          details: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Return accepted and rejected one-title-per-interchange decisions."""
    parsed: dict[str, dict] = {}
    records: dict[str, dict] = {}
    for key in sorted(groups):
        rows = groups[key]
        source = str(rows[0].get("title") or "")
        detail = details.get(str(rows[0]["listing_id"])) or {}
        facts = parse(source, detail)
        parsed[key] = facts
        new, dropped = compose(facts)
        records[key] = {
            "interchange": key, "old_title": source, "new_title": new,
            "dropped": dropped, "facts": facts, "rows": rows,
        }

    seen: dict[str, list[str]] = {}
    for key, record in records.items():
        seen.setdefault(record["new_title"], []).append(key)
    for keys in seen.values():
        if len(keys) > 1:
            for key in keys:
                new, dropped = compose(parsed[key], keep_notes=True)
                records[key]["new_title"] = new
                records[key]["dropped"] = dropped
                records[key]["collided"] = True

    final: dict[str, list[str]] = {}
    for key, record in records.items():
        final.setdefault(record["new_title"], []).append(key)

    accepted, rejected = [], []
    for key in sorted(records):
        record = records[key]
        facts, rows = record.pop("facts"), record.pop("rows")
        ok, reason = validate(record["new_title"], record["old_title"], facts)
        if ok and len(final[record["new_title"]]) > 1:
            ok, reason = False, "title not unique across interchange groups"
        item = {
            **record,
            "old_length": title_length(record["old_title"]),
            "length": title_length(record["new_title"]),
            "make": facts.get("make"), "model": facts.get("model"),
            "make_from_donor": facts.get("make_from_donor", False),
            "notes": facts.get("notes"),
            "listing_ids": [str(row["listing_id"]) for row in rows],
        }
        (accepted if ok else rejected).append(item if ok else {**item, "reason": reason})
    return accepted, rejected
