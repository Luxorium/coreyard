"""Promote a work order the storefront already collected on into an invoice.

This is the second — and only other — module in CoreYard that writes to the yard system,
and it is deliberately separate from :mod:`coreyard.yms.orders`. That one turns a sale into
a *pullable* order; this one closes that order out once the part has actually gone. A site
can book orders through CoreYard and still invoice by hand, which is what happens until
``invoice_write`` appears in ``schema.json``.

The distinction that makes this safe: an invoice here is a **promotion of rows that already
exist**, not a second rendering of the storefront order. Every amount is SELECTed from the
work order inside the transaction, so the two documents cannot disagree about a total, and
the only values interpolated into the batch are ids this module allocated and the carrier's
tracking number. Booking already moved the stock — promoting touches no inventory at all.

    bin/coreyard orders invoice              # plan; writes nothing
    bin/coreyard orders invoice --apply

No table or column name appears here. They belong to the yard system's vendor, so like
:mod:`coreyard.yms.orders` this module owns the transaction shape and the guards; the site
owns the names, in ``invoice_write``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from coreyard.yms import schema as schema_mod
# The same escaping, not a second copy of it: a tracking number is carrier-supplied text
# reaching a db_owner connection with no parameter binding, and a divergent copy of this
# guard is exactly the kind of thing that rots quietly.
from coreyard.yms.orders import _int, _literal

MAX_TRACKING = 50


class InvoiceWriteError(RuntimeError):
    """A promotion was refused or failed. The transaction rolled back; nothing changed."""


@dataclass(frozen=True)
class InvoiceResult:
    invoice_number: Optional[int]
    duplicate: bool = False


# ------------------------------------------------------------------ batch builder ---
def build_batch(order_number: Any, tracking: str,
                write: "schema_mod.InvoiceWrite") -> str:
    """Render the whole promotion as one T-SQL batch.

    Pure and separate from execution, for the same reason the order writer is: ``--apply``
    is off by default and the plan has to show exactly what would run, not an approximation.
    """
    number = _int(order_number, "work order number")
    if number < 1:
        raise InvoiceWriteError(f"implausible work order number {order_number!r}")

    d = write.defaults
    declares = [
        f"DECLARE @order_number int = {number};",
        f"DECLARE @store        int = {_int(d.store, 'store')};",
        f"DECLARE @yard         int = {_int(d.yard, 'yard')};",
        f"DECLARE @employee     int = {_int(d.employee, 'employee')};",
        f"DECLARE @drawer       int = {_int(d.drawer, 'cash drawer')};",
        f"DECLARE @payment_type int = {_int(d.payment_type, 'payment type')};",
        f"DECLARE @tracking nvarchar({MAX_TRACKING}) = "
        f"{_literal(tracking, MAX_TRACKING)};",
        "DECLARE @order_id int, @invoice_id int, @invoice_number int;",
        "DECLARE @li_count int, @li_first int, @li_last int;",
        "DECLARE @existing int;",
        "DECLARE @out TABLE (v int);",
    ]

    def block(fragment: str) -> str:
        lines = fragment.strip().rstrip(";").splitlines()
        return "\n".join("  " + l for l in lines) + ";"

    def allocate(var: str, statement: str) -> str:
        return (f"{block(statement)}\n"
                f"  SELECT {var} = v FROM @out; DELETE FROM @out;\n"
                f"  IF {var} IS NULL BEGIN ROLLBACK TRANSACTION;"
                f" THROW 50011, 'counter allocation returned no value', 1; END")

    def guard(condition: str, code: int, message: str) -> list[str]:
        return [f"  IF {condition}",
                f"  BEGIN ROLLBACK TRANSACTION; THROW {code}, '{message}', 1; END"]

    body: list[str] = [
        "  -- what is still open on this work order; a cancelled line is not invoiced",
        block(write.line_count),
        *guard("@li_count IS NULL OR @li_count < 1", 50012,
               "work order has no open lines to invoice"),
        "",
        "  -- ids first, so a counter clash aborts before anything is written",
        allocate("@invoice_id", write.counter_invoice_id),
        allocate("@invoice_number", write.counter_invoice_number),
        "",
        "  -- never reuse an id the application has already issued",
        f"  IF EXISTS ({write.exists_invoice_id.strip().rstrip(';')})",
        "  BEGIN ROLLBACK TRANSACTION;"
        " THROW 50013, 'allocated invoice id already exists', 1; END",
        f"  IF EXISTS ({write.exists_invoice_number.strip().rstrip(';')})",
        "  BEGIN ROLLBACK TRANSACTION;"
        " THROW 50014, 'allocated invoice number already exists', 1; END",
        "",
        "  -- one block of line ids, so N lines cost one allocation and cannot interleave",
        allocate("@li_last", write.counter_line_block),
        "  SET @li_first = @li_last - @li_count + 1;",
        "",
        "  -- header, then lines, both promoted from the work order's own figures",
        block(write.header_insert),
        *guard("@@ROWCOUNT <> 1", 50015, "invoice header did not write exactly one row"),
        block(write.lines_insert),
        *guard("@@ROWCOUNT <> @li_count", 50016,
               "invoice lines did not match the work order"),
        "",
        "  -- close the order out; this is what stops a second promotion finding it",
        block(write.lines_close),
        *guard("@@ROWCOUNT <> @li_count", 50017,
               "closing the work order lines did not match the invoice"),
        block(write.order_close),
        "",
        "  COMMIT TRANSACTION;",
        "  SELECT @invoice_number AS invoice_number, 0 AS duplicate;",
    ]

    indented = "\n".join(("  " + b if b and not b.startswith("  ") else b) for b in body)
    return f"""SET NOCOUNT ON;
SET XACT_ABORT ON;

{chr(10).join(declares)}

BEGIN TRANSACTION;

-- The work order to promote, held for the life of the transaction.
{write.order_lookup.strip().rstrip(';')};

IF @order_id IS NULL
BEGIN
  ROLLBACK TRANSACTION;
  THROW 50010, 'no open work order with that number to promote', 1;
END

-- Idempotence. A fulfillment can be reported twice and a retry must not raise a second
-- invoice; an already-invoiced work order returns the number it already has.
{write.duplicate_check.strip().rstrip(';')};

IF @existing IS NOT NULL
BEGIN
  ROLLBACK TRANSACTION;
  SELECT @existing AS invoice_number, 1 AS duplicate;
END
ELSE
BEGIN
{indented}
END
"""


# --------------------------------------------------------------------- execution ---
def execute(conn: Any, batch: str) -> InvoiceResult:
    """Run a built batch on an open connection, raising on any server error."""
    from coreyard.yms.orders import _errors

    conn.sql_query(batch)
    problems = _errors(conn)
    if problems:
        raise InvoiceWriteError("; ".join(problems))
    rows = [dict(r) for r in conn.rows]
    if not rows:
        raise InvoiceWriteError(
            "the invoice batch returned no result row; nothing was committed")
    row = rows[-1]
    number = row.get("invoice_number")
    return InvoiceResult(
        invoice_number=int(number) if number not in (None, "NULL") else None,
        duplicate=bool(int(row.get("duplicate") or 0)))


def promote(order_number: Any, tracking: str = "", *,
            dry_run: bool = False) -> InvoiceResult:
    """Promote one work order into an invoice. Returns the resulting invoice number."""
    write = schema_mod.load().invoice_write
    if write is None:
        raise InvoiceWriteError(
            "This installation has no 'invoice_write' section in schema.json, so CoreYard "
            "will not raise invoices. See schema.example.json."
        )
    batch = build_batch(order_number, tracking, write)
    if dry_run:
        print(batch)
        return InvoiceResult(invoice_number=None)

    from coreyard.yms.db import connect

    with connect() as conn:
        return execute(conn, batch)
