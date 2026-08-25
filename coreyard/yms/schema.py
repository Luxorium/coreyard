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
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from coreyard.config import REPO_ROOT, _get, load_env

SCHEMA_PATH = REPO_ROOT / "schema.json"
EXAMPLE_PATH = REPO_ROOT / "schema.example.json"

# Fields the transform layer expects the extract query to alias. Anything absent simply
# comes back as None and the renderer omits it.
REQUIRED_FIELDS = ("r_number", "part_type", "price")

# Synthetic column the delta query adds to carry each changed row's scope verdict.
DELTA_SCOPE_FIELD = "in_scope"


class SchemaError(RuntimeError):
    pass


def sql_timestamp(value: "datetime | str") -> str:
    """Render a delta cursor as a quoted, unambiguous SQL datetime literal.

    Cursors originate in this tool's own state file rather than from a user, but they are
    interpolated into SQL text (the transport has no parameter binding on this path), so the
    value is re-derived from a parsed ``datetime`` instead of being trusted as a string.
    Anything that does not parse is rejected rather than passed through.

    ``YYYY-MM-DDTHH:MM:SS`` is used because SQL Server reads that form the same way under
    every language/dateformat session setting, unlike ``DD/MM/YYYY`` style literals.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise SchemaError(f"not a usable delta cursor: {value!r}") from exc
    if not isinstance(value, datetime):
        raise SchemaError(f"not a usable delta cursor: {value!r}")
    return "'" + value.strftime("%Y-%m-%dT%H:%M:%S") + "'"


def _configured_path() -> Path:
    """Resolve the optional override at runtime, after `.env` is intentionally loaded."""
    load_env()
    return Path(_get("COREYARD_SCHEMA", str(SCHEMA_PATH)) or str(SCHEMA_PATH))


def _boolean(data: dict[str, Any], key: str, default: bool = False) -> bool:
    """Read a JSON boolean without treating the string ``"false"`` as true."""
    value = data.get(key, default)
    if type(value) is not bool:
        raise SchemaError(
            f"schema 'order_write.{key}' must be a JSON boolean (true or false), "
            f"not {value!r}"
        )
    return value


@dataclass(frozen=True)
class OrderDefaults:
    """Which store, yard, customer and employee a storefront order is booked against.

    These mirror whatever the yard system's own e-commerce integration already uses, so
    online orders land in one consistent place instead of a second one CoreYard invented.
    """

    store: int = 1
    yard: int = 1
    customer: str = ""
    account: str = ""
    employee: int = 0


@dataclass(frozen=True)
class OrderWrite:
    """SQL for writing a storefront sale back into the source database.

    Optional, and absent by default: an installation that has not filled this in cannot
    write orders at all, which is the right default for a tool whose whole extract layer is
    otherwise read-only.

    Every fragment references variables the writer declares (``@order_id``, ``@li_rnum``,
    ``@ship_name`` …) and must never interpolate a value itself — the writer is what quotes
    and length-limits buyer-supplied text. See ``schema.example.json`` for the full list.
    """

    defaults: OrderDefaults

    duplicate_check: str            # sets @existing from the order reference
    counter_order_id: str           # each: UPDATE ... OUTPUT INSERTED.<col> INTO @out
    counter_order_number: str
    counter_line_id: str
    counter_audit_id: str
    exists_order_id: str            # each: a SELECT the writer wraps in IF EXISTS (...)
    exists_order_number: str
    exists_line_id: str
    header_insert: str
    line_insert: str
    inventory_read: str             # sets @oldqty for the audit row
    inventory_take: str             # must affect exactly one row
    audit_insert: str

    # Tax. The line-item flag is the decisive one: a non-taxable line computes no tax even
    # if somebody later triggers a recalculation in the application. Leave it false when the
    # storefront already collects and remits sales tax, or the same sale is taxed twice.
    line_items_taxable: bool = False
    recalculate_taxes: bool = False
    apply_customer_tax_rate: bool = False

    _REQUIRED = ("duplicate_check", "counter_order_id", "counter_order_number",
                 "counter_line_id", "counter_audit_id", "exists_order_id",
                 "exists_order_number", "exists_line_id", "header_insert",
                 "line_insert", "inventory_read", "inventory_take", "audit_insert")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrderWrite":
        missing = [k for k in cls._REQUIRED if not (data.get(k) or "").strip()]
        if missing:
            raise SchemaError(
                "schema 'order_write' is incomplete; missing: " + ", ".join(missing) +
                f". See 'order_write' in {EXAMPLE_PATH.name}."
            )
        raw = data.get("defaults") or {}
        defaults = OrderDefaults(
            store=int(raw.get("store", 1)),
            yard=int(raw.get("yard", 1)),
            customer=str(raw.get("customer", "")).strip(),
            account=str(raw.get("account", "")).strip(),
            employee=int(raw.get("employee", 0)),
        )
        if not defaults.customer:
            raise SchemaError(
                "schema 'order_write.defaults.customer' is required: storefront orders have "
                "to be booked against some customer account in the yard system."
            )
        return cls(
            defaults=defaults,
            line_items_taxable=_boolean(data, "line_items_taxable"),
            recalculate_taxes=_boolean(data, "recalculate_taxes"),
            apply_customer_tax_rate=_boolean(data, "apply_customer_tax_rate"),
            **{k: data[k].strip() for k in cls._REQUIRED},
        )


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
    order_write: "OrderWrite | None" = None   # None => this site cannot write orders

    # --- optional delta (incremental catch-up) support ----------------------------------
    # A SQL expression for "when was this row last touched". Supplying it lets a run ask
    # "what changed since <cursor>" instead of re-reading the whole yard, which is what
    # makes a frequent catch-up sync affordable. Absent => only full extracts are possible.
    modified_at: str = ""
    # Optional query returning (r_number, changed_at) for parts whose *photos* moved since
    # ``{since}``. Photo edits usually live in their own table and need not touch the part
    # row, so a part that just gained pictures is invisible to ``modified_at`` alone.
    image_changes: str = ""

    # --- optional order lifecycle feedback ----------------------------------------------
    # A query returning one row per storefront order the source system has progressed, with
    # columns `order_reference` (the storefront order name the sale was booked under),
    # `external_reference` (the source system's own order/invoice number) and, optionally,
    # `status`. Absent => this installation cannot sync order status back to the storefront.
    order_status: str = ""

    # --- optional catalogue enrichment (off unless the renderer is asked for it) --------
    # (part_type_code, alias) pairs: alternate shopper vocabulary for a part type.
    part_type_aliases: str = ""
    # (stock_number, engine, transmission, drivetrain, body, trim, doors) for donor vehicles.
    vehicle_details: str = ""

    @property
    def supports_delta(self) -> bool:
        return bool(self.modified_at)

    @property
    def supports_order_status(self) -> bool:
        return bool(self.order_status)

    def build_order_status_query(self) -> str:
        """The site's own "which storefront orders have progressed" query.

        Returned verbatim. Which tables record an invoice, and how a storefront order is
        linked back to one, belong to the yard system's vendor — so like every other source
        expression they live in the local, gitignored mapping and never in this repository.
        """
        if not self.order_status:
            raise SchemaError(
                "this mapping has no 'order_status' query, so CoreYard cannot tell which "
                "storefront orders the source system has progressed. Add one (see "
                f"{EXAMPLE_PATH.name}) or skip order status sync."
            )
        return self.order_status

    @property
    def supports_images_filter(self) -> bool:
        """Whether this mapping can answer "does this part have photos" in SQL."""
        return bool(self.images_filter)

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
        if DELTA_SCOPE_FIELD in select:
            # The delta query appends this as a computed column; a mapped field of the same
            # name would make the row ambiguous and silently corrupt the in/out-of-scope
            # decision that drives retirement.
            raise SchemaError(
                f"schema 'select' must not map a field named {DELTA_SCOPE_FIELD!r}: "
                f"the delta query computes that column itself."
            )
        write = data.get("order_write") or None
        return cls(
            select=dict(select),
            source=data["source"].strip(),
            scope=data["scope"].strip(),
            order_by=(data.get("order_by") or "").strip(),
            images_filter=(data.get("images_filter") or "").strip(),
            interchange_applications=(data.get("interchange_applications") or "").strip(),
            interchange_makes=(data.get("interchange_makes") or "").strip(),
            modified_at=(data.get("modified_at") or "").strip(),
            order_status=(data.get("order_status") or "").strip(),
            image_changes=(data.get("image_changes") or "").strip(),
            part_type_aliases=(data.get("part_type_aliases") or "").strip(),
            vehicle_details=(data.get("vehicle_details") or "").strip(),
            order_write=OrderWrite.from_dict(write) if write else None,
        )

    def build_lookup_query(self, r_numbers: list[str], with_scope: bool = False,
                           images_only: bool = False) -> str:
        """Select specific parts by R#, deliberately ignoring ``scope``.

        Scope answers "what may be listed". This answers "what is this part", which is a
        different question and has to keep working after the answer to the first one becomes
        no — by the time an order arrives, the part it sold has usually already dropped out
        of scope, and a puller still needs its bin location.

        R#s reach this from Shopify order SKUs, which a draft order can set to arbitrary
        text, so they are validated against a strict character class and emitted quoted.

        ``with_scope`` adds the same ``in_scope`` verdict the delta query carries, for callers
        that looked a part up by identity (a photo-only change, say) and still have to decide
        whether it may be listed. ``images_only`` folds the photo predicate into that verdict,
        so it matches what a full run would decide about the same part.
        """
        seen: set[str] = set()
        keys = []
        for value in r_numbers:
            key = str(value).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key):
                raise SchemaError(f"refusing to look up implausible R# {value!r}")
            if key not in seen:      # an order can list the same part on two lines
                seen.add(key)
                keys.append(f"'{key}'")
        if not keys:
            raise SchemaError("no R#s to look up")
        columns = ",\n    ".join(f"{expr} AS {field}" for field, expr in self.select.items())
        if with_scope:
            verdict = self.scope
            if images_only and self.images_filter:
                verdict = f"({self.scope}) AND ({self.images_filter})"
            columns += (
                f",\n    CASE WHEN ({verdict}) THEN 1 ELSE 0 END AS {DELTA_SCOPE_FIELD}"
            )
        return (f"SELECT\n    {columns}\nFROM {self.source}\n"
                f"WHERE {self.select['r_number']} IN ({', '.join(keys)})")

    def build_query(self, limit: int | None = None, images_only: bool = False) -> str:
        top = f"TOP {int(limit)} " if limit else ""
        columns = ",\n    ".join(f"{expr} AS {field}" for field, expr in self.select.items())
        sql = f"SELECT {top}\n    {columns}\nFROM {self.source}\nWHERE {self.scope}"
        if images_only and self.images_filter:
            sql += f"\n  AND {self.images_filter}"
        if self.order_by:
            sql += f"\nORDER BY {self.order_by}"
        return sql

    def build_identity_page_query(
        self, page_size: int, after: Any = None, images_only: bool = False,
    ) -> str:
        """One page of just the R#s that are listable.

        Reconciliation and audits ask "which parts *may* be listed", not "what are they" —
        and the answer for a whole yard is 26,000 identifiers, not 26,000 fully rendered
        products. Selecting the identity column alone keeps that question affordable enough
        to ask on every run, which is what lets the store be compared against the yard
        rather than against a state file that may have drifted from both.
        """
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        identity = self.select["r_number"]
        sql = (f"SELECT TOP {int(page_size)}\n    {identity} AS r_number\n"
               f"FROM {self.source}\nWHERE ({self.scope})")
        if images_only and self.images_filter:
            sql += f"\n  AND {self.images_filter}"
        if after is not None:
            marker = str(after).replace("'", "''")
            sql += f"\n  AND {identity} > N'{marker}'"
        return sql + f"\nORDER BY {identity}"

    def build_page_query(
        self, page_size: int, after: Any = None, images_only: bool = False,
    ) -> str:
        """Build one bounded keyset page for transports that cannot stream results.

        The SMB/TDS transport's parser holds and repeatedly slices an entire result set.
        Keeping each reply bounded avoids quadratic transient memory while preserving the
        full extract. Keyset paging also avoids making SQL Server rescan and discard every
        preceding page, which is especially costly over a named pipe.
        """
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        columns = ",\n    ".join(f"{expr} AS {field}" for field, expr in self.select.items())
        identity = self.select["r_number"]
        sql = f"SELECT TOP {int(page_size)}\n    {columns}\nFROM {self.source}\nWHERE ({self.scope})"
        if images_only and self.images_filter:
            sql += f"\n  AND {self.images_filter}"
        if after is not None:
            marker = str(after).replace("'", "''")
            sql += f"\n  AND {identity} > N'{marker}'"
        # R# is the stable, unique publishing identity, so it is also the page cursor.
        sql += f"\nORDER BY {identity}"
        return sql

    def build_delta_query(
        self, since: "datetime | str", page_size: int, after: Any = None,
        images_only: bool = False,
    ) -> str:
        """One page of "parts whose row changed since ``since``" — scope-independent.

        Deliberately **not** filtered by ``scope``. A full extract asks "what may be listed",
        and answers by omission: a part that sold simply is not in the result, and the run
        infers retirement from its absence. A delta run cannot reason from absence, because
        almost every part is absent from any short window. So it asks the opposite question —
        "which rows moved" — and carries ``scope`` along as the ``in_scope`` flag, turning a
        sale into positive evidence (a row that is present and out of scope) rather than a
        silence that is indistinguishable from a broken query.
        """
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        if not self.modified_at:
            raise SchemaError(
                "this mapping has no 'modified_at' expression, so it cannot answer "
                "'what changed since ...'. Add one (see schema.example.json) or run a "
                "full sync instead."
            )
        columns = ",\n    ".join(f"{expr} AS {field}" for field, expr in self.select.items())
        identity = self.select["r_number"]
        # The scope verdict has to be the *same* verdict a full run would reach, photo policy
        # included. A delta that called an unphotographed part listable would publish it and
        # the next full sync would archive it, every few minutes, forever.
        verdict = self.scope
        if images_only and self.images_filter:
            verdict = f"({self.scope}) AND ({self.images_filter})"
        sql = (
            f"SELECT TOP {int(page_size)}\n    {columns},\n"
            f"    CASE WHEN ({verdict}) THEN 1 ELSE 0 END AS {DELTA_SCOPE_FIELD}\n"
            f"FROM {self.source}\nWHERE {self.modified_at} > {sql_timestamp(since)}"
        )
        if after is not None:
            marker = str(after).replace("'", "''")
            sql += f"\n  AND {identity} > N'{marker}'"
        sql += f"\nORDER BY {identity}"
        return sql

    def build_image_changes_query(self, since: "datetime | str") -> str:
        """Parts whose photo set changed since ``since``.

        Separate from ``modified_at`` because photographing a part typically writes only to
        the media table; the part row itself never moves, so a newly photographed part is
        invisible to the row-level cursor.
        """
        if not self.image_changes:
            raise SchemaError("this mapping has no 'image_changes' query")
        return self.image_changes.replace("{since}", sql_timestamp(since))


def is_configured(path: Path | None = None) -> bool:
    return (path if path is not None else _configured_path()).exists()


def load(path: Path | None = None) -> SourceSchema:
    """Read the local schema mapping, or explain how to create one."""
    target = path if path is not None else _configured_path()
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
