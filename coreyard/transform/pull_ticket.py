"""Render a Shopify order as a printable pull ticket.

A worker fulfilling an online order needs two things on one page: where the part is in the
yard, and where it is going. Those live in different systems — bin location and donor vehicle
come from the yard database, buyer and shipping address come from Shopify — so neither system
can print this sheet on its own. This module is the join.

Output is plain HTML sized for US Letter. No PDF library, no template engine: a browser or
``lp`` renders it, and the markup stays readable enough to adjust without learning anything.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from coreyard.config import StoreProfile
from coreyard.models import Part


@dataclass
class TicketLine:
    """One ordered part: what Shopify sold, plus what the yard knows about it."""

    sku: str
    title: str
    quantity: int = 1
    price: str = ""
    part: Optional[Part] = None      # None when the SKU matched no row in the yard database

    def location(self) -> str:
        return (self.part.location if self.part and self.part.location else "").strip()


@dataclass
class Ticket:
    order_name: str                  # "#1042"
    created_at: str = ""
    buyer: str = ""
    email: str = ""
    phone: str = ""
    shipping_address: list[str] = field(default_factory=list)
    shipping_method: str = ""
    note: str = ""
    lines: list[TicketLine] = field(default_factory=list)
    financial_status: str = ""


def _e(value) -> str:
    return html.escape(str(value or "")).strip()


def _rows(ticket: Ticket) -> str:
    out = []
    for line in ticket.lines:
        part = line.part
        # The location column is the single most useful thing on the page, so an unresolved
        # SKU says so loudly rather than rendering an empty cell that reads as "no bin".
        location = _e(line.location()) or '<span class="missing">NOT FOUND</span>'
        details = []
        if part:
            if part.fitment_label():
                details.append(_e(part.fitment_label()))
            if part.stock_number:
                details.append(f"Stock #{_e(part.stock_number)}")
            if part.interchange_number:
                details.append(f"Interchange #{_e(part.interchange_number)}")
            if part.side:
                details.append(_e(part.side))
            if part.grade:
                details.append(f"Grade {_e(part.grade)}")
        out.append(
            f"""      <tr>
        <td class="loc">{location}</td>
        <td class="sku">{_e(line.sku)}</td>
        <td>
          <div class="title">{_e(line.title)}</div>
          <div class="details">{' &middot; '.join(details)}</div>
        </td>
        <td class="qty">{line.quantity}</td>
        <td class="price">{_e(line.price)}</td>
        <td class="check"></td>
      </tr>"""
        )
    return "\n".join(out)


def render(ticket: Ticket, store: Optional[StoreProfile] = None) -> str:
    """Return the ticket as a standalone printable HTML document."""
    store = store or StoreProfile()
    address = "<br>".join(_e(part) for part in ticket.shipping_address if part)
    contact = " &middot; ".join(_e(v) for v in (ticket.email, ticket.phone) if v)
    printed = datetime.now().strftime("%Y-%m-%d %H:%M")
    heading = _e(store.origin() or store.vendor) or "Pull Ticket"
    note = (f'<div class="note"><strong>Order note:</strong> {_e(ticket.note)}</div>'
            if ticket.note else "")
    unresolved = sum(1 for line in ticket.lines if line.part is None)
    warning = (f'<div class="warn">{unresolved} line(s) had no matching part in the yard '
               f'database — check these by hand before pulling.</div>' if unresolved else "")

    return f"""<!doctype html>
<meta charset="utf-8">
<title>Pull ticket {_e(ticket.order_name)}</title>
<style>
  @page {{ size: letter; margin: 0.5in; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 11pt/1.4 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: #000; margin: 0; }}
  header {{ display: flex; justify-content: space-between; align-items: flex-start;
            border-bottom: 3px solid #000; padding-bottom: 8px; margin-bottom: 14px; }}
  .yard {{ font-size: 13pt; font-weight: 700; }}
  .order {{ font-size: 22pt; font-weight: 700; text-align: right; line-height: 1; }}
  .sub {{ font-size: 9pt; color: #444; text-align: right; }}
  .panels {{ display: flex; gap: 18px; margin-bottom: 14px; }}
  .panel {{ flex: 1; border: 1px solid #999; padding: 8px 10px; }}
  .panel h2 {{ font-size: 8pt; letter-spacing: .09em; text-transform: uppercase;
               color: #444; margin: 0 0 5px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ font-size: 8pt; letter-spacing: .09em; text-transform: uppercase; text-align: left;
        border-bottom: 2px solid #000; padding: 5px 6px; }}
  td {{ border-bottom: 1px solid #ccc; padding: 8px 6px; vertical-align: top; }}
  .loc {{ font-size: 15pt; font-weight: 700; font-family: ui-monospace, Menlo, monospace;
          white-space: nowrap; }}
  .sku {{ font-family: ui-monospace, Menlo, monospace; font-weight: 700; white-space: nowrap; }}
  .title {{ font-weight: 600; }}
  .details {{ font-size: 8.5pt; color: #555; margin-top: 2px; }}
  .qty, .price {{ text-align: right; white-space: nowrap; }}
  .check {{ width: 34px; }}
  .check::after {{ content: ""; display: block; width: 20px; height: 20px;
                   border: 2px solid #000; margin: 0 auto; }}
  .missing {{ color: #a00; font-weight: 700; }}
  .warn {{ border: 2px solid #a00; color: #a00; padding: 7px 10px; margin: 12px 0;
           font-weight: 600; }}
  .note {{ border-left: 4px solid #000; padding: 6px 10px; margin: 12px 0; background: #f4f4f4; }}
  footer {{ margin-top: 20px; padding-top: 8px; border-top: 1px solid #999;
            font-size: 8.5pt; color: #555; display: flex; justify-content: space-between; }}
  /* Ink on paper: the browser drops backgrounds by default, and the sign-off line and
     checkboxes are the parts a worker actually writes on. */
  @media print {{ .note {{ background: none; }} }}
</style>
<header>
  <div>
    <div class="yard">{heading}</div>
    <div class="sub" style="text-align:left">Online order &middot; pull ticket</div>
  </div>
  <div>
    <div class="order">{_e(ticket.order_name)}</div>
    <div class="sub">{_e(ticket.created_at)}</div>
    <div class="sub">{_e(ticket.financial_status)}</div>
  </div>
</header>

<div class="panels">
  <div class="panel">
    <h2>Ship to</h2>
    <div><strong>{_e(ticket.buyer)}</strong></div>
    <div>{address or "&mdash;"}</div>
    <div class="details">{contact}</div>
  </div>
  <div class="panel">
    <h2>Shipping method</h2>
    <div>{_e(ticket.shipping_method) or "&mdash;"}</div>
    <h2 style="margin-top:10px">Pulled by</h2>
    <div style="border-bottom:1px solid #000; height:20px"></div>
  </div>
</div>

{warning}{note}

<table>
  <thead>
    <tr><th>Location</th><th>R#</th><th>Part</th><th class="qty">Qty</th>
        <th class="price">Price</th><th></th></tr>
  </thead>
  <tbody>
{_rows(ticket)}
  </tbody>
</table>

<footer>
  <span>Printed {printed}</span>
  <span>{len(ticket.lines)} line(s)</span>
</footer>
"""


def from_order(payload: dict, parts: dict[str, Part],
               store: Optional[StoreProfile] = None) -> Ticket:
    """Build a Ticket from a Shopify order webhook payload plus looked-up parts."""
    ship = payload.get("shipping_address") or payload.get("billing_address") or {}
    customer = payload.get("customer") or {}
    buyer = " ".join(
        v for v in (ship.get("first_name") or customer.get("first_name"),
                    ship.get("last_name") or customer.get("last_name")) if v
    ).strip() or (payload.get("email") or "")

    address = [
        ship.get("company"),
        ship.get("address1"),
        ship.get("address2"),
        " ".join(v for v in (ship.get("city"), ship.get("province_code") or ship.get("province"),
                             ship.get("zip")) if v).strip(),
        ship.get("country") if (ship.get("country_code") or "US") != "US" else None,
    ]

    lines = []
    for item in payload.get("line_items") or []:
        sku = str(item.get("sku") or "").strip()
        lines.append(TicketLine(
            sku=sku,
            title=item.get("title") or item.get("name") or "",
            quantity=int(item.get("quantity") or 1),
            price=item.get("price") or "",
            part=parts.get(sku),
        ))

    shipping_lines = payload.get("shipping_lines") or []
    return Ticket(
        order_name=payload.get("name") or f"#{payload.get('order_number', '')}",
        created_at=(payload.get("created_at") or "")[:16].replace("T", " "),
        buyer=buyer,
        email=payload.get("email") or customer.get("email") or "",
        phone=ship.get("phone") or payload.get("phone") or customer.get("phone") or "",
        shipping_address=[a for a in address if a],
        shipping_method=(shipping_lines[0].get("title") if shipping_lines else ""),
        note=payload.get("note") or "",
        financial_status=(payload.get("financial_status") or "").replace("_", " "),
        lines=lines,
    )
