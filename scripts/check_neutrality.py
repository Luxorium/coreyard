#!/usr/bin/env python3
"""Fail if vendor names or a site's real schema leak into the repository.

CoreYard reads a database you license from someone else, and writes eBay listings through
a portal you license from someone else. Their product names, their table layout and their
field identifiers are theirs, so none of it belongs in this source tree — every
installation supplies those locally (``.env``, ``schema.json`` and ``portal.json``, all
gitignored). The same applies to the identity of whichever yard maintains the project:
this is a tool any yard can run, so its author's business name must be no more visible in
the tree than anyone else's.

``.html`` is scanned as well as the obvious text suffixes. Test fixtures captured from a
real portal or storefront are exactly the kind of file whose provenance is forgotten, and
an unscanned suffix is a hole the rule cannot see through.

This runs in CI so the rule is enforced rather than remembered.

The forbidden terms are stored as SHA-256 digests rather than as words. A checker that
spells out the names it bans defeats its own purpose: this file is public, and a plain
list would put the vendor's name and the maintainer's business name into the very tree
the rule exists to keep them out of. Digests let CI enforce the rule without publishing
the words, or letting a search engine index them. That raises the cost of casual
discovery; it is not a defence against someone who already suspects a term and tests it.

Matching is a case-insensitive substring search, hashed: each window of each forbidden
length is hashed and looked up. Separators are deliberately *not* normalized away, because
they carry the meaning here. A vendor's column names run their words together, while this
project's own neutral fields for the same ideas are ``snake_case`` — the underscore is the
whole difference, and stripping it would make the check unable to tell a leak from the
field it is supposed to be replaced by. Spacing variants of a term therefore need their own
digests, which is why several appear below.

This file is itself checked, which is the point: the first draft of this paragraph named a
forbidden column to illustrate the rule, and CI caught it.

To add a term, hash its lowercased form and add the digest below, along with its length in
``_LENGTHS``:

    python3 -c "import hashlib,sys; t=sys.argv[1].lower(); \\
      print(len(t), hashlib.sha256(t.encode()).hexdigest())" "The Term"
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Digest of the lowercased term -> why it is forbidden, so a failure can explain the rule
# without restating the term it is protecting.
_FORBIDDEN: dict[str, str] = {
    "aa23a758a34d81db5017f7f7aeddf084851aa75b11bd8f0913b27b252f416bfc":
        "a vendor's product or company name",
    "e9265c82274dad2386b239c16021ff5519c35614b549d0df29b1f1fde1fc861d":
        "a vendor's product or company name",
    "7fadc7390f435c6ea66abfe23155be9f3c5486bfe6f14d0b3821c31c85abac0b":
        "a vendor's product or company name",
    "eb26a9dcba5836629d1405636af03fa9b756030f5a1453d30046b0558563da62":
        "the maintaining installation's own identity",
    "856aea3a3e469d93457d58a5baacb838fe067da191b7496b3bf43fc9e68f2484":
        "a specific vendor's table or column name",
    "cfa1fa9d6a700c7121d58b840f28b9014746313a326939c5d873ef109b2f813a":
        "a vendor's product or company name",
    "c51abbc136eebd0fdb84b76dcafd17caaae8732ac929793d1516cdddea53477d":
        "the maintaining installation's own identity",
    "f2fcc43eac804056115bcec2f1e0347229eb48dafe7606ae7645a942e17fa4e2":
        "a vendor's product or company name",
    "afdebf4c32b5b7143915c820b4ded021f76442ec694c858ecf2783989826a7e1":
        "a vendor's product or company name",
    "bebcdf8a30463925a560db13eb14d1542937adc79e5e107a9a69d7b6dc05027e":
        "the maintaining installation's own identity",
    "c744111f345139a6e8c8758f168960e650c747d13c7fa9591b26deca87fdd52a":
        "the maintaining installation's own identity",
    "76e522f2c4e2718dbabc58346ab663fa43abdf0051ebdbce2996fa85c8db5605":
        "a specific vendor's table or column name",
    "45c42c71d1b4eeb26b77d07fdd250a633e0c5a216bff44a4efbad1d5a24a07b6":
        "a specific vendor's table or column name",
    "eec921d8c60b922a535e4356fd9f7c38cb4fe283064f4130f097b15b7eb3c99c":
        "a specific vendor's table or column name",
    "94ca6f39506bd528cb4fdceeb0d41750ddddbcf3ac08ae32f97c218a9c738160":
        "a specific vendor's table or column name",
    "3d82d1ac4c2c1e04283c470d38098104783393d229417bdac35b37870aef7695":
        "the maintaining installation's own identity",
    "712cdc99ac96d27137294e823e14769a9e5632ac69b0bbe95fb1841fad7462c4":
        "a vendor's product or company name",
    "c0067ec9d98d15d47ab4c97801b91e9a4d2c1133901cce0ca5fc31aa5b8c72a4":
        "the maintaining installation's own identity",
    "bff45381f30052252ebcdde2b4f2e4df607e8c4a1ff95b0dc0d841ee8e7be3ae":
        "a specific vendor's table or column name",
    "b205ab3061878fd1819ae5f21680e7cd2b81b7a376247e2809d821f7c3ec33be":
        "a specific vendor's table or column name",
    "4a5ab2ba4291d4e298a94b972190e86de023714d79f5d7f521854309f080405f":
        "a specific vendor's table or column name",
    "ddd8a5c2a44a31fd74f3ff2fc0dc8d8e65c5ec3ceaa5aa81c19cd93cdb8b12c6":
        "a specific vendor's table or column name",
    "c07253cf7f468d5b62a2f1e2880d6e923f6d9836a40d8efafffddd9343b9f102":
        "a specific vendor's table or column name",
    "7e4df6ddf85e2d396d396f8d444092f3bc0fd68e90335e559a6e3687f3b0eaca":
        "a specific vendor's table or column name",
    "2185ab5add8765c1052eba94df72272a527d9096c8bfed9b81efd5507a3572c1":
        "a specific vendor's table or column name",
    "0391c46ac0d649515e1a204d3397b321f0cb91d61a72dc61c8212e8cc9faf026":
        "a specific vendor's table or column name",
}

# Lengths of the forbidden terms — the window sizes worth hashing. Publishing these
# reveals only how long the words are.
_LENGTHS = frozenset({5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 17})

SKIP_DIRS = {".git", ".venv", "out", "__pycache__", "node_modules", ".github/workflows"}
SKIP_FILES = {"schema.json", "portal.json", ".env"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".sh", ".yml", ".yaml", ".toml",
                 ".html", ".example", ""}


def offending_reason(line: str) -> str | None:
    """Why this line violates the policy, or None if it is clean.

    Hashing every window of every forbidden length is what lets the terms themselves stay
    out of this file, while keeping the plain case-insensitive substring behaviour that the
    surrounding code depends on.
    """
    text = line.lower()
    for length in _LENGTHS:
        if len(text) < length:
            continue
        for start in range(len(text) - length + 1):
            digest = hashlib.sha256(text[start:start + length].encode()).hexdigest()
            reason = _FORBIDDEN.get(digest)
            if reason:
                return reason
    return None


def tracked_files() -> list[pathlib.Path] | None:
    """Paths that would be published, or None outside a checkout (e.g. a tarball).

    The rule is about what gets *committed*, so the index is the right thing to read.
    Walking the filesystem instead would flag a correctly-configured installation for its
    own local files — the generated launcher, a populated ``schema.json`` — which is the
    exact opposite of what this check asks people to do.

    Untracked-but-not-ignored files are included deliberately. A brand-new module is
    exactly where a leak arrives, and reading the index alone would let one through
    unexamined until after it had been committed.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
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
    seen = set()
    for path in paths:
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
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
    for path, rel in candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            reason = offending_reason(line)
            if reason:
                failures.append(f"{rel}:{lineno}: contains {reason}")

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
