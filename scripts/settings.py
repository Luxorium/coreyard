#!/usr/bin/env python3
"""Every setting CoreYard reads, generated from the code that reads it.

A configuration reference written by hand is a configuration reference that is wrong within
two releases: a key gets renamed, a default moves, an option is added for one installation
and never written down. This walks the source for `config._get` and `config.flag` calls and
reports what it finds — the key, whether it is text or a switch, the default the code falls
back to, whether it refuses to run without it, and which part of the program asks.

    scripts/settings.py              # the table
    scripts/settings.py --write      # regenerate docs/SETTINGS.md
    scripts/settings.py --check      # the document and .env.example still match the code

`--check` belongs in CI for the same reason the functionality inventory's does: a document
that describes settings the code no longer has is worse than no document, because it is
believed.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Every other script here does this, and for the same reason: run as `python
# scripts/settings.py`, `sys.path[0]` is `scripts/`, so the package beside it is not
# importable — which is how this passed in a venv with CoreYard installed and failed in CI,
# where the hygiene job installs the requirements and not the package.
sys.path.insert(0, str(ROOT))
# The scripts are configured surface too: `healthcheck.py` reads its own notifier command,
# and a settings reference that stopped at the package would omit it while `.env.example`
# documents it.
SOURCES = (ROOT / "coreyard", ROOT / "scripts")
DOC = ROOT / "docs" / "SETTINGS.md"
EXAMPLE = ROOT / ".env.example"

# Grouped by the prefix the key already carries rather than by the module that reads it:
# `config.py` reads nearly all of them, so grouping by reader would file the database
# settings under the storefront. The prefixes are the namespacing an operator already sees
# in `.env`.
_GROUPS = (
    ("YMS_", "Source database"),
    ("SMB_", "SMB transport — the database pipe and the photo share"),
    ("SHOPIFY_", "Shopify"),
    ("STORE_", "Storefront policy"),
    ("SOURCE_", "Source selection and freshness"),
    ("COREYARD_", "CoreYard behaviour"),
)


# The accessors a setting can be read through, and the type each one imposes. A setting read
# through `_duration` is a duration whether or not anybody wrote that down, which is the
# whole reason this is generated rather than maintained.
_ACCESSORS = {
    "_get": "text",
    "_text": "text",
    "flag": "switch",
    "_duration": "duration (e.g. `45m`, `2h`)",
    "_positive_int": "whole number",
    "_positive_float": "number",
    "_retention_days": "whole number of days",
}

#: Read from the real environment only, never from `.env` — the location of the settings
#: file cannot itself be configured inside that file.
_ENVIRONMENT_ONLY = {"COREYARD_HOME", "XDG_DATA_HOME"}


def group_of(key: str) -> str:
    for prefix, name in _GROUPS:
        if key.startswith(prefix):
            return name
    return "Other"


class Setting:
    def __init__(self, key: str, kind: str, default, required: bool, where: str):
        self.key = key
        self.kind = kind
        self.default = default
        self.required = required
        self.where = {where}

    def merge(self, other: "Setting") -> None:
        self.where |= other.where
        # A key read as required anywhere is required: the run that needs it is the one that
        # decides, and reporting the laxer call site would describe a program that starts.
        self.required = self.required or other.required
        if self.default in (None, "") and other.default not in (None, ""):
            self.default = other.default

    @property
    def area(self) -> str:
        return group_of(self.key)

    @property
    def environment_only(self) -> bool:
        return self.key in _ENVIRONMENT_ONLY

    def shown_default(self) -> str:
        if self.required:
            return "— (required)"
        if self.default is None:
            return "unset"
        if self.default == "":
            return "empty"
        return f"`{self.default}`"


def _environ_read(node: ast.Call) -> bool:
    """Is this `os.environ.get(...)` rather than some other object's `.get`?"""
    func = node.func
    return (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ")


def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None


def collect() -> dict[str, Setting]:
    found: dict[str, Setting] = {}
    for path in sorted(p for source in SOURCES for p in source.rglob("*.py")):
        relative = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            key = _literal(node.args[0]) if node.args else None
            if not isinstance(key, str) or not key.isupper() or "_" not in key:
                continue
            if name == "get" and _environ_read(node):
                # `os.environ.get(KEY)`, which is how the settings that decide where `.env`
                # itself lives have to be read.
                kind = "text"
            elif name in _ACCESSORS:
                kind = _ACCESSORS[name]
            else:
                continue
            default = _literal(node.args[1]) if len(node.args) > 1 else None
            required = any(kw.arg == "required" and _literal(kw.value) is True
                           for kw in node.keywords)
            setting = Setting(key, kind, default, required, relative)
            if key in found:
                found[key].merge(setting)
            else:
                found[key] = setting
    return dict(sorted(found.items()))


def documented_in_example() -> set[str]:
    keys = set()
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("#").strip()
        if "=" in line and line.split("=", 1)[0].isupper():
            keys.add(line.split("=", 1)[0].strip())
    return keys


HEADER = """\
# Settings

Generated by `scripts/settings.py` from the code that reads each key — do not edit by hand.
Every setting CoreYard looks at is here, whether or not an installation sets it.

**Precedence, once, for all of them:** a real environment variable wins; otherwise the value
in `.env` under the data root; otherwise the default in the table. `.env` never overrides an
environment variable that is already set, so a one-off `SOURCE_MAX_AGE_HOURS=0 coreyard sync`
does what it looks like it does. Comments must be on their own line — the parser does not
strip a trailing `#`.

A **switch** is true for `1`, `true`, `yes` or `on`, and false for `0`, `false`, `no`, `off`
or empty. Everything else is text; where a number is expected, the text is parsed as one.

A setting marked **required** has no usable default: the command that needs it refuses to
start and names the key. Which commands need which is what `coreyard doctor` reports, by
capability rather than by key.

Renamed keys keep working — `config._LEGACY_KEYS` maps the old spelling to the new one and
the first run that sees an old name says so on stderr.

"""


def legacy_names() -> dict[str, str]:
    """Old spellings that still work, so the table can say so where it matters."""
    from coreyard.config import _LEGACY_KEYS

    return dict(_LEGACY_KEYS)


def render() -> str:
    settings = collect()
    legacy = legacy_names()
    lines = [HEADER]
    order = [name for _, name in _GROUPS] + ["Other"]
    for area in [a for a in order if any(s.area == a for s in settings.values())]:
        rows = [s for s in settings.values() if s.area == area]
        lines.append(f"## {area}\n")
        lines.append("| Setting | Kind | Default | Read by |")
        lines.append("|---|---|---|---|")
        for setting in rows:
            where = ", ".join(f"`{w}`" for w in sorted(setting.where))
            if setting.environment_only:
                where += " — environment only, never read from `.env`"
            if setting.key in legacy:
                where += f" — was `{legacy[setting.key]}`, which still works"
            lines.append(f"| `{setting.key}` | {setting.kind} | "
                         f"{setting.shown_default()} | {where} |")
        lines.append("")
    lines.append(f"{len(settings)} settings.\n")
    return "\n".join(lines)


def check() -> int:
    settings = collect()
    problems = []
    if not DOC.exists():
        problems.append(f"{DOC.relative_to(ROOT)} does not exist; run --write")
    elif DOC.read_text(encoding="utf-8") != render():
        problems.append(f"{DOC.relative_to(ROOT)} is stale; run --write")

    # `.env.example` is what a new installation copies, so a setting it names that no longer
    # exists is a line somebody will set and wonder about.
    example = documented_in_example()
    unknown = example - set(settings) - set(legacy_names().values())
    if unknown:
        problems.append(".env.example names settings the code does not read: "
                        + ", ".join(sorted(unknown)))
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(f"{len(settings)} settings documented; .env.example names "
          f"{len(example)} of them and nothing else.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help=f"regenerate {DOC.name}")
    ap.add_argument("--check", action="store_true",
                    help="fail if the document or .env.example disagrees with the code")
    args = ap.parse_args(argv)
    if args.check:
        return check()
    if args.write:
        DOC.parent.mkdir(parents=True, exist_ok=True)
        DOC.write_text(render(), encoding="utf-8")
        print(f"wrote {DOC.relative_to(ROOT)} ({len(collect())} settings)")
        return 0
    print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
