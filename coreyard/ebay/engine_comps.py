"""Collect and filter public comparable listings for used engine assemblies."""

from __future__ import annotations

import json
import re
import statistics
import time
from pathlib import Path
from urllib.parse import quote_plus

import requests

from coreyard.ebay.market_index import parse_page


_FILLER = {
    "engine", "motor", "assembly", "assy", "complete", "used", "oem", "fits",
    "fit", "for", "and", "the", "with", "without", "thru", "through", "digit",
    "4th", "5th", "6th", "8th", "id", "opt", "option", "base", "style", "new",
    "classic", "naturally", "aspirated", "low", "emissions", "federal",
    "california", "transmission", "manual", "automatic", "cvt", "speed",
}
_IS_ENGINE = re.compile(r"\b(engine|motor)\b", re.I)
_NOT_AN_ENGINE = re.compile(
    r"\b(transmission|transaxle|gearbox|transfer\s*case|differential|axle|"
    r"cylinder\s*head|head\s*gasket|gasket\s*set|turbocharger|supercharger|"
    r"intake\s*manifold|exhaust\s*manifold|manifold|catalytic|converter|muffler|"
    r"valve\s*cover|oil\s*(?:pan|cooler|pump|filter)|timing\s*cover|water\s*pump|"
    r"harness|ecm|ecu|pcm|computer|sensor|injector|ignition\s*coil|coil\s*pack|"
    r"alternator|starter|radiator|condenser|compressor|mounts?|bracket|pulley|"
    r"flywheel|flex\s*plate|crankshaft|camshaft|piston|connecting\s*rod|"
    r"throttle\s*body|fuel\s*pump|strut|shock\s*absorber|control\s*arm|"
    r"fan\s*(?:blade|assembly|clutch)|cooling\s*fan|harmonic\s*balancer|"
    r"clutch\s*kit|torque\s*converter|repair\s*manual|service\s*manual|toy)\b",
    re.I,
)
_WRONG_MARKET = re.compile(
    r"\b(remanufactured|reman|rebuilt|brand\s*new|crate\s*engine|core\s*only|"
    r"for\s*parts|parts\s*only|not\s*working|non[\s-]?running|lot\s*of|\d\s*pcs?)\b",
    re.I,
)
_JDM = re.compile(r"\bjdm\b|\bimported?\s+from\s+japan\b", re.I)
_DISPL = re.compile(r"\b(\d\.\d|\d)\s?L\b", re.I)
_FAMILY = re.compile(r"\b([A-Z]{2,3}\d{2}[A-Z]{0,3}|\d[A-Z]{2}-?F[A-Z]{1,2})\b")
_MILES = re.compile(r"\b(\d{1,3})\s?[kK]\s*(?:mi|miles)\b|\b(\d{2,3},\d{3})\s*miles\b", re.I)
_YEAR = re.compile(r"\b(19|20)(\d{2})\b")
PRICE_MIN, PRICE_MAX = 150.0, 12_000.0
USER_AGENT = "Mozilla/5.0 CoreYard/1.0"


def displacement(text: str) -> str | None:
    match = _DISPL.search(text or "")
    if not match:
        return None
    try:
        return f"{float(match.group(1)):g}"
    except ValueError:
        return None


def family_code(text: str) -> str | None:
    for match in _FAMILY.finditer(text or ""):
        code = match.group(1)
        if not re.fullmatch(r"(19|20)\d{2}", code):
            return code
    return None


def years_in(text: str) -> set[int]:
    return {int(prefix + suffix) for prefix, suffix in _YEAR.findall(text or "")}


def query_for(row: dict, detail: dict | None = None) -> str:
    detail = detail or {}
    title = str(row.get("title") or "")
    size = displacement(title) or displacement(str(detail.get("conditions_options") or ""))
    family = family_code(title)
    query_title = re.sub(r"\bVIN\s+[A-Z0-9]\b", " ", title, flags=re.I)
    if re.search(r"Fits\s+[\d\-]+\s", title):
        make = str(detail.get("make") or "").title().replace(" Truck", "")
        model = str(detail.get("model") or "").strip().title()
        if make:
            model = " ".join(word for word in model.split()
                             if word.lower() != make.lower())
        parts = [str(detail.get("model_year") or "").strip(), make, model]
    else:
        parts = [word for word in re.findall(r"[A-Za-z0-9.\-]+", query_title)
                 if word.lower() not in _FILLER
                 and not re.fullmatch(r"\d\.\d?L?", word, re.I)
                 and not re.fullmatch(r"\d{5,}", word)][:7]
    if size:
        parts.append(f"{float(size):.1f}L")
    if family:
        parts.append(family)
    parts += ["engine", "motor"]
    seen, result = set(), []
    for part in parts:
        part = part.strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            result.append(part)
    return " ".join(result)


def short_query_for(row: dict, detail: dict | None = None) -> str:
    detail = detail or {}
    title = str(row.get("title") or "")
    size = displacement(title) or displacement(str(detail.get("conditions_options") or ""))
    make = str(detail.get("make") or "").title().replace(" Truck", "")
    model = str(detail.get("model") or "").strip().title()
    if make:
        model = " ".join(word for word in model.split() if word.lower() != make.lower())
    if not (make and model):
        rest = re.sub(r"^\s*(?:19|20)\d{2}\s*(?:-\s*(?:19|20)?\d{2})?\s*", "", title)
        values = [word for word in re.findall(r"[A-Za-z0-9-]+", rest)
                  if word.lower() not in _FILLER]
        make, model = (values + ["", ""])[:2]
    return " ".join(part for part in [make, model,
                                      f"{float(size):.1f}L" if size else "", "engine"]
                    if part).strip()


def fetch(query: str, destination: Path, session: requests.Session,
          delay: float = 1.5) -> bool:
    if destination.is_file() and destination.stat().st_size >= 10_000:
        return True
    response = session.get("https://picclick.com/?q=" + quote_plus(query),
                           headers={"User-Agent": USER_AGENT}, timeout=45)
    time.sleep(delay)
    if response.status_code != 200 or len(response.content) < 10_000:
        return False
    destination.write_bytes(response.content)
    return True


def comp_miles(title: str) -> int | None:
    match = _MILES.search(title or "")
    if not match:
        return None
    return int(match.group(1)) * 1000 if match.group(1) else int(
        match.group(2).replace(",", "")
    )


def score(source_title: str, source_displacement: str | None,
          source_family: str | None, source_years: set[int],
          item: dict) -> tuple[int, dict] | None:
    title, price = item["title"], item["price"]
    if not PRICE_MIN <= price <= PRICE_MAX or not _IS_ENGINE.search(title):
        return None
    if _NOT_AN_ENGINE.search(title) or _WRONG_MARKET.search(title):
        return None
    size = displacement(title)
    if source_displacement and size and size != source_displacement:
        return None
    points = 40 if size and size == source_displacement else 0
    family = family_code(title)
    if source_family and family and family.upper() == source_family.upper():
        points += 35
    overlap = years_in(title) & source_years
    points += min(20, 4 * len(overlap))
    source_words = {word.lower() for word in re.findall(r"[A-Za-z]{3,}", source_title)} - _FILLER
    hit_words = {word.lower() for word in re.findall(r"[A-Za-z]{3,}", title)} - _FILLER
    points += min(30, 3 * len(source_words & hit_words))
    if points < 30:
        return None
    return points, {
        "title": title, "price": round(price, 2), "score": points,
        "miles": comp_miles(title), "jdm": bool(_JDM.search(title)),
    }


def comps_for(row: dict, detail: dict | None, page,
              limit: int = 14) -> dict:
    detail = detail or {}
    title = str(row.get("title") or "")
    size = displacement(title) or displacement(str(detail.get("conditions_options") or ""))
    family = family_code(title)
    years = years_in(title)
    if str(detail.get("model_year") or "").isdigit():
        years.add(int(detail["model_year"]))
    pages = [page] if isinstance(page, Path) else list(page)
    picked = []
    for item in [item for source in pages for item in parse_page(source)]:
        found = score(title, size, family, years, item)
        if found:
            picked.append(found[1])
    picked.sort(key=lambda item: (-item["score"], item["price"]))
    seen, unique = set(), []
    for item in picked:
        key = (round(item["price"], 2), item["title"][:40].lower())
        if key not in seen:
            seen.add(key)
            unique.append(item)
    top = unique[:limit]
    prices = sorted(item["price"] for item in top)
    stats = {}
    if prices:
        domestic = sorted(item["price"] for item in top if not item["jdm"])
        stats = {
            "comp_count": len(prices), "comp_low": prices[0],
            "comp_median": round(statistics.median(prices), 2),
            "comp_high": prices[-1],
            "comp_domestic_median": (round(statistics.median(domestic), 2)
                                     if domestic else None),
        }
    return {"query_displacement": size, "family_code": family, "comps": top, **stats}


def collect(groups: dict[str, list[dict]], details: dict[str, dict], pages: Path,
            out: Path, *, delay: float = 1.5, log=print) -> dict[str, dict]:
    pages.mkdir(parents=True, exist_ok=True)
    done = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    session = requests.Session()
    keys = [key for key in sorted(groups) if key not in done]
    for number, key in enumerate(keys, 1):
        row = groups[key][0]
        detail = details.get(str(row["listing_id"])) or {}
        query = query_for(row, detail)
        page = pages / f"{re.sub(r'[^A-Za-z0-9._-]', '_', key)}.html"
        try:
            ok = fetch(query, page, session, delay=delay)
        except Exception as exc:
            log(f"{key}: fetch failed: {type(exc).__name__}: {str(exc)[:90]}")
            ok = False
        record = {"id": key, "query": query, "comps": [], "comp_count": 0}
        if ok:
            record.update(comps_for(row, detail, page))
            record.update({"id": key, "query": query})
        done[key] = record
        if number % 20 == 0 or number == len(keys):
            temporary = out.with_suffix(out.suffix + ".tmp")
            temporary.write_text(json.dumps(done, indent=2) + "\n", encoding="utf-8")
            temporary.replace(out)
            log(f"{number}/{len(keys)} fetched")
    return done
