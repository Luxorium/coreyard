"""Raise the invoice once the storefront says the part actually went.

The sale reaches the yard system as a work order, and a work order is a promise: somebody
still has to walk out and pull the part. Invoicing is what says that happened. Until now
that was a person retyping the order at the counter hours later, which is fine right up
until the day it is forgotten and a shipped part sits unbilled.

Shipping is the event worth keying on, because it is the one that means the part is off the
donor vehicle and in a box. So this reads the pairing the source system already keeps —
which storefront order became which work order, and whether that work order has been
invoiced — asks the storefront which of the still-booked ones have shipped, and promotes
those. :mod:`coreyard.yms.invoices` does the write.

    bin/coreyard orders invoice              # plan; writes nothing
    bin/coreyard orders invoice --apply

This is the mirror of :mod:`coreyard.orders.lifecycle`, which pushes the *source* system's
progress back onto the storefront. The two run in opposite directions over the same pairing
and cannot chase each other: this one only ever acts on a work order with no invoice, and
that one only ever acts on an order that has one.

**In-store sales are deliberately left alone.** Shopify marks a counter sale fulfilled the
instant the card taps, which for a yard means nothing has been pulled yet — the part is
still on the donor car. Treating that as "shipped" would invoice the sale before anybody
touched it, so those stay manual, invoiced at the counter where the customer is standing.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

from coreyard.orders.lifecycle import SourceOrder, source_orders
from coreyard.yms.orders import is_in_store

# Everything needed to decide, in one call for up to a page of orders. Fulfillments carry
# the tracking number, which is worth having on the invoice: it is what the counter is asked
# for when a customer rings up about a parcel.
_ORDERS_BY_NAME = """query($q:String!){
  orders(first:%d, query:$q){
    nodes{
      name sourceName displayFinancialStatus displayFulfillmentStatus
      fulfillments(first:10){ status trackingInfo{ number } }
    }
  }
}"""

BOOKED = "booked"
PAID = "PAID"
FULFILLED = "FULFILLED"
# Shopify's search is an OR of name terms; keep each request comfortably inside one page.
CHUNK = 20


@dataclass(frozen=True)
class Promotion:
    """One booked order, and whether it has earned an invoice yet."""

    order_name: str
    work_order: str
    tracking: str = ""
    skip: str = ""              # why not; empty when it should be promoted

    @property
    def wanted(self) -> bool:
        return not self.skip


def tracking_of(order: dict) -> str:
    """The first tracking number on a successful fulfillment, or "".

    Orders ship in one parcel here; if that ever stops being true the invoice carries the
    first number rather than a joined list, because the column is one tracking number wide.
    """
    for fulfillment in order.get("fulfillments") or []:
        if str(fulfillment.get("status") or "").upper() != "SUCCESS":
            continue
        for info in fulfillment.get("trackingInfo") or []:
            number = str(info.get("number") or "").strip()
            if number:
                return number
    return ""


def decide(source: SourceOrder, order: Optional[dict]) -> Promotion:
    """Whether this booked work order should be invoiced. Pure: no network, no database."""
    seen = Promotion(order_name=source.order_reference,
                     work_order=source.external_reference)

    def skip(reason: str) -> Promotion:
        return Promotion(seen.order_name, seen.work_order, skip=reason)

    if source.status and source.status != BOOKED:
        return skip(f"already {source.status}")
    if not source.external_reference:
        return skip("no work order number to promote")
    if order is None:
        return skip("no such order on the storefront")
    if is_in_store(order):
        return skip("in-store sale; invoiced at the counter")

    financial = str(order.get("displayFinancialStatus") or "").upper()
    if financial != PAID:
        # A refund is settled by a credit invoice somebody decides on, not by this.
        return skip(f"financial status is {financial or 'unknown'}")

    fulfillment = str(order.get("displayFulfillmentStatus") or "").upper()
    if fulfillment != FULFILLED:
        # Partly-shipped waits: one invoice for the whole work order, once it has all gone.
        return skip(f"{fulfillment.lower().replace('_', ' ') or 'not fulfilled'}")

    return Promotion(seen.order_name, seen.work_order, tracking=tracking_of(order))


def storefront_orders(client, names: list[str]) -> dict[str, dict]:
    """Look the named orders up, a chunk at a time. Keyed by order name."""
    found: dict[str, dict] = {}
    for start in range(0, len(names), CHUNK):
        chunk = names[start:start + CHUNK]
        query = " OR ".join(f"name:{n}" for n in chunk)
        nodes = client.graphql(_ORDERS_BY_NAME % CHUNK, {"q": query})["orders"]["nodes"]
        for node in nodes:
            # Shopify's name search is fuzzy — "#104" also matches "#1042" — so only an
            # exact hit counts, exactly as lifecycle.find_order does.
            if node.get("name") in chunk:
                found[node["name"]] = node
    return found


def plan(client, sources: Optional[list[SourceOrder]] = None) -> list[Promotion]:
    """Work out what would be invoiced. Reads only."""
    rows = [s for s in (sources if sources is not None else source_orders())
            if (s.status or BOOKED) == BOOKED]
    orders = storefront_orders(client, [s.order_reference for s in rows])
    return [decide(s, orders.get(s.order_reference)) for s in rows]


def run(args) -> int:
    from coreyard.sink.shopify_api import ShopifyClient
    from coreyard.yms.invoices import InvoiceWriteError, promote

    client = ShopifyClient()
    decisions = plan(client)
    wanted = [d for d in decisions if d.wanted]

    for decision in decisions:
        if decision.wanted:
            tracking = f" tracking {decision.tracking}" if decision.tracking else ""
            print(f"  {decision.order_name:<8} WO {decision.work_order:<6} "
                  f"-> invoice{tracking}")
        elif args.verbose:
            print(f"  {decision.order_name:<8} WO {decision.work_order:<6} "
                  f"-- {decision.skip}")

    if not wanted:
        print(f"\nNothing to invoice; {len(decisions)} booked order(s) examined.")
        return 0
    if not args.apply:
        print(f"\n{len(wanted)} work order(s) would be invoiced. "
              f"Re-run with --apply to write them.")
        return 0

    done = failed = 0
    for decision in wanted:
        try:
            result = promote(decision.work_order, decision.tracking)
        except InvoiceWriteError as exc:
            print(f"  ! {decision.order_name}: {exc}", file=sys.stderr)
            failed += 1
            continue
        done += 1
        note = " (already invoiced)" if result.duplicate else ""
        print(f"  {decision.order_name}: invoice {result.invoice_number}{note}")

    print(f"\n{done} invoiced, {failed} failed.")
    return 1 if failed else 0
