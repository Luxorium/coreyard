"""Write a sales order back into the source database.

This is the **only** module in CoreYard that writes to the yard system, and it exists
because a storefront sale has to become a real order in the system of record or the yard
is running two sets of books. Everything else in ``coreyard.yms`` is SELECT-only and must
stay that way.

Three things make that write safe enough to run unattended:

* **One transaction, ``SET XACT_ABORT ON``.** Any error at any point rolls the whole order
  back. A half-written order — a header with no lines, or a part taken off the shelf with
  no order behind it — is worse than no order at all, because nobody is watching at 6am.
* **Ids are allocated, never guessed.** The source system has no IDENTITY columns; the
  application hands out every id from counter rows, and it is doing so concurrently while
  we run. Each id comes from a single ``UPDATE ... OUTPUT`` under ``UPDLOCK``, never a
  ``SELECT`` then ``UPDATE``, because read-then-write hands out an order number somebody
  else is already using. Every allocated id is then checked to be unused before insert.
* **Idempotence on the order reference.** Shopify delivers webhooks at least once and will
  redeliver on any doubt, so the batch refuses to insert when an order already carries this
  reference and reports the existing number instead. Without that, a retry bills the yard a
  second work order for one sale.

The inventory decrement is guarded on "enough on hand" and must affect exactly one row, so
a part can never go negative and an oversell fails loudly instead of quietly.

No table or column name appears here. They belong to the yard system's vendor, so like the
rest of the extract layer they come from the site's local ``schema.json`` — see
``order_write`` in ``schema.example.json``. This module owns the transaction structure and
the guards; the site owns the names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from coreyard.yms import schema as schema_mod

# Widths are enforced here rather than left to the server because a silent truncation on a
# money or id column is a data error, while an over-long name is merely cosmetic. Anything
# the site's columns declare narrower than this is still truncated server-side.
MAX = {
    "reference": 20, "customer": 12, "account": 9, "note": 255,
    "name": 64, "address": 64, "city": 64, "state": 2, "postal": 10, "country": 2,
    "phone": 20, "email": 100, "description": 20, "interchange": 12,
    "ship_via": 20, "model": 20, "line_note": 798,
}


class OrderWriteError(RuntimeError):
    """A write was refused or failed. The transaction rolled back; nothing changed."""


# ----------------------------------------------------------------- value handling ---
def _text(value: Any, limit: int) -> str:
    """Trim to a column's width, dropping control characters.

    NUL and friends have no business in a name field and some drivers truncate the whole
    string at the first one, which would silently shorten an address instead of failing.
    """
    s = "" if value is None else str(value)
    s = "".join(ch for ch in s if ch == " " or not (ord(ch) < 32 or ord(ch) == 127))
    return s.strip()[:limit]


def _literal(value: Any, limit: int) -> str:
    """A safely quoted SQL string literal.

    Order payloads are attacker-influenced text — a buyer types their own name and address —
    and impacket's TDS interface takes a batch of SQL text with no parameter binding, so
    this doubling is the only thing between a shipping address and arbitrary SQL running as
    ``db_owner``. It is applied to every string that reaches the batch, with no exceptions;
    the templates in ``schema.json`` reference declared variables and never interpolate.
    """
    return "N'" + _text(value, limit).replace("'", "''") + "'"


def _int(value: Any, label: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise OrderWriteError(f"{label} must be a whole number, got {value!r}") from None


def _money(value: Any, label: str) -> str:
    try:
        amount = Decimal(str(value).strip() or "0").quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise OrderWriteError(f"{label} is not a valid amount: {value!r}") from None
    if amount < 0:
        raise OrderWriteError(f"{label} may not be negative: {amount}")
    if amount >= Decimal("1000000"):
        raise OrderWriteError(f"{label} is implausibly large: {amount}")
    return f"{amount:.2f}"


def _r_number(value: Any) -> int:
    """R#s arrive as Shopify SKUs, which a draft order can set to arbitrary text."""
    key = str(value or "").strip()
    if not re.fullmatch(r"[0-9]{1,9}", key):
        raise OrderWriteError(f"refusing to write a line for implausible R# {value!r}")
    return int(key)


# ------------------------------------------------------------------------ models ---
@dataclass(frozen=True)
class OrderLine:
    """One part on the order. ``r_number`` must name a real row in the yard inventory.

    ``year``/``model`` describe the donor vehicle, not the buyer's car: they come from the
    yard's own inventory row and are what the source system records per line.
    """

    r_number: str
    quantity: int = 1
    unit_price: Decimal = Decimal("0")
    description: str = ""
    interchange_number: str = ""
    discount: Decimal = Decimal("0")
    year: Optional[int] = None
    model: str = ""
    note: str = ""              # the storefront's own wording, which the 20-char desc loses

    def subtotal(self) -> Decimal:
        return Decimal(str(self.unit_price)) * self.quantity

    def total(self) -> Decimal:
        return self.subtotal() - Decimal(str(self.discount or 0))


@dataclass(frozen=True)
class SalesOrder:
    """A storefront sale, in the neutral shape the writer understands.

    ``reference`` is the idempotence key and the human cross-reference: it is what lets
    somebody standing in the office match a work order to the order in Shopify.

    Billing and shipping are kept separate because a storefront order genuinely has both and
    they are often different people — the card holder and whoever receives the part.
    """

    reference: str
    lines: list[OrderLine] = field(default_factory=list)
    ship_name: str = ""
    ship_address1: str = ""
    ship_address2: str = ""
    ship_city: str = ""
    ship_state: str = ""
    ship_postal: str = ""
    ship_country: str = ""
    ship_phone: str = ""
    ship_email: str = ""
    bill_name: str = ""
    bill_address1: str = ""
    bill_address2: str = ""
    bill_city: str = ""
    bill_state: str = ""
    bill_postal: str = ""
    bill_country: str = ""
    bill_phone: str = ""
    bill_email: str = ""
    ship_via: str = ""          # the shipping method the buyer chose
    freight: Decimal = Decimal("0")
    note: str = ""

    def parts_total(self) -> Decimal:
        return sum((l.total() for l in self.lines), Decimal("0"))

    def discount_total(self) -> Decimal:
        return sum((Decimal(str(l.discount or 0)) for l in self.lines), Decimal("0"))

    def total(self) -> Decimal:
        """What the buyer paid, excluding tax — the storefront remits that separately."""
        return self.parts_total() + Decimal(str(self.freight or 0))


@dataclass(frozen=True)
class OrderResult:
    order_number: Optional[int]
    duplicate: bool = False
    skipped: tuple[str, ...] = ()


# ------------------------------------------------------------------ batch builder ---
def build_batch(order: SalesOrder, write: "schema_mod.OrderWrite") -> str:
    """Render the whole transaction as one T-SQL batch.

    Kept pure and separate from execution so the dangerous part — the SQL text itself — can
    be asserted against in offline tests, and so ``--dry-run`` shows exactly what would run
    rather than an approximation of it.
    """
    if not order.lines:
        raise OrderWriteError("refusing to create an order with no line items")
    reference = _text(order.reference, MAX["reference"])
    if not reference:
        raise OrderWriteError("an order reference is required; it is the duplicate guard")

    d = write.defaults
    total = _money(order.total(), "order total")
    parts_total = _money(order.parts_total(), "parts total")
    freight = _money(order.freight, "freight")
    discount = _money(order.discount_total(), "discount total")

    declares = [
        f"DECLARE @order_ref  nvarchar({MAX['reference']})  = {_literal(reference, MAX['reference'])};",
        f"DECLARE @customer   nvarchar({MAX['customer']})   = {_literal(d.customer, MAX['customer'])};",
        f"DECLARE @account    nvarchar({MAX['account']})    = {_literal(d.account, MAX['account'])};",
        f"DECLARE @employee   int      = {_int(d.employee, 'employee')};",
        f"DECLARE @store      int      = {_int(d.store, 'store')};",
        f"DECLARE @yard       int      = {_int(d.yard, 'yard')};",
        f"DECLARE @amount     money    = {total};",
        f"DECLARE @parts_total money   = {parts_total};",
        f"DECLARE @freight    money    = {freight};",
        f"DECLARE @discount   money    = {discount};",
        f"DECLARE @taxable    bit      = {1 if write.line_items_taxable else 0};",
        f"DECLARE @recalc_tax bit      = {1 if write.recalculate_taxes else 0};",
        f"DECLARE @apply_rate bit      = {1 if write.apply_customer_tax_rate else 0};",
        f"DECLARE @note       nvarchar({MAX['note']}) = {_literal(order.note, MAX['note'])};",
        f"DECLARE @ship_name  nvarchar({MAX['name']})    = {_literal(order.ship_name, MAX['name'])};",
        f"DECLARE @ship_addr1 nvarchar({MAX['address']}) = {_literal(order.ship_address1, MAX['address'])};",
        f"DECLARE @ship_addr2 nvarchar({MAX['address']}) = {_literal(order.ship_address2, MAX['address'])};",
        f"DECLARE @ship_city  nvarchar({MAX['city']})    = {_literal(order.ship_city, MAX['city'])};",
        f"DECLARE @ship_state nvarchar({MAX['state']})   = {_literal(order.ship_state, MAX['state'])};",
        f"DECLARE @ship_zip   nvarchar({MAX['postal']})  = {_literal(order.ship_postal, MAX['postal'])};",
        f"DECLARE @ship_ctry  nvarchar({MAX['country']}) = {_literal(order.ship_country, MAX['country'])};",
        f"DECLARE @ship_phone nvarchar({MAX['phone']})   = {_literal(order.ship_phone, MAX['phone'])};",
        f"DECLARE @ship_email nvarchar({MAX['email']})   = {_literal(order.ship_email, MAX['email'])};",
        f"DECLARE @ship_via  nvarchar({MAX['ship_via']}) = {_literal(order.ship_via, MAX['ship_via'])};",
        f"DECLARE @bill_name  nvarchar({MAX['name']})    = {_literal(order.bill_name, MAX['name'])};",
        f"DECLARE @bill_addr1 nvarchar({MAX['address']}) = {_literal(order.bill_address1, MAX['address'])};",
        f"DECLARE @bill_addr2 nvarchar({MAX['address']}) = {_literal(order.bill_address2, MAX['address'])};",
        f"DECLARE @bill_city  nvarchar({MAX['city']})    = {_literal(order.bill_city, MAX['city'])};",
        f"DECLARE @bill_state nvarchar({MAX['state']})   = {_literal(order.bill_state, MAX['state'])};",
        f"DECLARE @bill_zip   nvarchar({MAX['postal']})  = {_literal(order.bill_postal, MAX['postal'])};",
        f"DECLARE @bill_ctry  nvarchar({MAX['country']}) = {_literal(order.bill_country, MAX['country'])};",
        f"DECLARE @bill_phone nvarchar({MAX['phone']})   = {_literal(order.bill_phone, MAX['phone'])};",
        f"DECLARE @bill_email nvarchar({MAX['email']})   = {_literal(order.bill_email, MAX['email'])};",
        "DECLARE @order_id int, @order_number int, @li_id int, @audit_id int;",
        f"DECLARE @li_rnum int, @li_qty int, @li_price money, @oldqty int,"
        f" @li_desc nvarchar({MAX['description']}), @li_intch nvarchar({MAX['interchange']}),"
        f" @li_sub money, @li_total money, @li_year int,"
        f" @li_model nvarchar({MAX['model']}), @li_notes nvarchar({MAX['line_note']});",
        "DECLARE @existing int;",
        "DECLARE @out TABLE (v int);",
    ]

    def block(fragment: str) -> str:
        """One site-supplied statement, terminated and indented as a unit.

        Every line is indented, not just the first: a dry-run is meant to be read by
        somebody deciding whether to let this touch their database.
        """
        lines = fragment.strip().rstrip(";").splitlines()
        return "\n".join("  " + l for l in lines) + ";"

    def allocate(var: str, statement: str) -> str:
        # OUTPUT ... INTO a table variable, then read it: the allocation and the increment
        # are one statement, so a concurrent allocator cannot be handed the same value.
        return (f"{block(statement)}\n"
                f"  SELECT {var} = v FROM @out; DELETE FROM @out;\n"
                f"  IF {var} IS NULL BEGIN ROLLBACK TRANSACTION;"
                f" THROW 50001, 'counter allocation returned no value', 1; END")

    body: list[str] = [
        "  -- ids first, so a counter clash aborts before anything is written",
        allocate("@order_id", write.counter_order_id),
        allocate("@order_number", write.counter_order_number),
        "",
        "  -- never reuse an id the application has already issued",
        f"  IF EXISTS ({write.exists_order_id.strip().rstrip(';')})",
        "  BEGIN ROLLBACK TRANSACTION;"
        " THROW 50002, 'allocated order id already exists', 1; END",
        f"  IF EXISTS ({write.exists_order_number.strip().rstrip(';')})",
        "  BEGIN ROLLBACK TRANSACTION;"
        " THROW 50003, 'allocated order number already exists', 1; END",
        "",
        "  -- header",
        block(write.header_insert),
    ]

    for index, line in enumerate(order.lines, start=1):
        rnum = _r_number(line.r_number)
        qty = _int(line.quantity, f"line {index} quantity")
        if qty < 1:
            raise OrderWriteError(f"line {index} has a quantity of {qty}")
        price = _money(line.unit_price, f"line {index} price")
        if line.total() < 0:
            raise OrderWriteError(
                f"line {index} discounts more than it costs "
                f"({line.discount} off {line.subtotal()})")
        body += [
            "",
            f"  -- line {index}: R#{rnum}",
            f"  SET @li_rnum = {rnum}; SET @li_qty = {qty}; SET @li_price = {price};",
            f"  SET @li_sub = {_money(line.subtotal(), f'line {index} subtotal')};"
            f" SET @li_total = {_money(line.total(), f'line {index} total')};",
            f"  SET @li_year = {int(line.year) if line.year else 0};",
            f"  SET @li_model = {_literal(line.model, MAX['model'])};",
            f"  SET @li_notes = {_literal(line.note, MAX['line_note'])};",
            f"  SET @li_desc = {_literal(line.description, MAX['description'])};",
            f"  SET @li_intch = {_literal(line.interchange_number, MAX['interchange'])};",
            allocate("@li_id", write.counter_line_id),
            f"  IF EXISTS ({write.exists_line_id.strip().rstrip(';')})",
            "  BEGIN ROLLBACK TRANSACTION;"
            " THROW 50004, 'allocated line id already exists', 1; END",
            block(write.line_insert),
            "",
            "  -- take it off the shelf; this, not the order, is what delists the part",
            block(write.inventory_read),
            block(write.inventory_take),
            "  IF @@ROWCOUNT <> 1",
            "  BEGIN ROLLBACK TRANSACTION;"
            f" THROW 50005, 'R#{rnum} is not available in the quantity ordered', 1; END",
            allocate("@audit_id", write.counter_audit_id),
            block(write.audit_insert),
        ]

    body.append("")
    body.append("  COMMIT TRANSACTION;")
    body.append("  SELECT @order_number AS order_number, 0 AS duplicate;")

    indented = "\n".join(("  " + b if b and not b.startswith("  ") else b) for b in body)
    return f"""SET NOCOUNT ON;
SET XACT_ABORT ON;

{chr(10).join(declares)}

BEGIN TRANSACTION;

-- Idempotence. Shopify redelivers on any doubt and a retry must not buy the part twice.
-- Held for the life of the transaction so two concurrent deliveries serialise here.
{write.duplicate_check.strip().rstrip(';')};

IF @existing IS NOT NULL
BEGIN
  ROLLBACK TRANSACTION;
  SELECT @existing AS order_number, 1 AS duplicate;
END
ELSE
BEGIN
{indented}
END
"""


# --------------------------------------------------------------------- execution ---
def _errors(conn: Any) -> list[str]:
    """Collect TDS error tokens.

    ``db.query`` cannot be reused for a write: impacket collects server errors as reply
    tokens rather than raising, so a statement that failed looks exactly like one that
    returned no rows. Tolerable for a SELECT, disqualifying for a transaction — the batch
    would appear to succeed while the transaction had already been aborted underneath it.
    """
    from impacket.tds import TDS_ERROR_TOKEN

    found = []
    for key in conn.replies.keys():
        for reply in conn.replies[key]:
            if reply["TokenType"] == TDS_ERROR_TOKEN:
                found.append(f"Msg {reply['Number']}, Line {reply['LineNumber']}: "
                             f"{reply['MsgText'].decode('utf-16le')}")
    return found


def execute(conn: Any, batch: str) -> OrderResult:
    """Run a built batch on an open connection, raising on any server error."""
    conn.sql_query(batch)
    problems = _errors(conn)
    if problems:
        raise OrderWriteError("; ".join(problems))
    rows = [dict(r) for r in conn.rows]
    if not rows:
        raise OrderWriteError("the order batch returned no result row; nothing was committed")
    row = rows[-1]
    number = row.get("order_number")
    return OrderResult(order_number=int(number) if number not in (None, "NULL") else None,
                       duplicate=bool(int(row.get("duplicate") or 0)))


def create(order: SalesOrder, *, dry_run: bool = False) -> OrderResult:
    """Create one order in the source database. Returns the resulting order number."""
    write = schema_mod.load().order_write
    if write is None:
        raise OrderWriteError(
            "This installation has no 'order_write' section in schema.json, so CoreYard "
            "will not write orders. See schema.example.json."
        )
    batch = build_batch(order, write)
    if dry_run:
        print(batch)
        return OrderResult(order_number=None)

    from coreyard.yms.db import connect

    with connect() as conn:
        return execute(conn, batch)


# ------------------------------------------------------- Shopify payload -> order ---

# Shopify's ``source_name`` for a sale rung up in person rather than on the storefront: the
# POS app, and the "quick sale" tile that a phone-as-card-terminal tap comes through as. The
# older POS app reported the device it ran on instead, which is why those two are here too.
IN_STORE_SOURCES = frozenset({"pos", "quick_sale", "iphone", "android"})

# What to call a walk-in on the work order. A counter sale carries no shipping address and
# usually no customer record, so :func:`from_shopify`'s ``who`` has nothing to return and the
# name column lands empty — which reads at the counter as a work order belonging to nobody
# rather than as a sale already paid for, waiting on somebody to go pull the part.
IN_STORE_CUSTOMER = "In-Store Shopify Customer"


def is_in_store(payload: dict) -> bool:
    """True when the sale was rung up in person rather than on the storefront."""
    return str(payload.get("source_name") or "").strip().lower() in IN_STORE_SOURCES


def from_shopify(payload: dict, parts: dict) -> tuple[SalesOrder, list[str]]:
    """Map a Shopify order webhook payload onto a :class:`SalesOrder`.

    Returns the order plus the SKUs that were skipped. A storefront order can carry lines
    that are not yard parts at all — a shipping charge, a hand-added product, a SKU typo —
    and those must not stop the rest of the order reaching the yard system. A line is only
    written when its SKU resolved to a real inventory row.
    """
    ship = payload.get("shipping_address") or payload.get("billing_address") or {}
    bill = payload.get("billing_address") or payload.get("shipping_address") or {}
    customer = payload.get("customer") or {}
    email = payload.get("email") or customer.get("email") or ""
    phone = payload.get("phone") or customer.get("phone") or ""

    in_store = is_in_store(payload)

    def who(addr: dict) -> str:
        name = " ".join(v for v in (addr.get("first_name") or customer.get("first_name"),
                                    addr.get("last_name") or customer.get("last_name")) if v)
        # A named walk-in keeps their name; the label is only for the usual counter sale
        # that has no name to keep.
        return name.strip() or email or (IN_STORE_CUSTOMER if in_store else "")

    lines: list[OrderLine] = []
    skipped: list[str] = []
    for item in payload.get("line_items") or []:
        sku = str(item.get("sku") or "").strip()
        part = parts.get(sku)
        if not sku or part is None:
            skipped.append(sku or (item.get("title") or "?"))
            continue
        title = item.get("name") or item.get("title") or ""
        lines.append(OrderLine(
            r_number=sku,
            quantity=int(item.get("quantity") or 1),
            unit_price=Decimal(str(item.get("price") or "0")),
            # The yard's own part-type wording, not the Shopify title: this column is 20
            # characters and the storefront title is a sentence. The full title is kept on
            # the line's note field, where there is room for it.
            description=part.part_type or title,
            interchange_number=part.interchange_number or "",
            discount=Decimal(str(item.get("total_discount") or "0")),
            year=part.year,
            model=part.model or "",
            note=title,
        ))

    shipping_lines = payload.get("shipping_lines") or []
    freight = sum((Decimal(str(s.get("price") or "0")) for s in shipping_lines), Decimal("0"))
    reference = payload.get("name") or f"#{payload.get('order_number', '')}"
    # The buyer's own note is the part somebody actually needs to read ("leave at side door"),
    # so it goes first and the order reference follows it.
    buyer_note = (payload.get("note") or "").strip()
    origin = f"Shopify {reference}" + (" in-store" if in_store else "")
    order = SalesOrder(
        reference=reference,
        lines=lines,
        ship_name=who(ship),
        ship_address1=ship.get("address1") or "",
        ship_address2=ship.get("address2") or "",
        ship_city=ship.get("city") or "",
        ship_state=ship.get("province_code") or ship.get("province") or "",
        ship_postal=ship.get("zip") or "",
        ship_country=ship.get("country_code") or ship.get("country") or "",
        ship_phone=ship.get("phone") or phone,
        ship_email=email,
        bill_name=who(bill),
        bill_address1=bill.get("address1") or "",
        bill_address2=bill.get("address2") or "",
        bill_city=bill.get("city") or "",
        bill_state=bill.get("province_code") or bill.get("province") or "",
        bill_postal=bill.get("zip") or "",
        bill_country=bill.get("country_code") or bill.get("country") or "",
        bill_phone=bill.get("phone") or phone,
        bill_email=email,
        ship_via=(shipping_lines[0].get("title") or "") if shipping_lines else "",
        freight=freight,
        note=f"{buyer_note} [{origin}]" if buyer_note else origin,
    )
    return order, skipped


if __name__ == "__main__":       # pragma: no cover - manual inspection aid
    import argparse
    import json
    from pathlib import Path

    ap = argparse.ArgumentParser(
        prog="coreyard.yms.orders",
        description="Show the SQL CoreYard would run for a saved Shopify order payload.")
    ap.add_argument("file", help="an order JSON payload (see out/test_order.json)")
    ap.add_argument("--execute", action="store_true",
                    help="actually create the order (default: print the SQL only)")
    args = ap.parse_args()

    from coreyard.yms.inventory import fetch_parts_by_r_number

    body = json.loads(Path(args.file).read_text(encoding="utf-8"))
    skus = [str(i.get("sku") or "").strip()
            for i in (body.get("line_items") or []) if i.get("sku")]
    sales_order, ignored = from_shopify(body, fetch_parts_by_r_number(skus) if skus else {})
    if ignored:
        print(f"-- skipping non-part line(s): {', '.join(ignored)}")
    outcome = create(sales_order, dry_run=not args.execute)
    if args.execute:
        print(f"order {outcome.order_number}"
              f"{' (already existed)' if outcome.duplicate else ' created'}")
