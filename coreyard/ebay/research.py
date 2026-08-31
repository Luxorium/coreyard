"""Condition-aware engine pricing over pre-collected eBay comparables.

Research runs per listing, because mileage, grade, test status, defects, and time in
inventory can make two engines in one interchange group worth very different amounts.
Comparables are supplied to the model rather than searched on every call; results are
checkpointed after every batch and all hard price/disclosure guards are enforced in code.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from decimal import Decimal
from pathlib import Path

from coreyard.ai import AIError, RateLimited, ask_json, batches, deadline_now
from coreyard.transform.pricing import parse_money

FLOOR = Decimal("199.99")
MAX_OVER_MEDIAN = Decimal("1.6")

SYSTEM = """You price used OEM engine assemblies for a US auto-recycling yard's eBay
catalogue. You receive real eBay comparables that were already collected and filtered.
Never invent a comparable and never cite one absent from the supplied list."""

RULES = """Price every listing individually, even when listings share an interchange.

1. Anchor on the supplied comparables, not the current yard price. Prefer domestic_median.
   Imported low-mileage units marked jdm are ceiling context, not the domestic anchor.
2. Target a sale within 30-45 days: at or slightly below the domestic median for an
   average engine.
3. Adjust for THIS engine's mileage, tested/not-tested note, days in inventory, and grade.
   miles 0 means unknown, not zero. Older stock must never be adjusted upward for age.
4. needs_disclosure is true only for an explicit defect/missing component or an explicit
   inability to test. The mere absence of a TESTED note is normal, not a disclosure.
5. With no usable comparable, return confidence none and suggested_price 0. Never guess.
6. USD excluding shipping, one engine, never below $199.99.
7. reasoning is one sentence. evidence is the two supplied comparables that drove the
   result, one per line, copied rather than invented.

Return exactly one entry for every supplied listing_id."""

SCHEMA = {
    "type": "object",
    "properties": {"prices": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "listing_id": {"type": "string"},
            "suggested_price": {"type": "number"},
            "market_anchor": {"type": "number"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low", "none"]},
            "basis": {"type": "string", "enum": ["domestic", "jdm", "mixed", "none"]},
            "needs_disclosure": {"type": "boolean"},
            "condition_summary": {"type": "string"},
            "reasoning": {"type": "string"},
            "evidence": {"type": "string"},
        },
        "required": [
            "listing_id", "suggested_price", "market_anchor", "confidence", "basis",
            "needs_disclosure", "condition_summary", "reasoning", "evidence",
        ],
        "additionalProperties": False,
    }}},
    "required": ["prices"],
    "additionalProperties": False,
}

_DEFECT = re.compile(
    r"\bneeds?\b|bad\s+crank|\bbroken\b|\bcrack|pull and check|check if core|"
    r"could not test|cannot test|not tested|no start", re.I
)


def group_by_interchange(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = str(row.get("interchange_number") or f"_listing_{row['listing_id']}")
        groups.setdefault(key, []).append(row)
    return groups


def _age_days(detail: dict, today: dt.date) -> int | None:
    for key in ("date_inventoried", "date_created"):
        match = re.match(r"(\d+)/(\d+)/(\d{4})", str(detail.get(key) or ""))
        if not match:
            continue
        try:
            value = dt.date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
        except ValueError:
            continue
        return (today - value).days
    return None


def build_items(groups: dict[str, list[dict]], details: dict, comps: dict,
                keys: list[str], today: dt.date | None = None,
                max_comps: int = 10) -> list[dict]:
    today = today or dt.date.today()
    items = []
    for key in keys:
        comp = comps.get(key) or {}
        listings = []
        for row in groups[key]:
            listing_id = str(row["listing_id"])
            detail = details.get(listing_id) or {}
            try:
                miles = int(float(detail.get("mileage") or 0))
            except (TypeError, ValueError):
                miles = 0
            listings.append({
                "listing_id": listing_id,
                "title": row.get("title") or "",
                "miles": miles,
                "grade": detail.get("grade") or "Ungraded",
                "conditions": detail.get("conditions_options") or "",
                "days_in_inventory": _age_days(detail, today),
                "current_price": str(parse_money(row.get("price")) or 0),
            })
        items.append({
            "interchange": key,
            "comp_count": comp.get("comp_count") or 0,
            "domestic_median": comp.get("comp_domestic_median"),
            "comp_median": comp.get("comp_median"),
            "comp_low": comp.get("comp_low"),
            "comp_high": comp.get("comp_high"),
            "comps": [{
                "price": item["price"], "miles": item.get("miles", 0),
                "jdm": bool(item.get("jdm")), "title": str(item["title"])[:78],
            } for item in (comp.get("comps") or [])[:max_comps]],
            "listings": listings,
        })
    return items


def apply_guards(record: dict, listing: dict, group: dict) -> dict:
    """Make price floors, comp ceilings, and defect holds independent of the model."""
    out = dict(record)
    suggested = parse_money(record.get("suggested_price")) or Decimal("0")
    out.update(raw_suggested=str(suggested), floor=str(FLOOR))
    flags: list[str] = []
    if suggested <= 0:
        out.update(floored=False, capped=False, suggested_price=0.0,
                   review_flags=["no price returned"])
        return out
    median = group.get("domestic_median") or group.get("comp_median")
    if median:
        ceiling = Decimal(str(median)) * MAX_OVER_MEDIAN
        if suggested > ceiling:
            flags.append(f"capped from ${suggested} to {MAX_OVER_MEDIAN}x comp median")
            suggested = ceiling
    out["floored"] = suggested < FLOOR
    if out["floored"]:
        flags.append("market cleared below the price floor")
        suggested = FLOOR
    if record.get("needs_disclosure") or _DEFECT.search(listing.get("conditions") or ""):
        out["needs_disclosure"] = True
        flags.append("condition caveat must be disclosed before publishing")
    if not group.get("comp_count"):
        flags.append("no comparables found")
    out["capped"] = any(item.startswith("capped") for item in flags)
    out["suggested_price"] = float(suggested.quantize(Decimal("0.01")))
    out["review_flags"] = flags
    return out


def research_batch(items: list[dict], *, model_name: str | None = None,
                   deadline: float | None = None):
    prompt = (
        f"{RULES}\n\nPrice {sum(len(x['listings']) for x in items)} engine listings "
        f"across {len(items)} interchange groups:\n\n"
        f"{json.dumps(items, separators=(',', ': '), indent=1)}"
    )
    answer, usage = ask_json(
        prompt, SCHEMA, model_name=model_name, system=SYSTEM, allow_web=False,
        deadline=deadline
    )
    sources = {listing["listing_id"]: (listing, group)
               for group in items for listing in group["listings"]}
    priced = []
    for result in answer.get("prices", []):
        source = sources.get(str(result.get("listing_id")))
        if not source:
            continue
        listing, group = source
        record = apply_guards(result, listing, group)
        record.update({
            "interchange": group["interchange"], "title": listing["title"],
            "miles": listing["miles"], "grade": listing["grade"],
            "conditions": listing["conditions"],
            "days_in_inventory": listing["days_in_inventory"],
            "current_price": listing["current_price"],
            "comp_count": group.get("comp_count") or 0,
            "comp_domestic_median": group.get("domestic_median"),
            "comp_median": group.get("comp_median"),
        })
        priced.append(record)
    return priced, usage


def deterministic_record(listing: dict, group: dict) -> dict:
    """Price from the real comp median when the subscription model is rate-limited.

    This is intentionally conservative and explainable. It never invents a market anchor:
    without a supplied median it returns no price, exactly like the model path. Existing AI
    decisions are preserved by :func:`fill_deterministic`; this fills only missing rows.
    """
    anchor = group.get("domestic_median") or group.get("comp_median")
    comps = group.get("comps") or []
    if not anchor:
        return apply_guards({
            "listing_id": listing["listing_id"], "suggested_price": 0,
            "market_anchor": 0, "confidence": "none", "basis": "none",
            "needs_disclosure": False, "condition_summary": "No usable comparables",
            "reasoning": "No supplied market median was available, so no price was guessed.",
            "evidence": "",
        }, listing, group)

    factor = Decimal("0.96")
    reasons = ["96% of the supplied domestic median"]
    miles = int(listing.get("miles") or 0)
    if miles == 0:
        factor -= Decimal("0.05")
        reasons.append("unknown mileage -5%")
    elif miles < 60000:
        factor += Decimal("0.15")
        reasons.append("under 60k miles +15%")
    elif miles < 100000:
        factor += Decimal("0.07")
        reasons.append("under 100k miles +7%")
    elif miles >= 200000:
        factor -= Decimal("0.20")
        reasons.append("200k+ miles -20%")
    elif miles >= 150000:
        factor -= Decimal("0.10")
        reasons.append("150k+ miles -10%")

    conditions = str(listing.get("conditions") or "")
    lower = conditions.lower()
    explicit_untested = bool(re.search(
        r"could not test|cannot test|not tested|pull and check|check if core|no start", lower
    ))
    tested = bool(re.search(r"\btested\b", lower)) and not explicit_untested
    defect = bool(_DEFECT.search(conditions))
    if tested:
        factor += Decimal("0.08")
        reasons.append("tested +8%")
    if explicit_untested:
        factor -= Decimal("0.20")
        reasons.append("explicitly untested -20%")
    if defect and not explicit_untested:
        factor -= Decimal("0.25")
        reasons.append("stated defect -25%")

    grade = str(listing.get("grade") or "").strip().upper()
    if grade.startswith("A"):
        factor += Decimal("0.05")
        reasons.append("grade A +5%")
    elif grade.startswith("C"):
        factor -= Decimal("0.10")
        reasons.append("grade C -10%")
    days = listing.get("days_in_inventory")
    if isinstance(days, int) and days > 1095:
        factor -= Decimal("0.15")
        reasons.append("over three years old -15%")
    elif isinstance(days, int) and days > 730:
        factor -= Decimal("0.10")
        reasons.append("over two years old -10%")
    elif isinstance(days, int) and days > 365:
        factor -= Decimal("0.05")
        reasons.append("over one year old -5%")

    raw = Decimal(str(anchor)) * max(factor, Decimal("0.40"))
    # Familiar .99 ending, rounded down so the fallback never becomes less competitive.
    suggested = Decimal(math.floor(float(raw))) - Decimal("0.01")
    nearest = sorted(comps, key=lambda item: abs(float(item["price"]) - float(anchor)))[:2]
    evidence = "\n".join(
        f"${float(item['price']):.2f} - {str(item['title'])[:100]}" for item in nearest
    )
    count = int(group.get("comp_count") or 0)
    confidence = "high" if count >= 5 else "medium" if count >= 3 else "low"
    basis = "domestic" if group.get("domestic_median") else "jdm"
    record = apply_guards({
        "listing_id": listing["listing_id"], "suggested_price": float(suggested),
        "market_anchor": float(anchor), "confidence": confidence, "basis": basis,
        "needs_disclosure": defect, "condition_summary": conditions[:160],
        "reasoning": "; ".join(reasons) + ".", "evidence": evidence,
    }, listing, group)
    return record


def _checkpoint(path: Path, records: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(list(records.values()), indent=2, default=str) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def fill_deterministic(groups: dict[str, list[dict]], details: dict, comps: dict,
                       output: Path, *, keys: list[str] | None = None,
                       resume: bool = True) -> list[dict]:
    """Fill missing researched listings from comps without replacing completed AI work."""
    done: dict[str, dict] = {}
    if resume and output.is_file():
        done = {str(item["listing_id"]): item
                for item in json.loads(output.read_text(encoding="utf-8"))}
    selected = sorted(keys if keys is not None else groups)
    current_ids = {str(row["listing_id"]) for key in selected for row in groups[key]}
    done = {key: value for key, value in done.items() if key in current_ids}
    for group in build_items(groups, details, comps, selected):
        for listing in group["listings"]:
            listing_id = listing["listing_id"]
            if listing_id in done:
                continue
            record = deterministic_record(listing, group)
            record.update({
                "interchange": group["interchange"], "title": listing["title"],
                "miles": listing["miles"], "grade": listing["grade"],
                "conditions": listing["conditions"],
                "days_in_inventory": listing["days_in_inventory"],
                "current_price": listing["current_price"],
                "comp_count": group.get("comp_count") or 0,
                "comp_domestic_median": group.get("domestic_median"),
                "comp_median": group.get("comp_median"),
                "method": "deterministic-comp-fallback",
            })
            done[listing_id] = record
    _checkpoint(output, done)
    return list(done.values())


def run(groups: dict[str, list[dict]], details: dict, comps: dict, output: Path,
        *, keys: list[str] | None = None, batch_size: int = 20,
        model_name: str | None = None, resume: bool = True, log=print) -> list[dict]:
    keys = sorted(keys if keys is not None else groups)
    done: dict[str, dict] = {}
    if resume and output.is_file():
        try:
            done = {str(item["listing_id"]): item
                    for item in json.loads(output.read_text(encoding="utf-8"))}
        except (OSError, ValueError, KeyError, TypeError):
            done = {}
    current_ids = {str(row["listing_id"]) for key in keys for row in groups[key]}
    done = {key: value for key, value in done.items() if key in current_ids}
    todo = [key for key in keys
            if any(str(row["listing_id"]) not in done for row in groups[key])]
    remaining = sum(len(groups[key]) for key in todo)
    log(f"{len(keys)} groups: {len(done)} listings already priced, "
        f"{remaining} listings across {len(todo)} groups to go")
    deadline = deadline_now()
    for number, chunk in enumerate(batches(todo, batch_size), 1):
        try:
            priced, usage = research_batch(
                build_items(groups, details, comps, chunk), model_name=model_name,
                deadline=deadline
            )
        except (RateLimited, AIError) as exc:
            log(f"  stopped before batch {number}: {exc}")
            break
        for record in priced:
            done[str(record["listing_id"])] = record
        _checkpoint(output, done)
        log(f"  batch {number}: +{len(priced)} ({len(done)} total), "
            f"{usage.get('output_tokens', 0):,} output tokens")
    return list(done.values())
