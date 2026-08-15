#!/usr/bin/env python3
"""Fail if vendor names or a site's real schema leak into the repository.

CoreYard reads a database you license from someone else. Their product names and their
table layout are theirs, so neither belongs in this source tree — every installation
supplies those locally (``.env`` and ``schema.json``, both gitignored).

This runs in CI so the rule is enforced rather than remembered.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Names that must never appear: the software, its vendor, that vendor's parent, and
# product-specific share/group names.
FORBIDDEN_TERMS = [
    "powerlink", "powerstation", "hollander", "solera", "plimages", "plusers", "elink",
]

# Column and table names belonging to a specific vendor's schema.
FORBIDDEN_SCHEMA = [
    "inventoryid", "stockticketnumber", "interchangenumber", "inventorynumber",
    "carline_makes", "interchangelist", "parttypenbr", "intchnbr", "ecomdescription",
    "blockonlinesale", "quantityavailable", "retailprice", "partrating",
]

SKIP_DIRS = {".git", ".venv", "out", "__pycache__", "node_modules", ".github/workflows"}
SKIP_FILES = {"schema.json", ".env", "check_neutrality.py"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".sh", ".yml", ".yaml", ".toml", ".example", ""}


def tracked_files() -> list[pathlib.Path] | None:
    """Paths git is tracking, or None outside a checkout (e.g. an unpacked tarball).

    The rule is about what gets *committed*, so the index is the right thing to read.
    Walking the filesystem instead would flag a correctly-configured installation for its
    own local files — the generated launcher, a populated ``schema.json`` — which is the
    exact opposite of what this check is asking people to do.
    """
    try:
        proc = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [pathlib.Path(p) for p in proc.stdout.split("\0") if p]


def candidate_files():
    tracked = tracked_files()
    paths = (
        [ROOT / rel for rel in tracked] if tracked is not None
        else [p for p in ROOT.rglob("*") if p.is_file()]
    )
    for path in paths:
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.name in SKIP_FILES:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield path, rel


def main() -> int:
    failures: list[str] = []
    patterns = [(t, re.compile(re.escape(t), re.I)) for t in FORBIDDEN_TERMS + FORBIDDEN_SCHEMA]
    for path, rel in candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for term, pattern in patterns:
                if pattern.search(line):
                    failures.append(f"{rel}:{lineno}: contains {term!r}")

    if failures:
        print("Vendor-neutrality check FAILED:\n")
        for f in failures[:40]:
            print("  " + f)
        if len(failures) > 40:
            print(f"  ... and {len(failures) - 40} more")
        print("\nThese belong in local config (.env / schema.json), not in the repository.")
        return 1
    print("Vendor-neutrality check passed: no vendor names or site schema in the tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
