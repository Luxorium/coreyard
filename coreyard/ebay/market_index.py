"""Parse and price wheel comparables from a public eBay listing index.

Installation-specific research corrections live in ``EBAY_RESEARCH_OVERRIDES_FILE``.
That file is ignored because its interchange keys and manual evidence belong to one yard.
"""

from __future__ import annotations

import html
import json
import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

from coreyard.config import REPO_ROOT, _get
from coreyard.ebay.market_research import FLOORS, material_from_title
from coreyard.ebay.util import groups_by_interchange


MAKES = {
    "bmw", "buick", "cadillac", "chevrolet", "chevy", "chrysler", "dodge",
    "ford", "gmc", "honda", "hyundai", "infiniti", "jeep", "kia", "lexus",
    "mazda", "mercedes", "benz", "mitsubishi", "nissan", "pontiac", "ram",
    "saturn", "smart", "subaru", "toyota", "volkswagen", "volvo",
}
COMMON = {
    "wheel", "wheels", "rim", "rims", "oem", "factory", "original",
    "replacement", "alloy", "aluminum", "steel", "chrome", "clad", "painted",
    "polished", "machined", "silver", "black", "gray", "spoke", "spokes",
    "hole", "holes", "inch", "inches", "front", "rear", "sedan", "coupe",
    "suv", "van", "pickup", "door", "base", "spare", "road", "option", "opt",
    "limited", "with", "without", "fits", "fit", "for", "and", "the",
}
BAD = re.compile(
    r"\b(set|pair|[234]\s*(?:pc|piece|wheels?|rims?)|"
    r"wheel\s*(?:and|&)\s*tire|tires?|tyres?|center\s*cap|hubcap|wheel\s*cover|"
    r"steering|flywheel|simulator|trim\s*ring|skin|insert|repair|service|replica|"
    r"aftermarket|reconditioned|remanufactured|refinished|refurbished|new|"
    r"road\s*ready|rtx)\b", re.I,
)
PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.\d{2})?)")
ITEM_RE = re.compile(
    r'<li id="item-(?P<item>\d+)">.*?'
    r'<h3 title="(?P<title>.*?)".*?</h3>.*?'
    r'<div class="price"><strong>(?P<price>.*?)</strong>', re.S,
)
SIZE_RE = re.compile(r"\b(\d{2})x(\d(?:\.\d+|\s*(?:-\s*)?1/2)?)\b", re.I)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}(?:\s*-\s*(?:(?:19|20)?\d{2}))?\b")


@dataclass(frozen=True)
class ResearchOverrides:
    materials: dict[str, str]
    source_aliases: dict[str, str]
    excludes: dict[str, re.Pattern]
    required_tokens: dict[str, set[str]]
    supplemental_comps: dict[str, list[dict]]


EMPTY_OVERRIDES = ResearchOverrides({}, {}, {}, {}, {})


def load_overrides(path: str | Path | None = None) -> ResearchOverrides:
    configured = path or _get(
        "EBAY_RESEARCH_OVERRIDES_FILE",
        str(REPO_ROOT / "out" / "ebay-research-overrides.json"),
    )
    source = Path(str(configured))
    if not source.is_file():
        return EMPTY_OVERRIDES
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("eBay research overrides must be a version 1 object")
    allowed = {"version", "materials", "source_aliases", "excludes",
               "required_tokens", "supplemental_comps"}
    if set(data) - allowed:
        raise ValueError("eBay research overrides contain unknown keys")
    return ResearchOverrides(
        materials={str(k): str(v) for k, v in (data.get("materials") or {}).items()},
        source_aliases={str(k): str(v)
                        for k, v in (data.get("source_aliases") or {}).items()},
        excludes={str(k): re.compile(str(v), re.I)
                  for k, v in (data.get("excludes") or {}).items()},
        required_tokens={str(k): {str(x).lower() for x in v}
                         for k, v in (data.get("required_tokens") or {}).items()},
        supplemental_comps={str(k): list(v)
                            for k, v in (data.get("supplemental_comps") or {}).items()},
    )


def norm_size(text: str) -> str | None:
    match = SIZE_RE.search((text or "").replace("X", "x"))
    if not match:
        return None
    raw_width = match.group(2)
    width = f"{raw_width.strip()[0]}.5" if "1/2" in raw_width else raw_width
    return f"{int(match.group(1))}x{float(width):g}"


def diameters(text: str) -> set[str]:
    size = norm_size(text)
    if size:
        return {size.split("x")[0]}
    return set(re.findall(
        r'(?<!\d)(1[4-9]|2[0-2])(?=\s*(?:inch|in\b|["”]|\d{4}\b))',
        (text or "").lower(),
    ))


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def spoke_counts(text: str) -> set[str]:
    normalized = (text or "").lower().replace("-spoke", " spoke").replace("spokes", "spoke")
    return set(re.findall(
        r"(?<![\d./x-])\b(\d{1,2})(?=\s+(?:[a-z0-9-]+\s+){0,3}spoke\b)",
        normalized,
    ))


def years(text: str) -> set[int]:
    found: set[int] = set()
    for match in re.finditer(
        r"\b((?:19|20)\d{2})\s*-\s*((?:19|20)?\d{2})\b", text or ""
    ):
        start = int(match.group(1))
        raw_end = match.group(2)
        end = int(raw_end) if len(raw_end) == 4 else start // 100 * 100 + int(raw_end)
        if end < start:
            end += 100
        if end - start <= 30:
            found.update(range(start, end + 1))
    found.update(int(raw) for raw in re.findall(r"\b(?:19|20)\d{2}\b", text or ""))
    for start_raw, end_raw in re.findall(r"(?<!\d)(\d{2})\s*-\s*(\d{2})(?!\d)", text or ""):
        start = 2000 + int(start_raw) if int(start_raw) <= 29 else 1900 + int(start_raw)
        end = 2000 + int(end_raw) if int(end_raw) <= 29 else 1900 + int(end_raw)
        if end >= start and end - start <= 30:
            found.update(range(start, end + 1))
    return found


def identifying_words(title: str) -> set[str]:
    prefix = SIZE_RE.split(title or "", maxsplit=1)[0]
    found = words(YEAR_RE.sub(" ", prefix)) - MAKES - COMMON
    return {word for word in found if len(word) > 1 and not word.isdigit()}


def query_for(title: str) -> str:
    size = norm_size(title) or ""
    prefix = YEAR_RE.sub(" ", SIZE_RE.split(title or "", maxsplit=1)[0])
    keep = [word for word in re.findall(r"[A-Za-z0-9-]+", prefix)
            if word.lower() not in COMMON]
    material = material_from_title(title)
    return " ".join(keep + [size, material if material != "unknown" else "",
                            "wheel", "rim", "OEM"]).strip()


def parse_page(path: Path) -> list[dict]:
    if not path.is_file() or path.stat().st_size < 10_000:
        return []
    raw = path.read_text(errors="ignore")
    result = []
    for match in ITEM_RE.finditer(raw):
        title = html.unescape(re.sub(r"<[^>]+>", " ", match.group("title")))
        prices = PRICE_RE.findall(html.unescape(match.group("price")))
        if prices:
            result.append({
                "item": match.group("item"), "title": title.strip(),
                "price": float(prices[-1].replace(",", "")),
            })
    return result


def expected_material(title: str) -> str:
    material = material_from_title(title)
    if material != "unknown":
        return material
    return "alloy" if any(word in (title or "").lower()
                          for word in ("machined", "polished")) else "unknown"


def select_comps(source: str, items: list[dict], *, material: str | None = None,
                 key: str | None = None,
                 overrides: ResearchOverrides = EMPTY_OVERRIDES) -> list[dict]:
    wanted_size = norm_size(source)
    source_diameters = diameters(source)
    wanted_diameter = wanted_size.split("x")[0] if wanted_size else next(
        iter(source_diameters), None
    )
    identity = identifying_words(source)
    expected = material or expected_material(source)
    wanted_years = years(source)
    styles = spoke_counts(source)
    selected = []
    for item in items:
        title = item["title"]
        low = title.lower()
        if BAD.search(low) or item["price"] < 25 or item["price"] > 900:
            continue
        if key in overrides.excludes and overrides.excludes[key].search(low):
            continue
        title_words = words(title)
        if not ({"wheel", "wheels", "rim", "rims"} & title_words):
            continue
        required = overrides.required_tokens.get(key or "")
        if required and not (required & title_words):
            continue
        comp_years = years(title)
        if wanted_years and comp_years and not (wanted_years & comp_years):
            continue
        comp_size = norm_size(title)
        if comp_size and wanted_size and comp_size != wanted_size:
            continue
        comp_diameters = diameters(title)
        if not comp_size and wanted_diameter and comp_diameters:
            if wanted_diameter not in comp_diameters:
                continue
        overlap = identity & title_words
        if identity and not overlap:
            continue
        if expected == "steel" and ({"alloy", "aluminum", "machined", "polished"} & title_words):
            continue
        if expected == "alloy" and "steel" in title_words:
            continue
        year_match = bool(wanted_years and comp_years and wanted_years & comp_years)
        short_code = re.sub(r"^0+", "", (key or "").split("-")[-1])
        code_signal = short_code.isdigit() and short_code in title_words
        oem_signal = bool({"oem", "oe", "factory", "original", "fits", "fit"} & title_words)
        used_signal = bool({"used", "wear", "ware", "takeoff", "take", "salvage", "from"}
                           & title_words)
        comp_styles = spoke_counts(low)
        exact = (wanted_size is not None and comp_size == wanted_size and year_match
                 and bool(overlap) and (used_signal or code_signal)
                 and (not styles or bool(styles & comp_styles)))
        if not oem_signal and not exact:
            continue
        score = 6 if wanted_size is not None and comp_size == wanted_size else (
            3 if wanted_diameter and wanted_diameter in comp_diameters else 0
        )
        score += min(6, 2 * len(overlap)) + (2 if year_match else 0)
        score += 1 if {"oem", "factory", "original"} & title_words else 0
        score += 3 if code_signal else 0
        if styles and comp_styles:
            style_match = bool(styles & comp_styles)
            if not style_match:
                source_counts = {int(value) for value in styles}
                comp_counts = {int(value) for value in comp_styles}
                doubled = any(word in source.lower() or word in low
                              for word in ("split", "double"))
                style_match = doubled and any(
                    left == 2 * right or right == 2 * left
                    for left in source_counts for right in comp_counts
                )
            if not style_match:
                continue
            score += 2
        if score >= 6:
            selected.append({**item, "score": score})
    unique = {}
    for comp in sorted(selected, key=lambda item: (-item["score"], item["price"])):
        identity_key = (re.sub(r"\W+", " ", comp["title"].lower()).strip(),
                        comp["price"])
        unique.setdefault(identity_key, comp)
    selected = list(unique.values())
    selected.sort(key=lambda item: (-item["score"], item["price"]))
    best = selected[0]["score"] if selected else 0
    return [item for item in selected if item["score"] >= max(6, best - 4)][:20]


def percentile(values: list[float], position: float) -> float:
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * position
    low, high = math.floor(index), math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def selling_price(values: list[float], floor: float) -> float:
    ordered = sorted(values)
    median = statistics.median(ordered)
    core = [value for value in ordered if median * 0.5 <= value <= median * 1.75]
    target = (percentile(core, 0.30) * 0.97 if len(core) >= 4
              else statistics.median(core or ordered) * 0.95)
    return floor if target <= floor else max(floor, math.floor(target / 5) * 5 - 0.01)


def confidence(count: int) -> str:
    return "high" if count >= 5 else "medium" if count >= 3 else "low" if count else "none"


def prepare(rows: list[dict], titles: list[dict], research: list[dict], pages: Path,
            config: Path) -> list[tuple[str, str]]:
    groups = groups_by_interchange(rows)
    proposed = {item["interchange"]: item["new_title"] for item in titles}
    done = {item["id"] for item in research}
    pages.mkdir(parents=True, exist_ok=True)
    pending = []
    lines = []
    for key in sorted(groups):
        page = pages / f"{key}.html"
        if key in done or (page.is_file() and page.stat().st_size >= 10_000):
            continue
        title = proposed.get(key, str(groups[key][0].get("title") or ""))
        query = query_for(title)
        pending.append((key, query))
        lines += [f'url = "https://picclick.com/?q={quote_plus(query)}"',
                  f'output = "{page}"']
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return pending


def build(rows: list[dict], titles: list[dict], research: list[dict], pages: Path,
          *, overrides: ResearchOverrides = EMPTY_OVERRIDES) -> list[dict]:
    groups = groups_by_interchange(rows)
    proposed = {item["interchange"]: item["new_title"] for item in titles}
    done = {item["id"]: dict(item) for item in research if item.get("id") in groups}
    for key, record in done.items():
        group = groups[key]
        record.update({
            "listing_ids": [str(row["listing_id"]) for row in group],
            "quantity": len(group), "description": group[0].get("title"),
            "current_price": group[0].get("price"),
        })
    for key in sorted(groups):
        if key in done:
            continue
        group = groups[key]
        source = overrides.source_aliases.get(
            key, proposed.get(key, str(group[0].get("title") or ""))
        )
        material = overrides.materials.get(key, expected_material(source))
        floor = float(FLOORS[material if material in FLOORS else "steel"])
        comps = select_comps(source, parse_page(pages / f"{key}.html"),
                             material=material, key=key, overrides=overrides)
        comps.extend(overrides.supplemental_comps.get(key, []))
        prices = sorted(float(item["price"]) for item in comps)
        suggested = selling_price(prices, floor) if prices else floor
        evidence_comps = sorted(comps, key=lambda item: item["price"])
        if len(evidence_comps) > 4:
            indexes = sorted({0, len(evidence_comps) // 3,
                              2 * len(evidence_comps) // 3, len(evidence_comps) - 1})
            evidence_comps = [evidence_comps[index] for index in indexes]
        evidence = f"Public active eBay index ({date.today().isoformat()}):\n" + "\n".join(
            f'{item["title"]} — ${float(item["price"]):.2f}' for item in evidence_comps
        )
        if not comps:
            evidence += "No safely matched comparable; floor used and flagged."
        done[key] = {
            "id": key, "material": material, "suggested_price": round(suggested, 2),
            "comp_low": min(prices) if prices else 0,
            "comp_median": round(statistics.median(prices), 2) if prices else 0,
            "comp_high": max(prices) if prices else 0, "comp_count": len(prices),
            "basis": "active" if prices else "none", "confidence": confidence(len(prices)),
            "evidence": evidence, "floor": f"{floor:.2f}",
            "raw_suggested": f"{suggested:.2f}", "floored": suggested <= floor,
            "needs_review": material == "unknown" or len(prices) < 3,
            "listing_ids": [str(row["listing_id"]) for row in group],
            "quantity": len(group), "description": group[0].get("title"),
            "current_price": group[0].get("price"), "source": "public_active_index",
        }
    return [done[key] for key in sorted(done)]
