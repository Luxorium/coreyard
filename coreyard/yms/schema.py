"""Site-supplied mapping of the source database.

The extract layer needs table and column names from the yard management system it reads.
Those names belong to that system's vendor, not to this project, so **none of them live in
this repository**. Each installation supplies its own mapping in a local ``schema.json``
(gitignored, like ``.env``), and ``schema.example.json`` documents the shape with
placeholders.

Run ``python -m coreyard.yms.discover_schema`` against your own database to find the names
to put in it.

The mapping is deliberately expressed as SQL fragments rather than a rigid table/column
model. Real installations differ in ways a fixed model cannot capture — extra joins,
vendor-specific flags, different scope rules — and fragments keep the engine generic
without pretending to understand anyone's schema.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coreyard.config import REPO_ROOT

SCHEMA_PATH = Path(os.environ.get("COREYARD_SCHEMA", REPO_ROOT / "schema.json"))
EXAMPLE_PATH = REPO_ROOT / "schema.example.json"

# Fields the transform layer expects the extract query to alias. Anything absent simply
# comes back as None and the renderer omits it.
REQUIRED_FIELDS = ("r_number", "part_type", "price")


class SchemaError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceSchema:
    """One installation's view of its own database."""

    select: dict[str, str]          # output field -> SQL expression
    source: str                     # FROM ... plus any JOINs
    scope: str                      # WHERE predicate selecting listable parts
    order_by: str
    images_filter: str = ""         # extra predicate for "has photos"
    interchange_applications: str = ""   # {part_type_code} {interchange_code} placeholders
    interchange_makes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceSchema":
        missing = [k for k in ("select", "source", "scope") if not data.get(k)]
        if missing:
            raise SchemaError(f"schema is missing required section(s): {', '.join(missing)}")
        select = data["select"]
        absent = [f for f in REQUIRED_FIELDS if f not in select]
        if absent:
            raise SchemaError(
                f"schema 'select' must map these fields: {', '.join(absent)}. "
                f"See {EXAMPLE_PATH.name}."
            )
        return cls(
            select=dict(select),
            source=data["source"].strip(),
            scope=data["scope"].strip(),
            order_by=(data.get("order_by") or "").strip(),
            images_filter=(data.get("images_filter") or "").strip(),
            interchange_applications=(data.get("interchange_applications") or "").strip(),
            interchange_makes=(data.get("interchange_makes") or "").strip(),
        )

    def build_query(self, limit: int | None = None, images_only: bool = False) -> str:
        top = f"TOP {int(limit)} " if limit else ""
        columns = ",\n    ".join(f"{expr} AS {field}" for field, expr in self.select.items())
        sql = f"SELECT {top}\n    {columns}\nFROM {self.source}\nWHERE {self.scope}"
        if images_only and self.images_filter:
            sql += f"\n  AND {self.images_filter}"
        if self.order_by:
            sql += f"\nORDER BY {self.order_by}"
        return sql


def is_configured(path: Path | None = None) -> bool:
    return (path or SCHEMA_PATH).exists()


def load(path: Path | None = None) -> SourceSchema:
    """Read the local schema mapping, or explain how to create one."""
    target = path or SCHEMA_PATH
    if not target.exists():
        raise SchemaError(
            f"No schema mapping at {target}.\n"
            f"This project ships no vendor schema. Create one from {EXAMPLE_PATH.name}:\n"
            f"    cp {EXAMPLE_PATH.name} {target.name}\n"
            f"then fill in your own table and column names — "
            f"`python -m coreyard.yms.discover_schema` will list them."
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{target} is not valid JSON: {exc}") from exc
    # Keys beginning with "_" are treated as comments, since JSON has none.
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    return SourceSchema.from_dict(data)
