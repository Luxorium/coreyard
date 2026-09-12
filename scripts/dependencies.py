#!/usr/bin/env python3
"""What CoreYard installs, what each piece is for, and what it was qualified against.

Two direct dependencies reach twenty-odd packages once their own requirements are resolved,
and "it works here" is not a record of which twenty-odd. This prints the closure actually
installed, with the licence each one declares, so a release can be qualified against a set
somebody can read rather than against whatever the index served that afternoon.

    scripts/dependencies.py                 # the closure, name==version
    scripts/dependencies.py --licences      # ...and what each one is licensed under
    scripts/dependencies.py --write         # record it in requirements-lock.txt
    scripts/dependencies.py --check         # pyproject and requirements.txt still agree

`--check` is the one that belongs in CI. The direct dependencies are declared twice — in
`pyproject.toml` for anyone installing the package and in `requirements.txt` for the
installer script — and two lists of the same thing drift in one direction: the one nobody
runs from gets forgotten, and an installation that worked from a checkout stops working from
a wheel.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import pathlib
import re
import sys

try:                                    # 3.11+
    import tomllib
except ModuleNotFoundError:             # 3.10, which this project supports
    tomllib = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements-lock.txt"

# A requirement line reduced to the name it installs: "requests>=2.31" -> "requests".
_NAME = re.compile(r"^[A-Za-z0-9_.\-]+")


def _key(name: str) -> str:
    return name.lower().replace("_", "-")


#: `[project] dependencies = [...]`, for the interpreters with no `tomllib`. A regex over
#: one array is not a TOML parser and is not pretending to be: it reads the one key this
#: needs, and `--check` compares what it found against `requirements.txt`, so a shape it
#: misread shows up as a disagreement rather than as a silent pass.
_DEPENDENCIES = re.compile(r"^dependencies\s*=\s*\[(.*?)\]", re.M | re.S)


def declared_in_pyproject() -> set[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if tomllib is not None:
        requirements = tomllib.loads(text)["project"]["dependencies"]
    else:
        match = _DEPENDENCIES.search(text)
        if not match:
            raise SystemExit("pyproject.toml has no [project] dependencies array")
        requirements = re.findall(r"[\"']([^\"']+)[\"']", match.group(1))
    names = (_NAME.match(req) for req in requirements)
    return {_key(match.group(0)) for match in names if match}


def declared_in_requirements() -> set[str]:
    out = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # A commented-out requirement is an *optional* one being described, not a declared
        # dependency: `# boto3>=1.34` is documentation of a capability nobody installs by
        # default, and counting it would make the two lists disagree forever.
        if not line or line.startswith("#"):
            continue
        match = _NAME.match(line)
        if match:
            out.add(_key(match.group(0)))
    return out


def closure(roots: set[str]) -> dict[str, str]:
    """Every distribution the roots pull in, as installed here. name -> version."""
    found: dict[str, str] = {}

    def walk(name: str) -> None:
        key = _key(name)
        if key in found:
            return
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            return
        found[key] = dist.version
        for requirement in dist.requires or []:
            # Extras are not installed unless asked for, so they are not in the closure.
            if ";" in requirement and "extra" in requirement.split(";", 1)[1]:
                continue
            match = _NAME.match(requirement.split(";")[0].strip())
            if match:
                walk(match.group(0))

    for root in sorted(roots):
        walk(root)
    return dict(sorted(found.items()))


def licence_of(name: str) -> str:
    try:
        meta = metadata.distribution(name).metadata
    except metadata.PackageNotFoundError:
        return "not installed"
    stated = meta.get("License-Expression") or meta.get("License")
    if stated and stated.strip() and stated.strip().lower() != "unknown":
        return stated.strip().splitlines()[0]
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            return classifier.rsplit("::", 1)[-1].strip()
    return "unstated"


def render(with_licences: bool) -> str:
    lines = []
    for name, version in closure(declared_in_pyproject()).items():
        line = f"{name}=={version}"
        if with_licences:
            line = f"{line:<28} {licence_of(name)}"
        lines.append(line)
    return "\n".join(lines)


HEADER = """\
# The dependency set this release was qualified against, as installed — not an install
# instruction. `requirements.txt` and `pyproject.toml` declare the two direct dependencies
# with floors; this records what those floors actually resolved to when the suite, the
# packaged CI job and the live installation were exercised.
#
# It carries no hashes and pins nothing: installing from it would reproduce this afternoon's
# index rather than verify it, which is a stronger claim than the project can make. Treat it
# as evidence for a release, and regenerate it with `scripts/dependencies.py --write`.
#
# Generated from the qualification environment. Do not edit by hand.
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--licences", "--licenses", dest="licences", action="store_true",
                    help="also print what each distribution says it is licensed under")
    ap.add_argument("--write", action="store_true",
                    help=f"record the closure in {LOCK.name}")
    ap.add_argument("--check", action="store_true",
                    help="verify pyproject.toml and requirements.txt declare the same set")
    args = ap.parse_args(argv)

    if args.check:
        pyproject, requirements = declared_in_pyproject(), declared_in_requirements()
        if pyproject != requirements:
            only_py = ", ".join(sorted(pyproject - requirements)) or "none"
            only_req = ", ".join(sorted(requirements - pyproject)) or "none"
            print("The two dependency declarations disagree:", file=sys.stderr)
            print(f"  only in pyproject.toml:   {only_py}", file=sys.stderr)
            print(f"  only in requirements.txt: {only_req}", file=sys.stderr)
            return 1
        print(f"Dependency declarations agree: {', '.join(sorted(pyproject))}.")
        return 0

    body = render(args.licences)
    if args.write:
        LOCK.write_text(HEADER + render(False) + "\n", encoding="utf-8")
        print(f"Wrote {LOCK.relative_to(ROOT)} ({len(body.splitlines())} distributions).")
        return 0
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
