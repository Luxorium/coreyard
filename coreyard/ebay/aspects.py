"""Derive eBay engine item specifics from normalized portal listing data.

The category vocabulary is installation data fetched from eBay and named by
``EBAY_ASPECTS_FILE``.  No portal form names or category IDs are embedded here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from coreyard.config import REPO_ROOT, _get


class AspectError(ValueError):
    """Aspect metadata or a derived patch is unsafe."""


_VOCAB: dict[str, dict] | None = None
_VOCAB_PATH: str | None = None


def vocab(path: str | Path | None = None, *, required: bool = False) -> dict[str, dict]:
    """Load eBay aspect metadata, or return an empty vocabulary for derivation-only use."""
    global _VOCAB, _VOCAB_PATH
    configured = str(path or _get(
        "EBAY_ASPECTS_FILE", str(REPO_ROOT / "out" / "ebay-item-aspects.json")
    ))
    if _VOCAB is not None and configured == _VOCAB_PATH:
        return _VOCAB
    source = Path(configured)
    if not source.is_file():
        if required:
            raise AspectError(
                f"eBay aspect metadata not found: {source}; set EBAY_ASPECTS_FILE"
            )
        return {}
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AspectError(f"cannot read eBay aspect metadata {source}: {exc}") from exc
    raw = data.get("aspects") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise AspectError("eBay aspect metadata must contain an 'aspects' object")
    _VOCAB, _VOCAB_PATH = raw, configured
    return raw


_BRAND = {
    "ford": "Ford", "lincoln": "Lincoln", "mercury": "Mercury",
    "chevrolet": "Chevrolet", "chevy": "Chevrolet", "gmc": "GMC",
    "buick": "Buick", "cadillac": "Cadillac", "pontiac": "Pontiac",
    "oldsmobile": "Oldsmobile", "saturn": "Saturn", "hummer": "Hummer",
    "dodge": "Dodge", "ram": "Ram", "chrysler": "Chrysler", "jeep": "Jeep",
    "plymouth": "Plymouth", "toyota": "Toyota", "lexus": "Lexus",
    "scion": "Scion", "honda": "Honda", "acura": "Acura",
    "nissan": "Nissan", "infiniti": "Infiniti", "datsun": "Datsun",
    "mazda": "Mazda", "mitsubishi": "Mitsubishi", "subaru": "Subaru",
    "suzuki": "Suzuki", "isuzu": "Isuzu", "hyundai": "Hyundai",
    "kia": "Kia", "genesis": "Genesis", "volkswagen": "Volkswagen",
    "vw": "Volkswagen", "audi": "Audi", "porsche": "Porsche",
    "bmw": "BMW", "mini": "Mini", "mercedes-benz": "Mercedes-Benz",
    "mercedes": "Mercedes-Benz", "volvo": "Volvo", "saab": "Saab",
    "jaguar": "Jaguar", "land rover": "Land Rover", "range rover": "Land Rover",
    "fiat": "Fiat", "alfa romeo": "Alfa Romeo", "maserati": "Maserati",
    "tesla": "Tesla", "smart": "Smart",
}
_BRAND_KEYS = sorted(_BRAND, key=len, reverse=True)

_MODEL_MAKE = {
    "VERSA": "Nissan", "SENTRA": "Nissan", "ALTIMA": "Nissan",
    "MICRA": "Nissan", "ROGUE": "Nissan", "KICKS": "Nissan",
    "MAXIMA": "Nissan", "QUEST": "Nissan", "FOCUS": "Ford",
    "FUSION": "Ford", "CROWN VICTORIA": "Ford", "ESCAPE": "Ford",
    "MUSTANG": "Ford", "FREESTAR": "Ford", "FIESTA": "Ford",
    "ESCORT": "Ford", "FIVE HUNDRED": "Ford", "TAURUS": "Ford",
    "CARAVAN": "Dodge", "CALIBER": "Dodge", "JOURNEY": "Dodge",
    "DAKOTA": "Dodge", "CIVIC": "Honda", "ODYSSEY": "Honda",
    "CR-Z": "Honda", "CR-V": "Honda", "IMPALA": "Chevrolet",
    "HHR": "Chevrolet", "MALIBU": "Chevrolet", "EQUINOX": "Chevrolet",
    "COROLLA": "Toyota", "YARIS": "Toyota", "ECHO": "Toyota",
    "RAV4": "Toyota", "ELANTRA": "Hyundai", "SANTA FE": "Hyundai",
    "LEGACY": "Subaru", "BAJA": "Subaru", "SABLE": "Mercury",
    "VILLAGER": "Mercury", "ALLURE": "Buick", "G6": "Pontiac",
    "ALERO": "Oldsmobile", "VUE": "Saturn", "TL": "Acura",
    "AMANTI": "Kia", "COMPASS": "Jeep", "BEETLE": "Volkswagen",
    "ACCLAIM": "Plymouth", "DEVILLE": "Cadillac",
}
_MODEL_AT_END = re.compile(r"Fits\s+[\d\-]+\s+([A-Z0-9][A-Z0-9 /\-]*?)\s+\d+\s*$")

_LAYOUT = [
    (re.compile(r"\bV[\s-]?(4|6|8|10|12)\b", re.I), "V"),
    (re.compile(r"\b(?:inline|straight|i|l)[\s-]?(3|4|5|6)\b", re.I), "Straight"),
    (re.compile(r"\b(?:flat|boxer|h)[\s-]?(4|6)\b", re.I), "Flat"),
    (re.compile(r"\bW(8|12)\b"), "W"),
]
_CYL_WORD = re.compile(r"\b(\d{1,2})[\s-]?cyl(?:inder)?\b", re.I)
_CYL_CID = re.compile(r"\((\d{1,2})-\d{2,3}\b")
_ENGINE_FAMILY = {
    "QR": ("4", None), "QG": ("4", None), "HR": ("4", None),
    "MR": ("4", None), "SR": ("4", None), "KA": ("4", None),
    "GA": ("4", None), "CA": ("4", None), "VQ": ("6", "V"),
    "VG": ("6", "V"), "VK": ("8", "V"), "VH": ("8", "V"),
    "RB": ("6", "Straight"), "TB": ("6", "Straight"),
    "ZZ": ("4", None), "NZ": ("4", None), "AZ": ("4", None),
    "SZ": ("4", None), "ZR": ("4", None), "AR": ("4", None),
    "NR": ("4", None), "RZ": ("4", None), "GR": ("6", "V"),
    "MZ": ("6", "V"), "VZ": ("6", "V"), "UZ": ("8", "V"),
    "UR": ("8", "V"),
}
_FAMILY_CODES = (
    re.compile(r"\b([A-Z]{2})[A-Z]?\d{1,2}(?:DE|DD|DET|DDT)\b"),
    re.compile(r"\b\d([A-Z]{2})-?F[A-Z]{1,2}\b"),
)
_DISPL = re.compile(r"\b(\d\.\d|\d)\s?L\b", re.I)
_DIESEL_MARQUE = [
    (re.compile(r"power\s*stroke", re.I), {"6", "6.0", "6.4", "6.7", "7.3"},
     ("8", "V")),
    (re.compile(r"duramax", re.I), {"6.6"}, ("8", "V")),
    (re.compile(r"cummins", re.I), {"5.9", "6.7"}, ("6", "Straight")),
]
_ASSEMBLY = [
    (re.compile(r"\blong\s*block\b", re.I), "Engine Long Block"),
    (re.compile(r"\bshort\s*block\b", re.I), "Engine Short Block"),
]
_FUEL = [
    (re.compile(r"\bdiesel\b", re.I), "Diesel"),
    (re.compile(r"\bhybrid\b", re.I), "Hybrid"),
    (re.compile(r"\bcng\b", re.I), "CNG"),
    (re.compile(r"\b(?:lpg|propane)\b", re.I), "LPG"),
    (re.compile(r"\b(?:gasoline|gas|petrol)\b", re.I), "Gasoline"),
    (re.compile(r"\b(?:efi|mfi|spfi|tbi)\b", re.I), "Gasoline"),
    (re.compile(r"\bflex\s*fuel\b", re.I), "Gasoline"),
]
_MILEAGE_BANDS = [
    (10_000, "Less Than 10,000 miles"),
    (25_000, "10,000-24,999 miles"),
    (50_000, "25,000-49,999 miles"),
    (75_000, "50,000-74,999 miles"),
    (100_001, "75,000-100,000 miles"),
]


def canonical(aspect: str, value: str | None, metadata: dict | None = None) -> str | None:
    if not value:
        return None
    meta = (metadata if metadata is not None else vocab()).get(aspect)
    if not meta:
        return value
    for listed in meta.get("values") or ():
        if listed.lower() == value.lower():
            return listed
    return None if meta.get("mode") == "SELECTION_ONLY" else value


def engine_size(text: str, metadata: dict | None = None) -> str | None:
    match = _DISPL.search(text or "")
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return canonical("Engine Size", f"{value:g} L", metadata)


def mileage_band(miles) -> str | None:
    try:
        value = int(float(miles))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    for ceiling, label in _MILEAGE_BANDS:
        if value < ceiling:
            return label
    return "More Than 100,000 miles"


def model_make(title: str) -> str | None:
    match = _MODEL_AT_END.search(title or "")
    return _MODEL_MAKE.get(match.group(1).strip()) if match else None


def brand(*texts: str) -> str | None:
    for text in texts:
        low = f" {(text or '').lower()} "
        for key in _BRAND_KEYS:
            if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", low):
                return _BRAND[key]
    for text in texts:
        found = model_make(text)
        if found:
            return found
    return None


def layout(text: str) -> tuple[str | None, str | None]:
    for pattern, block in _LAYOUT:
        match = pattern.search(text or "")
        if match:
            return match.group(1), block
    for pattern in (_CYL_WORD, _CYL_CID):
        match = pattern.search(text or "")
        if match:
            return match.group(1), None
    for pattern in _FAMILY_CODES:
        for family in pattern.findall(text or ""):
            if family in _ENGINE_FAMILY:
                return _ENGINE_FAMILY[family]
    displacement = _DISPL.search(text or "")
    if displacement:
        for pattern, litres, answer in _DIESEL_MARQUE:
            if pattern.search(text) and displacement.group(1) in litres:
                return answer
    return None, None


def fuel_type(text: str) -> str | None:
    for pattern, value in _FUEL:
        if pattern.search(text or ""):
            return value
    return None


def derive(row: dict, detail: dict | None = None,
           metadata: dict | None = None) -> dict[str, str]:
    """Build safe engine aspects from normalized grid and detail fields."""
    detail = detail or {}
    title = str(row.get("title") or "")
    conditions = str(detail.get("conditions_options") or
                     row.get("conditions_options") or "")
    combined = f"{title} {conditions}"
    out: dict[str, str] = {}
    found_brand = brand(str(detail.get("make") or ""), title)
    if found_brand:
        out["Brand"] = found_brand
    size = engine_size(title, metadata) or engine_size(conditions, metadata)
    if size:
        out["Engine Size"] = size
    cylinders, block = layout(combined)
    for name, value in (
        ("Number of Cylinders", cylinders),
        ("Block Type", block),
        ("Fuel Type", fuel_type(combined)),
        ("Mileage", mileage_band(detail.get("mileage") or row.get("mileage"))),
    ):
        accepted = canonical(name, value, metadata)
        if accepted:
            out[name] = accepted
    interchange = row.get("interchange_number") or detail.get("interchange_number")
    if interchange:
        out["Interchange Part Number"] = str(interchange)
    out["Type"] = "Complete Assembly"
    for pattern, value in _ASSEMBLY:
        if pattern.search(combined):
            out["Type"] = value
            break
    out["Performance Part"] = "No"
    out["Universal Fitment"] = "No"
    return out


def patch_key(name: str) -> str:
    return name.replace(" ", "_")


def to_patch(values: dict[str, str], warranty: str = "") -> dict[str, str]:
    out = {patch_key(key): value for key, value in values.items()}
    if warranty:
        out["Warranty"] = warranty
    return out


def coverage(rows: list[dict], details: dict[str, dict] | None = None,
             metadata: dict | None = None) -> dict[str, int]:
    details = details or {}
    tally: dict[str, int] = {}
    for row in rows:
        for key in derive(row, details.get(str(row.get("listing_id"))), metadata):
            tally[key] = tally.get(key, 0) + 1
    return dict(sorted(tally.items(), key=lambda item: -item[1]))
