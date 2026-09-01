"""Condition-aware pricing of one physical unit, over pre-collected comparables.

:mod:`coreyard.ebay.comps` answers what a *part like this* sells for: it scores public
listings against the facts the renderer knows and takes a median.  That is the market, and
it is the same market for every unit in an interchange group.  This module answers the
second question — what is *this* unit worth — because mileage, grade, whether anyone could
test it, a stated defect, and how long it has sat can make two units of one interchange
worth very different amounts, and the comparable median says nothing about any of them.

It is arithmetic, not judgement: a market anchor, a list of named percentage adjustments,
and hard guards over the result.  Every price carries the adjustments that produced it in
``reasoning``, so a reviewer reads why rather than trusting a number.  That is the whole
reason this is a program — an answer nobody can check is not reviewable at 20,000
listings, and the yard cannot afford to have one person read them all either way.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal
from pathlib import Path

from coreyard.transform.pricing import charm, parse_money

# The last-resort floor, for a caller that supplies no better one. A part type's own
# floor is anchored to the yard's retail price (``comps.floor_for``) and reaches this
# module on the group, because a table of 143 hand-maintained floors would still say
# nothing about the individual part in front of it.
FLOOR = Decimal("199.99")
MAX_OVER_MEDIAN = Decimal("1.6")

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
                max_comps: int = 10, mileage_matters: bool = True) -> list[dict]:
    """Assemble one priceable item per interchange group.

    ``mileage_matters`` says whether an absent mileage is a fact about the part or just a
    field this part type never fills in. On an engine it is a fact — an engine sold without
    a reading is worth less than one with a low one — so an unknown reading is discounted.
    On a tail lamp the donor's odometer is barely a signal and is usually absent anyway, and
    discounting every one of them for it would mark down a whole part type for nothing.
    """
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
            "floor": comp.get("floor"),
            "mileage_matters": mileage_matters,
            "comps": [{
                "price": item["price"], "miles": item.get("miles", 0),
                "jdm": bool(item.get("jdm")), "title": str(item["title"])[:78],
            } for item in (comp.get("comps") or [])[:max_comps]],
            "listings": listings,
        })
    return items


def apply_guards(record: dict, listing: dict, group: dict) -> dict:
    """Enforce the floor, the comp ceiling and the disclosure hold over a proposed price.

    Deliberately separate from the calculation that proposed it. A guard that lives inside
    the thing it guards is not a guard, and these three are the ones whose failure is
    expensive: a price under the floor gives the part away, one far over the comparables
    never sells, and an undisclosed defect is a return and a defect case.

    ``group["floor"]`` carries this part type's own floor when the caller knows it.
    """
    out = dict(record)
    suggested = parse_money(record.get("suggested_price")) or Decimal("0")
    floor = parse_money(group.get("floor")) or FLOOR
    out.update(raw_suggested=str(suggested), floor=str(floor))
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
    out["floored"] = suggested < floor
    if out["floored"]:
        flags.append("market cleared below the price floor")
        suggested = floor
    if record.get("needs_disclosure") or _DEFECT.search(listing.get("conditions") or ""):
        out["needs_disclosure"] = True
        flags.append("condition caveat must be disclosed before publishing")
    if not group.get("comp_count"):
        flags.append("no comparables found")
    out["capped"] = any(item.startswith("capped") for item in flags)
    out["suggested_price"] = float(suggested.quantize(Decimal("0.01")))
    out["review_flags"] = flags
    return out


def price_listing(listing: dict, group: dict) -> dict:
    """Price one unit from its comparable median and its own condition.

    Conservative and explainable by construction. It never invents a market anchor: with no
    supplied median it returns no price at all rather than a guess, because a listing left
    at the yard's own price is a listing that merely does not improve, while an invented one
    is wrong in a direction nobody can predict.
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
    if not group.get("mileage_matters", True):
        pass
    elif miles == 0:
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
    # The same charm rule the catalogue and the comparable search use, rounded down so the
    # fallback never becomes less competitive than the evidence behind it.
    suggested = charm(raw, round_down=True)
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


def price_all(groups: dict[str, list[dict]], details: dict, comps: dict,
                       output: Path, *, keys: list[str] | None = None,
                       resume: bool = True) -> list[dict]:
    """Price every listing in the selected groups, checkpointing as it goes.

    Resumable: a listing already in ``output`` is left alone, so a run stopped by its
    deadline resumes where it stopped rather than repricing what it had already decided.
    """
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
            record = price_listing(listing, group)
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
