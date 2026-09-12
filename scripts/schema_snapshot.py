#!/usr/bin/env python3
"""Record the shape of the source database CoreYard reads, so an upgrade can be checked.

CoreYard reaches the yard system through a hand-written map (`schema.json`) naming tables
and columns the vendor never promised to keep. That is fine until the vendor ships a major
version, at which point "will this still work?" is a question nobody can answer from the
release notes: they list features, not column definitions, and the change that breaks a
mapping is exactly the one too boring to announce.

So take a snapshot before the upgrade and compare after. What comes out is not an opinion
about the release, it is the diff of the surface this installation actually depends on.

    scripts/schema_snapshot.py --out out/schema-before.json     # before the upgrade
    scripts/schema_snapshot.py --compare out/schema-before.json # after it

Exit status is 0 when nothing CoreYard depends on changed, 1 when something did, so it can
gate an upgrade in a runbook. Read-only: it queries INFORMATION_SCHEMA and the source
application's own counter rows, and writes only the file you name.

The tables come from `schema.json`, not from a list in here — a site that maps a table this
script never heard of still gets it captured, and this file stays free of any installation's
schema, which is the same rule the rest of the tree follows.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from coreyard.config import DATA_ROOT  # noqa: E402

_NOT_A_TABLE = {"dbo", "information_schema", "sys", "select", "where"}

# Tables CoreYard writes to rather than only reads. A new NOT NULL column with no default is
# harmless in a table we SELECT from and fatal in one we INSERT into, because the insert
# names its columns explicitly and cannot supply a value for one it has never heard of.
#
# Read out of the site's own order-write mapping rather than listed here: the table names
# belong to an installation, not to this project, and a site that books work orders into a
# differently-named table deserves the same warning rather than a wrong one.
_WRITES = re.compile(r"\b(?:INSERT\s+(?:INTO\s+)?|UPDATE\s+)(?:dbo\.)?"
                     r"([A-Za-z_][A-Za-z0-9_]*)", re.I)


# Every section that writes, not just the first one. A write path whose tables are missing
# from this set is checked as though CoreYard only read them, which is the softer of the two
# verdicts: the upgrade that adds a required column to it is reported as news rather than as
# the broken insert it will be on the next run.
_WRITE_SECTIONS = ("order_write", "invoice_write", "backlink_write")


def write_tables(schema: dict) -> set[str]:
    """Tables the configured write paths insert into or update."""
    sections = [schema.get(name) for name in _WRITE_SECTIONS]
    blob = json.dumps([s for s in sections if isinstance(s, (dict, list))])
    return {name for name in _WRITES.findall(blob)
            if name.lower() not in _NOT_A_TABLE}


def counter_tables(schema: dict) -> list[str]:
    """The tables a write path allocates ids from, in the site's own spelling.

    Named by the mapping rather than by this file for the same reason as `write_tables`:
    the counter table belongs to an installation. Hardcoding one yard's name meant every
    other site captured no counters at all and was never told — and counters are the part
    of an upgrade that fails quietly, by handing out an id the application also intends
    to issue.
    """
    found: list[str] = []
    for name in _WRITE_SECTIONS:
        section = schema.get(name)
        if not isinstance(section, dict):
            continue
        for key, statement in section.items():
            if not key.startswith("counter_") or not isinstance(statement, str):
                continue
            for database, table in _QUALIFIED.findall(statement):
                entry = f"{database}.{table}" if database else table
                if table.lower() not in _NOT_A_TABLE and entry not in found:
                    found.append(entry)
    return found


# `dbo.NAME` is unambiguous. A bare FROM/JOIN is not: the map carries prose in its comment
# keys, and "from the source database" reads exactly like a table reference to a regex.
_QUALIFIED = re.compile(r"\b(?:([A-Za-z_][A-Za-z0-9_]*)\.)?dbo\.([A-Za-z_][A-Za-z0-9_]*)")
_FROM_JOIN = re.compile(
    r"\b(?:FROM|JOIN)\s+((?:[A-Za-z_][A-Za-z0-9_]*\.){0,2}[A-Za-z_][A-Za-z0-9_]*)", re.I)


def mapped_tables(schema_path: Path) -> tuple[list[str], list[str]]:
    """``(all candidates, the confidently-mapped ones)``.

    Both are returned because they answer different questions. Everything found is captured,
    since a table this script guessed wrong about costs one empty lookup — but only a
    `dbo.NAME` reference is certain enough to complain about when it turns out not to exist.
    Warning on the guesses reported `dbo`, `hol3` and the word "closed" as missing tables,
    which is a check lit for a non-problem and teaches people to skim past it.
    """
    blob = schema_path.read_text(encoding="utf-8")
    # A three-part name reaches a *different database* on the same server. The interchange
    # catalogue lives in one, so a snapshot that only read the yard database would miss the
    # tables a catalogue update is most likely to rebuild — and miss them silently, which is
    # the worst way for a pre-upgrade check to be wrong.
    certain = {f"{db}.{table}" if db else table
               for db, table in _QUALIFIED.findall(blob)
               if table.lower() not in _NOT_A_TABLE}
    guessed = set()
    for match in _FROM_JOIN.findall(blob):
        parts = match.split(".")
        table = parts[-1]
        if table.lower() in _NOT_A_TABLE:
            continue
        guessed.add(f"{parts[0]}.{table}" if len(parts) == 3 else table)
    return sorted(certain | guessed), sorted(certain)


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def capture(tables: list[str], counters: list[str] | None = None) -> dict:
    """Column definitions for the named tables, plus the counters the write paths allocate
    from and the server's own version string."""
    from coreyard.yms.db import connect, query

    snapshot: dict = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
        "counters": [],
        "server": "",
    }
    with connect() as conn:
        rows = query(conn, "SELECT CAST(@@VERSION AS varchar(200)) AS v")
        snapshot["server"] = str(dict(rows[0])["v"]).splitlines()[0] if rows else ""

        # Group by database: INFORMATION_SCHEMA is per-database, so a cross-database
        # reference has to be asked of that database's own catalogue view.
        by_database: dict[str, list[str]] = {}
        for entry in tables:
            database, _, table = entry.rpartition(".")
            by_database.setdefault(database, []).append(table)

        for database, plain in sorted(by_database.items()):
            view = f"{database}.INFORMATION_SCHEMA.COLUMNS" if database \
                else "INFORMATION_SCHEMA.COLUMNS"
            names = ", ".join(_quote(t) for t in plain)
            try:
                rows = query(conn, f"""
                    SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE,
                           CAST(ISNULL(CHARACTER_MAXIMUM_LENGTH, -1) AS int) AS maxlen,
                           CAST(CASE WHEN IS_NULLABLE = 'YES' THEN 1 ELSE 0 END AS int) AS nullable,
                           CAST(CASE WHEN COLUMN_DEFAULT IS NULL THEN 0 ELSE 1 END AS int) AS has_default
                      FROM {view}
                     WHERE TABLE_NAME IN ({names})
                     ORDER BY TABLE_NAME, ORDINAL_POSITION""")
            except Exception as exc:
                # A database this account cannot read is worth saying out loud rather than
                # quietly omitting: the comparison after the upgrade would then report every
                # one of its tables as unchanged, having never looked at them.
                snapshot.setdefault("unreadable", []).append(
                    {"database": database or "(current)",
                     "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
                continue
            for row in rows:
                d = dict(row)
                table = str(d["TABLE_NAME"])
                key = f"{database}.{table}" if database else table
                snapshot["tables"].setdefault(key, {})[str(d["COLUMN_NAME"])] = {
                    "type": str(d["DATA_TYPE"]),
                    "maxlen": int(d["maxlen"]),
                    "nullable": bool(int(d["nullable"])),
                    "has_default": bool(int(d["has_default"])),
                }

        # The write paths allocate ids by incrementing these rows. A renamed counter or a
        # changed increment does not fail loudly — it books a work order under an id the
        # application also intends to issue. Which table holds them is the site's to say.
        for entry in counters or []:
            database, _, table = entry.rpartition(".")
            prefix = f"{database}." if database else ""
            try:
                rows = query(conn, "SELECT CAST(TableName AS varchar(64)) AS t,"
                                   " CAST(CounterName AS varchar(64)) AS c,"
                                   " CAST(Increment AS int) AS inc"
                                   f" FROM {prefix}dbo.{table}"
                                   " ORDER BY TableName, CounterName")
                snapshot["counters"].extend(
                    {"source": entry,
                     "table": str(dict(r)["t"]).strip(),
                     "counter": str(dict(r)["c"]).strip(),
                     "increment": int(dict(r)["inc"])} for r in rows)
            except Exception as exc:      # a site may shape its counters differently
                snapshot["counters"].append(
                    {"source": entry, "error": f"{type(exc).__name__}: {exc}"})
    return snapshot


def compare(before: dict, after: dict,
            writes: set[str] | None = None) -> tuple[list[str], list[str]]:
    """``(breaking, informational)`` differences between two snapshots.

    ``writes`` names the tables the site's order write touches; an addition is
    judged against those and a read-only table is never broken by one.
    """
    writes = {name.lower() for name in (writes or set())}
    breaking: list[str] = []
    notes: list[str] = []

    if before.get("server") != after.get("server"):
        notes.append(f"server version: {before.get('server')!r} -> {after.get('server')!r}")

    old_tables, new_tables = before.get("tables", {}), after.get("tables", {})
    for table in sorted(set(old_tables) - set(new_tables)):
        breaking.append(f"{table}: table is gone")
    for table in sorted(set(new_tables) - set(old_tables)):
        notes.append(f"{table}: new table")

    for table in sorted(set(old_tables) & set(new_tables)):
        old_cols, new_cols = old_tables[table], new_tables[table]
        for column in sorted(set(old_cols) - set(new_cols)):
            breaking.append(f"{table}.{column}: column is gone")
        for column in sorted(set(new_cols) - set(old_cols)):
            spec = new_cols[column]
            # Only a table we insert into can be broken by an addition, and only when the
            # new column demands a value we have no way to supply.
            if table.lower() in writes and not spec["nullable"] and not spec["has_default"]:
                breaking.append(
                    f"{table}.{column}: new NOT NULL column with no default, and CoreYard "
                    f"inserts into {table} — the insert names its columns and cannot fill it")
            else:
                notes.append(f"{table}.{column}: new column ({spec['type']})")
        for column in sorted(set(old_cols) & set(new_cols)):
            old, new = old_cols[column], new_cols[column]
            if old["type"] != new["type"]:
                breaking.append(
                    f"{table}.{column}: type {old['type']} -> {new['type']}")
            elif old["maxlen"] != new["maxlen"]:
                if 0 <= new["maxlen"] < old["maxlen"]:
                    breaking.append(
                        f"{table}.{column}: narrowed {old['maxlen']} -> {new['maxlen']}")
                else:
                    notes.append(
                        f"{table}.{column}: widened {old['maxlen']} -> {new['maxlen']}")
            if old["nullable"] and not new["nullable"] and table.lower() in writes:
                breaking.append(f"{table}.{column}: became NOT NULL in a table we write")

    old_counters = {(c.get("table"), c.get("counter")): c for c in before.get("counters", [])}
    new_counters = {(c.get("table"), c.get("counter")): c for c in after.get("counters", [])}
    for key in sorted(set(old_counters) - set(new_counters), key=lambda k: tuple(map(str, k))):
        breaking.append(f"counter {key[0]}.{key[1]}: gone — the order write allocates ids here")
    for key in sorted(set(old_counters) & set(new_counters), key=lambda k: tuple(map(str, k))):
        old_inc, new_inc = old_counters[key].get("increment"), new_counters[key].get("increment")
        if old_inc != new_inc:
            breaking.append(
                f"counter {key[0]}.{key[1]}: increment {old_inc} -> {new_inc}")
    return breaking, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", metavar="PATH", help="write a snapshot here")
    ap.add_argument("--compare", metavar="PATH", help="compare the live schema against this")
    ap.add_argument("--schema", metavar="PATH", default=str(DATA_ROOT / "schema.json"),
                    help="the site's schema map (default: the installation's)")
    args = ap.parse_args(argv)

    schema_path = Path(args.schema)
    if not schema_path.exists():
        print(f"no schema map at {schema_path}", file=sys.stderr)
        return 2
    mapping = json.loads(schema_path.read_text(encoding="utf-8"))
    tables, certain = mapped_tables(schema_path)
    counters = counter_tables(mapping)
    print(f"Reading {len(tables)} mapped table(s) from the source database ...")
    live = capture(tables, counters)
    found = sum(len(cols) for cols in live["tables"].values())
    print(f"  {len(live['tables'])} table(s), {found} column(s), "
          f"{len(live['counters'])} counter row(s)")
    # Compared case-insensitively: the server resolves dbo.Carline to CARLINE without
    # complaint, so warning about the spelling would report a working mapping as broken.
    present = {name.lower() for name in live["tables"]}
    for table in certain:
        if table.lower() not in present:
            print(f"  ! {table} is mapped but is not in the database", file=sys.stderr)
    for gap in live.get("unreadable", []):
        print(f"  ! database {gap['database']} could not be read: {gap['error']}",
              file=sys.stderr)

    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(live, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Snapshot written to {target}")

    if not args.compare:
        if not args.out:
            print("Nothing to do: pass --out to record, or --compare to check.")
        return 0

    before = json.loads(Path(args.compare).read_text(encoding="utf-8"))
    breaking, notes = compare(before, live, write_tables(mapping))
    print(f"\nCompared against {args.compare} (taken {before.get('taken_at', '?')})")
    for line in notes:
        print(f"  [note ] {line}")
    for line in breaking:
        print(f"  [BREAK] {line}")
    if not breaking and not notes:
        print("  nothing CoreYard depends on has changed.")
    elif not breaking:
        print(f"\n{len(notes)} change(s), none of them breaking. "
              f"Run `bin/coreyard doctor` and a `sync --dry-run` to confirm.")
    else:
        print(f"\n{len(breaking)} breaking change(s). Fix the mapping in schema.json "
              f"before the next scheduled run writes anything.")
    return 1 if breaking else 0


if __name__ == "__main__":
    raise SystemExit(main())
