"""Checkpointed, AI-backed market research for normalized auto-part listings."""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path

from coreyard.ai import ask_json, deadline_now
from coreyard.transform.pricing import parse_money


FLOORS = {"steel": Decimal("74.99"), "alloy": Decimal("99.99")}
SYSTEM = (
    "You research competitive eBay prices for used OEM auto parts from a US auto "
    "recycler. Report real comparables and never invent evidence."
)
RULES = """For each part, search eBay market results and report a competitive single-item
price. Prefer sold/completed evidence, exclude new aftermarket products, sets, wrong
fitment and packages, and report no price when nothing usable exists. Prices are USD and
exclude shipping. Evidence must name only the few comparables that drove the decision."""
SCHEMA = {
    "type": "object",
    "properties": {
        "prices": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "material": {"type": "string",
                                 "enum": ["steel", "alloy", "unknown"]},
                    "suggested_price": {"type": "number"},
                    "comp_low": {"type": "number"},
                    "comp_median": {"type": "number"},
                    "comp_high": {"type": "number"},
                    "comp_count": {"type": "integer"},
                    "basis": {"type": "string",
                              "enum": ["sold", "active", "mixed", "none"]},
                    "confidence": {"type": "string",
                                   "enum": ["high", "medium", "low", "none"]},
                    "evidence": {"type": "string"},
                },
                "required": ["id", "material", "suggested_price", "comp_low",
                             "comp_median", "comp_high", "comp_count", "basis",
                             "confidence", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["prices"],
    "additionalProperties": False,
}


def material_from_title(title: str) -> str:
    value = (title or "").lower()
    if re.search(r"\balloy\b|alum", value):
        return "alloy"
    if re.search(r"\bsteel\b", value) or "road wheel" in value:
        return "steel"
    return "unknown"


def apply_floor(record: dict, source_title: str = "") -> dict:
    reported = record.get("material") or "unknown"
    material = reported if reported != "unknown" else material_from_title(source_title)
    floor = FLOORS.get(material, FLOORS["steel"])
    suggested = parse_money(record.get("suggested_price")) or Decimal("0")
    return {
        **record,
        "material": material,
        "floor": str(floor),
        "raw_suggested": str(suggested),
        "floored": suggested < floor,
        "suggested_price": float(floor if suggested < floor else suggested),
        "needs_review": material == "unknown",
    }


def build_items(groups: dict[str, list[dict]], keys: list[str]) -> list[dict]:
    result = []
    for key in keys:
        row = groups[key][0]
        title = str(row.get("title") or "")
        result.append({
            "id": key, "description": title, "interchange": key,
            "material_hint": material_from_title(title),
            "current_price": str(parse_money(row.get("price")) or 0),
            "quantity": len(groups[key]),
        })
    return result


def research_batch(items: list[dict], *, model_name: str | None = None,
                   deadline: float | None = None):
    prompt = f"{RULES}\n\nResearch these {len(items)} parts:\n{json.dumps(items)}"
    answer, usage = ask_json(
        prompt, SCHEMA, model_name=model_name, system=SYSTEM, allow_web=True,
        deadline=deadline,
    )
    by_id = {item["id"]: item for item in items}
    prices = [
        apply_floor(item, by_id.get(item.get("id"), {}).get("description", ""))
        for item in answer.get("prices", [])
    ]
    return prices, usage


def _write(path: Path, records: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(list(records.values()), indent=2, default=str) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def run(groups: dict[str, list[dict]], out: Path, *, keys: list[str] | None = None,
        batch_size: int = 6, workers: int = 3, model_name: str | None = None,
        resume: bool = True, log=print) -> list[dict]:
    """Research groups concurrently and checkpoint each completed batch."""
    selected = sorted(keys if keys is not None else groups)
    done: dict[str, dict] = {}
    if resume and out.is_file():
        try:
            done = {item["id"]: item
                    for item in json.loads(out.read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            done = {}
    todo = [key for key in selected if key not in done]
    chunks = [todo[start:start + batch_size]
              for start in range(0, len(todo), batch_size)]
    lock = threading.Lock()
    deadline = deadline_now()
    started = time.monotonic()
    completed = 0

    def work(chunk):
        return chunk, research_batch(
            build_items(groups, chunk), model_name=model_name, deadline=deadline
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(work, chunk): chunk for chunk in chunks}
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                _, (priced, _) = future.result()
            except Exception as exc:
                if log:
                    log(f"batch {chunk[0]}: FAILED {type(exc).__name__}: {str(exc)[:180]}")
                continue
            with lock:
                for item in priced:
                    group = groups.get(item["id"])
                    if not group:
                        continue
                    done[item["id"]] = {
                        **item,
                        "listing_ids": [str(row["listing_id"]) for row in group],
                        "quantity": len(group),
                        "description": group[0].get("title"),
                        "current_price": group[0].get("price"),
                    }
                _write(out, done)
                completed += 1
                if log:
                    elapsed = max(time.monotonic() - started, 1)
                    log(f"{completed}/{len(chunks)} batches; {len(done)} researched; "
                        f"{elapsed / 60:.1f}m elapsed")
    _write(out, done)
    return list(done.values())
