"""AI-assisted eBay SEO titles for normalized non-engine portal listings."""

from __future__ import annotations

import json
import re
from pathlib import Path

from coreyard.ai import AIError, ask_json, batches, deadline_now
from coreyard.ebay.util import TITLE_MAX, groups_by_interchange, title_length


SYSTEM = (
    "You write accurate eBay listing titles for a US auto recycler. Optimize for buyer "
    "search terms, preserve every fitment fact, and never invent condition claims."
)
RULES = f"""Hard rules:
1. Maximum {TITLE_MAX} effective characters; aim to use the available search surface.
2. Never use & or a quote character. Write 'and' instead of '&'.
3. Lead with year range, make and model, then the part and its specifics.
4. Add a make only when certain. If uncertain, omit it and use low confidence.
5. Preserve every hard source specific such as size, material, trim and drivetrain.
6. Include common buyer synonyms where they fit, such as Wheel Rim.
7. Drop a trailing internal stock number.
8. Never add condition, warranty or shipping claims.
9. Use Title Case and no trailing punctuation.
Return one entry per input id in the same order."""
SCHEMA = {
    "type": "object",
    "properties": {
        "titles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "confidence": {"type": "string",
                                   "enum": ["high", "medium", "low"]},
                    "note": {"type": "string"},
                },
                "required": ["id", "title", "confidence", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["titles"],
    "additionalProperties": False,
}


def _payload(groups: dict[str, list[dict]], keys: list[str]) -> list[dict]:
    return [
        {
            "id": key,
            "current_title": str(groups[key][0].get("title") or ""),
            "part_type": groups[key][0].get("part_type"),
            "interchange": groups[key][0].get("interchange_number"),
            "quantity": len(groups[key]),
        }
        for key in keys
    ]


def validate(title: str, source: dict) -> tuple[bool, str]:
    if not title.strip():
        return False, "empty"
    if title_length(title) > TITLE_MAX:
        return False, f"too long ({title_length(title)} > {TITLE_MAX})"
    if "&" in title or '"' in title:
        return False, "contains & or quote"
    if title.strip() == str(source.get("current_title") or "").strip():
        return False, "unchanged"
    for size in set(re.findall(
        r"\b\d{2}x\d(?:\.\d)?\b", str(source.get("current_title") or "")
    )):
        if size not in title:
            return False, f"dropped size {size}"
    return True, ""


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def generate(rows: list[dict], out: Path, *, batch_size: int = 40,
             model_name: str | None = None, resume: bool = True,
             log=print) -> tuple[list[dict], list[dict]]:
    """Generate checkpointed, guarded proposals per interchange group."""
    groups = groups_by_interchange(rows)
    accepted: list[dict] = []
    rejected: list[dict] = []
    done: set[str] = set()
    if resume and out.is_file():
        try:
            accepted = json.loads(out.read_text(encoding="utf-8"))
            done = {str(item["interchange"]) for item in accepted}
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            accepted = []
    keys = [key for key in sorted(groups) if key not in done]
    deadline = deadline_now()
    for number, keyset in enumerate(batches(keys, batch_size), 1):
        items = _payload(groups, keyset)
        prompt = f"{RULES}\n\nRewrite these {len(items)} parts:\n{json.dumps(items)}"
        try:
            answer, usage = ask_json(
                prompt, SCHEMA, model_name=model_name, system=SYSTEM,
                deadline=deadline, log=log,
            )
        except AIError:
            _write(out, accepted)
            raise
        by_id = {item["id"]: item for item in items}
        for proposal in answer.get("titles", []):
            source = by_id.get(proposal.get("id"))
            if source is None:
                rejected.append({**proposal, "reason": "unknown id"})
                continue
            ok, reason = validate(str(proposal.get("title") or ""), source)
            record = {
                "interchange": proposal["id"],
                "old_title": source["current_title"],
                "new_title": proposal["title"],
                "length": title_length(proposal["title"]),
                "confidence": proposal.get("confidence"),
                "note": proposal.get("note", ""),
                "listing_ids": [str(row["listing_id"])
                                for row in groups[proposal["id"]]],
            }
            (accepted if ok else rejected).append(
                record if ok else {**record, "reason": reason}
            )
        _write(out, accepted)
        if log:
            log(f"batch {number}: {len(keyset)} groups, "
                f"{usage.get('output_tokens', '?')} output tokens")
    _write(out, accepted)
    return accepted, rejected
