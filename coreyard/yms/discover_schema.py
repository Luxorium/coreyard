"""Map the source database's schema (read-only).

The table and column names belong to your yard system's vendor and must not be guessed. Run this once the
read-only SQL port is open; it enumerates tables, columns and row counts, ranks the tables
most likely to hold parts / pricing / availability / vehicle-fitment by name heuristics,
and dumps a few sample rows from the top candidates. The output (``out/schema/``) is what
you use to fill in the real names in your local ``schema.json``.

    python -m coreyard.yms.discover_schema
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from coreyard.config import load_settings, out_dir
from coreyard.yms.db import connect, query

OUT_DIR = out_dir() / "schema"

# Name fragments that hint at the tables we care about, grouped by concern.
KEYWORDS = {
    "part/inventory": ["part", "inventory", "invtry", "invt", "stock", "item", "lineitem"],
    "pricing": ["price", "pricing", "cost", "amount", "list"],
    "availability": ["status", "avail", "active", "sold", "state", "condition"],
    "vehicle/fitment": ["vehicle", "veh", "unit", "ymm", "year", "make", "model", "donor"],
    "interchange": ["interchange", "hci", "xref"],
}


def _rank(table: str) -> tuple[int, list[str]]:
    """Score a table name by how many concern-keywords it matches."""
    name = table.lower()
    hits: list[str] = []
    for concern, frags in KEYWORDS.items():
        if any(f in name for f in frags):
            hits.append(concern)
    return len(hits), hits


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    return v


def discover() -> dict[str, Any]:
    load_settings()  # validates DB config is present
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}

    with connect() as conn:
        report["server_version"] = query(conn, "SELECT @@VERSION AS v")[0]["v"]

        # Row counts per table (cheap, from partition stats).
        counts = {
            r["name"]: (_int_or_none(r["row_count"]) or 0)
            for r in query(
                conn,
                "SELECT t.name AS name, SUM(p.rows) AS row_count "
                "FROM sys.tables t "
                "JOIN sys.partitions p ON p.object_id=t.object_id AND p.index_id IN (0,1) "
                "GROUP BY t.name",
            )
        }

        # Columns for every table/view.
        cols_rows = query(
            conn,
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, "
            "CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, ORDINAL_POSITION "
            "FROM INFORMATION_SCHEMA.COLUMNS ORDER BY TABLE_NAME, ORDINAL_POSITION",
        )
        tables: dict[str, dict[str, Any]] = {}
        for r in cols_rows:
            t = r["TABLE_NAME"]
            entry = tables.setdefault(
                t,
                {"schema": r["TABLE_SCHEMA"], "rows": counts.get(t), "columns": []},
            )
            length = _int_or_none(r["CHARACTER_MAXIMUM_LENGTH"])
            typ = str(r["DATA_TYPE"]) + (f"({length})" if length and length > 0 else "")
            entry["columns"].append(f"{r['COLUMN_NAME']} {typ}")

        # Rank candidates.
        ranked = []
        for name, info in tables.items():
            score, hits = _rank(name)
            if score:
                ranked.append((score, info.get("rows") or 0, name, hits))
        ranked.sort(key=lambda x: (-x[0], -x[1]))
        report["tables"] = tables
        report["candidates"] = [
            {"table": n, "rows": info_rows, "concerns": hits, "score": s}
            for s, info_rows, n, hits in ranked
        ]

        # Sample the top candidates (read-only, TOP 5).
        samples: dict[str, Any] = {}
        for s, _rows, name, _hits in ranked[:15]:
            schema = tables[name]["schema"]
            try:
                rows = query(conn, f"SELECT TOP 5 * FROM [{schema}].[{name}]")
                samples[name] = [{k: _jsonable(v) for k, v in row.items()} for row in rows]
            except Exception as exc:  # keep going if one table won't sample
                samples[name] = {"error": str(exc)}
        report["samples"] = samples

    return report


def _write(report: dict[str, Any]) -> None:
    (OUT_DIR / "schema.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    lines: list[str] = []
    lines.append(f"SERVER: {report['server_version'].splitlines()[0]}")
    lines.append("")
    lines.append("=== RANKED CANDIDATE TABLES ===")
    for c in report["candidates"]:
        lines.append(f"  [{c['score']}] {c['table']}  (~{c['rows']:,} rows)  -> {', '.join(c['concerns'])}")
    lines.append("")
    lines.append("=== ALL TABLES / COLUMNS ===")
    for name in sorted(report["tables"]):
        info = report["tables"][name]
        rows = info.get("rows")
        rows_s = f"~{rows:,}" if isinstance(rows, int) else "?"
        lines.append(f"\n## {info['schema']}.{name}  ({rows_s} rows)")
        for col in info["columns"]:
            lines.append(f"    {col}")
    (OUT_DIR / "schema_report.txt").write_text("\n".join(lines), encoding="utf-8")


def add_arguments(ap: "argparse.ArgumentParser") -> "argparse.ArgumentParser":
    """No flags: discovery introspects whatever the configured account can see."""
    ap.set_defaults(func=lambda args: run())
    return ap


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard schema",
                                 description=__doc__.splitlines()[0])
    add_arguments(ap)
    ap.parse_args(argv)
    return run()


def run() -> int:
    report = discover()
    _write(report)
    print(f"Server: {report['server_version'].splitlines()[0]}")
    print(f"Tables: {len(report['tables'])}   Candidates: {len(report['candidates'])}")
    print("\nTop candidate tables:")
    for c in report["candidates"][:12]:
        print(f"  [{c['score']}] {c['table']:<32} ~{c['rows']:>8,} rows   {', '.join(c['concerns'])}")
    print(f"\nFull report -> {OUT_DIR/'schema_report.txt'}")
    print(f"Machine JSON -> {OUT_DIR/'schema.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
