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
STATUSES = ("PASS", "FAIL", "NOT VERIFIED", "IN PROGRESS")


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", action="store_true",
                        help="also count the rows by status")
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
            for status in STATUSES:
                if f"| {status} " in line or f"|{status}|" in line:
                    counts[status] += 1
                    break
        print(f"\n{len(wanted)} criteria, {len(have)} rows")
        for status, count in counts.items():
            print(f"  {status:<13}{count:>4}")

    if missing or stale:
        print(f"\n{len(missing)} missing, {len(stale)} stale — update {LEDGER}",
              file=sys.stderr)
        return 1
    print(f"{LEDGER.name}: all {len(wanted)} criteria have a row.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
