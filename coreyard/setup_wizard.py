"""``coreyard init`` — get a working installation without reading the manual first.

Setting CoreYard up meant hand-authoring a ``.env`` with forty-odd keys and, before any of
it did anything, a ``schema.json`` mapping a database nobody had described. That is a fine
ask of the person who wrote the tool and an unreasonable one of anybody else, and it is the
single biggest reason CoreYard was not really runnable by another yard.

This writes the two files a site actually has to own, from answers with sensible defaults,
and refuses to overwrite either without being told twice.

``--demo`` skips the questions entirely and points a fresh installation at the bundled
seven-part export, so the whole pipeline can be run — rendered, diffed, written to CSV —
on a laptop with no database, no photo share and no credentials. That path exists because
"try it" should cost a minute, not an afternoon.

Nothing here writes to a store or a database. It writes two local files.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

from coreyard.config import DATA_ROOT, bundled, cli_name

# Where the demo yard lands in *this installation*, and what `.env` therefore names. The CSV
# itself ships inside the package; an installed wheel has no checkout to point at, and a data
# root that is not the checkout would not find one anyway.
DEMO_RELATIVE = Path("examples") / "parts.csv"
DEMO_SOURCE = f"tabular:{DEMO_RELATIVE.as_posix()}"


def bundled_demo_csv() -> Path:
    """The example yard as shipped with the package."""
    return bundled("parts.csv")


def install_demo_source(data_root: Path = DATA_ROOT) -> Path:
    """Copy the bundled example yard into ``data_root`` and return where it landed.

    Copied rather than referenced in place so the demo behaves like a real source: it sits
    with the installation's own files, it can be opened and edited to see what a column does,
    and nothing points into a package directory that an upgrade replaces. An existing file is
    left alone — someone who edited the example meant to.
    """
    target = data_root / DEMO_RELATIVE
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundled_demo_csv(), target)
    return target

# Only the keys a site genuinely has to decide. The rest keep their documented defaults,
# and `.env.example` remains the full reference — a generated file nobody can read is not
# an improvement on a manual one.
_ENV_TEMPLATE = """\
# Written by `coreyard init`. See .env.example for every supported setting.

# --- identity -----------------------------------------------------------------
# The business name on every product, and the location shown in descriptions.
SHOPIFY_VENDOR={vendor}
STORE_CITY={city}
STORE_WARRANTY={warranty}

# The storefront's primary key: every product handle is "<prefix>-<R#>".
# Choose it once, before the first publish. Changing it on a live store makes every
# product look new and duplicates the whole catalogue.
SHOPIFY_HANDLE_PREFIX={handle_prefix}

# --- where inventory comes from -----------------------------------------------
{source_block}
# --- storefront policy --------------------------------------------------------
# Claims, weights, shipping and order handling, as one file. See store.json.
STORE_FILE={store_file}

# Only publish parts that have at least one photograph.
STORE_REQUIRE_IMAGES={require_images}
"""

_DATABASE_BLOCK = """\
# The source database, described by schema.json (copy schema.example.json and fill it in).
# `coreyard schema` introspects a live server and ranks the likely tables.
YMS_DB_HOST={db_host}
YMS_DB_PORT=1433
YMS_DB_NAME={db_name}
YMS_DB_USER={db_user}
YMS_DB_PASSWORD={db_password}

# The photo share. Photographs are named "<R#>_*".
SMB_HOST={smb_host}
SMB_SERVER_NAME={smb_server}
SMB_USER={smb_user}
SMB_PASSWORD={smb_password}
SMB_IMAGES_SHARE={smb_share}
SMB_INVENTORY_SUBDIR=Inventory
SMB_VEHICLE_SUBDIR=Vehicle
"""

_TABULAR_BLOCK = """\
# A CSV or SQLite export, with columns named after Part fields. No database, no share,
# no credentials. See coreyard/source/tabular.py for the accepted column names.
COREYARD_SOURCE={source}
# Photographs named "<R#>_*", if you have them.
#COREYARD_SOURCE_IMAGES=
"""


def render_env(answers: dict) -> str:
    """The ``.env`` body for these answers. Pure, so a test can read it."""
    source = str(answers.get("source") or "database")
    if source.startswith("tabular"):
        block = _TABULAR_BLOCK.format(source=source)
    else:
        block = _DATABASE_BLOCK.format(
            db_host=answers.get("db_host", ""), db_name=answers.get("db_name", ""),
            db_user=answers.get("db_user", ""), db_password=answers.get("db_password", ""),
            smb_host=answers.get("smb_host", ""), smb_server=answers.get("smb_server", ""),
            smb_user=answers.get("smb_user", ""), smb_password=answers.get("smb_password", ""),
            smb_share=answers.get("smb_share", ""),
        )
    return _ENV_TEMPLATE.format(
        vendor=answers.get("vendor", ""), city=answers.get("city", ""),
        warranty=answers.get("warranty", ""),
        handle_prefix=answers.get("handle_prefix", "coreyard"),
        source_block=block, store_file=answers.get("store_file", "store.json"),
        require_images="true" if answers.get("require_images") else "false",
    )


def render_store(answers: dict) -> dict:
    """The starting ``store.json``.

    Deliberately close to empty. Every section is optional and an absent one means the
    neutral default, so a generated file full of guessed policy would be worse than none:
    it would put claims in a seller's mouth that nobody chose.
    """
    profile: dict = {}
    if answers.get("warranty"):
        profile["_comment"] = ("Wording and claims this yard is willing to make. "
                               "Every key is optional; see coreyard/profile.py.")
    return {
        "version": 1,
        "profile": profile or {
            "_comment": ("Wording and claims this yard is willing to make. Every key is "
                         "optional and defaults to CoreYard's neutral policy; see "
                         "coreyard/profile.py for the full list.")
        },
    }


def _ask(prompt: str, default: str = "", *, interactive: bool = True,
         secret: bool = False) -> str:
    if not interactive:
        return default
    shown = f" [{default}]" if default else ""
    try:
        if secret:
            import getpass
            answer = getpass.getpass(f"{prompt}{shown}: ")
        else:
            answer = input(f"{prompt}{shown}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return answer.strip() or default


def collect(args, *, interactive: bool) -> dict:
    """Answers, from flags where given and questions where not."""
    if args.demo:
        return {"vendor": args.vendor or "Demo Yard", "city": args.city or "Springfield, IL",
                "warranty": "", "handle_prefix": args.handle_prefix or "coreyard",
                "source": DEMO_SOURCE, "store_file": str(args.store),
                "require_images": False}

    answers = {
        "vendor": args.vendor or _ask("Business name", interactive=interactive),
        "city": args.city or _ask("City, state", interactive=interactive),
        "warranty": args.warranty or _ask(
            "Warranty phrase (blank for none)", interactive=interactive),
        "handle_prefix": args.handle_prefix or _ask(
            "Shopify handle prefix", "coreyard", interactive=interactive),
        "store_file": str(args.store),
        "require_images": bool(args.require_images),
    }
    source = args.source or _ask(
        "Inventory source: 'database', or 'tabular:<path>' for a CSV/SQLite export",
        "database", interactive=interactive)
    answers["source"] = source
    if not source.startswith("tabular"):
        for key, prompt, secret in (
            ("db_host", "Database host", False), ("db_name", "Database name", False),
            ("db_user", "Database user", False), ("db_password", "Database password", True),
            ("smb_host", "Photo share host", False),
            ("smb_server", "Photo share server name", False),
            ("smb_user", "Photo share user", False),
            ("smb_password", "Photo share password", True),
            ("smb_share", "Photo share name", False),
        ):
            answers[key] = _ask(prompt, interactive=interactive, secret=secret)
    return answers


def write(answers: dict, env_path: Path, store_path: Path, *,
          force: bool = False) -> list[str]:
    """Write both files, refusing to clobber. Returns what was written."""
    blocked = [str(p) for p in (env_path, store_path) if p.exists() and not force]
    if blocked:
        raise FileExistsError(
            f"{', '.join(blocked)} already exist(s). Pass --force to overwrite, or point "
            "--env/--store somewhere else."
        )
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(render_env(answers), encoding="utf-8")
    # The .env holds credentials whenever a database source is configured.
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(render_store(answers), indent=2) + "\n",
                          encoding="utf-8")
    return [str(env_path), str(store_path)]


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    ap.description = "Write a working .env and store.json for this installation."
    ap.add_argument("--demo", action="store_true",
                    help="configure the bundled example export; no database or share")
    ap.add_argument("--yes", action="store_true",
                    help="never prompt; use flags and defaults only")
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    ap.add_argument("--vendor", default="")
    ap.add_argument("--city", default="")
    ap.add_argument("--warranty", default="")
    ap.add_argument("--handle-prefix", dest="handle_prefix", default="")
    ap.add_argument("--source", default="",
                    help="'database' (default) or 'tabular:<path>'")
    ap.add_argument("--require-images", action="store_true",
                    help="only publish parts that have a photograph")
    ap.add_argument("--env", type=Path, default=DATA_ROOT / ".env")
    ap.add_argument("--store", type=Path, default=DATA_ROOT / "store.json")
    ap.set_defaults(func=run)
    return ap


def run(args) -> int:
    interactive = not (args.yes or args.demo) and sys.stdin.isatty()
    if not interactive and not (args.demo or args.vendor or args.yes):
        print("Refusing to guess: pass --demo to try CoreYard on the bundled example, "
              "--vendor to configure a real yard, or run this on a terminal.",
              file=sys.stderr)
        return 2
    answers = collect(args, interactive=interactive)
    if args.demo:
        install_demo_source(Path(args.env).parent)
    try:
        written = write(answers, Path(args.env), Path(args.store), force=args.force)
    except FileExistsError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1

    print("Wrote:")
    for path in written:
        print(f"  {path}")
    cli = cli_name()
    print("\nNext:")
    if str(answers.get("source", "")).startswith("tabular"):
        print(f"  {cli} validate                        # check the config")
        print(f"  {cli} sync --sink csv --dry-run       # render the example yard")
    else:
        # The bundled path, not a bare filename: an installed CoreYard has no checkout to
        # copy from, and the instruction has to work where it is printed.
        print(f"  cp {bundled('schema.example.json')} \\\n     {DATA_ROOT / 'schema.json'}")
        print("                                               # then fill in each PLACEHOLDER")
        print(f"  {cli} schema                          # introspect and rank tables")
        print(f"  {cli} doctor                          # check connectivity")
        print(f"  {cli} sync --dry-run                  # a full pass, writing nothing")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = add_arguments(argparse.ArgumentParser(prog="coreyard init"))
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
