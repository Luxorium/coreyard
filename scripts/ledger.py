#!/usr/bin/env python3
"""Keep the evidence ledger level with the acceptance criteria.

QA-02 asks for one ledger row per criterion. The failure mode of a hand-maintained ledger
is not that a row is wrong — it is that a criterion has no row at all and so is never
looked at again. This does the one check that prevents that: every criterion in the
acceptance specification has a row, and every row names a criterion that still exists.

It deliberately does *not* judge a row's status. "Missing evidence is NOT VERIFIED, never
PASS" is a decision for the release owner, not for a script, and a tool that could mark its
own criteria PASS would be the least trustworthy thing in the release.

    python scripts/ledger.py            # list any criteria with no ledger row
    python scripts/ledger.py --summary  # count the rows by status
    python scripts/ledger.py --needs    # group what is left by what it is waiting on
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "RELEASE_ACCEPTANCE_v0.1.0.md"
LEDGER = ROOT / "docs" / "EVIDENCE_LEDGER.md"

CRITERION = re.compile(r"\*\*([A-Z]{2,5}-[0-9]{2}) — ([^*]+?)\*\*")
ROW = re.compile(r"^\|\s*`?([A-Z]{2,5}-[0-9]{2})`?\s*\|")
# "N/A" is last so the substring scan below cannot match it inside another word.
STATUSES = ("PASS", "FAIL", "NOT VERIFIED", "IN PROGRESS", "N/A")

# What the row is blocked on, from the key at the top of the ledger. A row with none is the
# failure this column exists to prevent: a criterion nobody can act on because nobody knows
# whether it is waiting for code, for a Shopify store, or for a person to be named.
KNOWN_NEEDS = {
    "done", "offline", "test store", "disposable database", "live yard", "spare host",
    "capacity host", "soak host", "two testers", "release owner", "release act",
    "out of scope", "the rest of this ledger",
}


def criteria() -> dict[str, str]:
    """``{"REL-01": "Supported product."}`` in specification order."""
    text = SPEC.read_text(encoding="utf-8")
    return {code: title.strip() for code, title in CRITERION.findall(text)}


def rows() -> dict[str, str]:
    """``{"REL-01": "<the whole table row>"}`` from the checked-in ledger."""
    if not LEDGER.exists():
        return {}
    found = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line.strip())
        if match:
            found[match.group(1)] = line
    return found


def cells(row: str) -> list[str]:
    """The row's cells, without the empty ones the leading and trailing pipes produce."""
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def status_of(row: str) -> str:
    parts = cells(row)
    return parts[2] if len(parts) > 2 else ""


def needs_of(row: str) -> str:
    parts = cells(row)
    return parts[3] if len(parts) > 3 else ""


def malformed(have: dict[str, str]) -> list[str]:
    """Rows a reader cannot act on: no status, no blocker, or an unexplained N/A."""
    problems = []
    for code, row in sorted(have.items()):
        status, needs = status_of(row), needs_of(row)
        if status not in STATUSES:
            problems.append(f"{code}: status {status!r} is not one of {', '.join(STATUSES)}")
        # A criterion can be waiting on two things at once — code *and* a Shopify store —
        # so the cell is one or more known values joined by " + ".
        unknown = [part for part in needs.split(" + ") if part not in KNOWN_NEEDS]
        if unknown:
            problems.append(
                f"{code}: needs {', '.join(repr(u) for u in unknown)} — not in the key at "
                f"the top of the ledger")
        # An out-of-scope criterion is the one status a reader will not accept on trust.
        if status == "N/A" and len(cells(row)[-1]) < 80:
            problems.append(f"{code}: marked N/A without a reason")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", action="store_true",
                        help="also count the rows by status")
    parser.add_argument("--needs", action="store_true",
                        help="group the unfinished criteria by what they are waiting on")
    args = parser.parse_args(argv)

    wanted, have = criteria(), rows()
    missing = [code for code in wanted if code not in have]
    stale = [code for code in have if code not in wanted]

    for code in missing:
        print(f"no ledger row for {code} — {wanted[code]}", file=sys.stderr)
    for code in stale:
        print(f"ledger row for {code}, which the specification no longer defines",
              file=sys.stderr)

    if args.summary:
        counts = {status: 0 for status in STATUSES}
        for line in have.values():
            status = status_of(line)
            if status in counts:
                counts[status] += 1
        print(f"\n{len(wanted)} criteria, {len(have)} rows")
        for status, count in counts.items():
            print(f"  {status:<13}{count:>4}")

    if args.needs:
        outstanding: dict[str, list[str]] = {}
        for code, line in have.items():
            if status_of(line) in ("PASS", "N/A"):
                continue
            outstanding.setdefault(needs_of(line), []).append(code)
        print(f"\n{sum(len(v) for v in outstanding.values())} criteria outstanding:")
        for needs, codes in sorted(outstanding.items(), key=lambda kv: -len(kv[1])):
            print(f"  {len(codes):>3}  {needs:<24}{' '.join(sorted(codes))}")

    broken = malformed(have)
    for problem in broken:
        print(problem, file=sys.stderr)

    if missing or stale or broken:
        print(f"\n{len(missing)} missing, {len(stale)} stale, {len(broken)} malformed "
              f"— update {LEDGER}", file=sys.stderr)
        return 1
    print(f"{LEDGER.name}: all {len(wanted)} criteria have a row.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
