"""Push the source system's order progress back onto the Shopify order.

The sale reaches the yard system as a work order. What happens next — picked, invoiced,
shipped — happens there, and the storefront never hears about it, so a buyer looking at
their account sees "unfulfilled" on an order that left the building yesterday.

This closes the loop in the direction that is safe to automate: it reads the source
system's own record of which storefront orders have progressed, finds the matching Shopify
order, and marks it. Three things happen, in increasing order of consequence:

1. **Tags.** Cheap, reversible, and what an operator filters on.
2. **A note** carrying the source reference, so the two systems can be reconciled by hand.
3. **Fulfillment**, and only when the site's policy says nothing else will ever close the
   order out. See :mod:`coreyard.orders.policy` — creating a fulfillment closes the
   fulfillment order, and doing that under a shipping app that has not bought the label yet
   costs the buyer their tracking number.

The link between the two systems is whatever reference the source order carries. CoreYard's
own order booking writes the storefront order name there, so an installation that books
through CoreYard has the link already; one that books some other way supplies a query that
produces the same pairing. Either way the SQL is the site's, in the local schema mapping.

Each row carries a ``status``. ``booked`` means the work order exists but has not been
invoiced: only the reference tag (``wo-<n>``) is written, so an operator can find the
order, and the rest waits. Anything else — ``invoiced``, or the empty string a
single-state query returns — gets the full treatment above. A query that reports only
invoiced orders therefore behaves exactly as before.

    bin/coreyard orders sync-status              # plan; writes nothing
    bin/coreyard orders sync-status --apply

Read-only against the source database. Needs ``write_orders`` on the Shopify app to apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from coreyard.orders.policy import OrderPolicy, resolve

_ORDER_BY_NAME = """query($q:String!){
  orders(first:5, query:$q){
    nodes{
      id name tags note displayFulfillmentStatus
      lineItems(first:100){ nodes{ id product{ tags } } }
      fulfillmentOrders(first:10){ nodes{ id status } }
    }
  }
}"""

_TAGS_ADD = """mutation($id:ID!,$tags:[String!]!){
  tagsAdd(id:$id, tags:$tags){ userErrors{ field message } } }"""

_ORDER_UPDATE = """mutation($input:OrderInput!){
  orderUpdate(input:$input){ order{ id } userErrors{ field message } } }"""

_FULFILL = """mutation($fulfillment:FulfillmentInput!){
  fulfillmentCreate(fulfillment:$fulfillment){
    fulfillment{ id status } userErrors{ field message } } }"""


@dataclass(frozen=True)
class SourceOrder:
    """One row of the site's order-status query."""

    order_reference: str            # the storefront order name, e.g. "#1042"
    external_reference: str = ""    # the source system's own order/invoice number
    status: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "SourceOrder":
        def text(key: str) -> str:
            value = row.get(key)
            return "" if value is None else str(value).strip()

        return cls(order_reference=text("order_reference"),
                   external_reference=text("external_reference"),
                   status=text("status"))


@dataclass
class Decision:
    """What one order needs, worked out before anything is written."""

    order_id: str = ""
    order_name: str = ""
    add_tags: list[str] = field(default_factory=list)
    note: str = ""                             # the full note to write, or "" for none
    fulfill: list[str] = field(default_factory=list)   # open fulfillment order ids
    reason: str = ""                           # why fulfillment was or was not chosen

    @property
    def empty(self) -> bool:
        return not (self.add_tags or self.note or self.fulfill)


def decide(order: dict, source: SourceOrder,
           policy: Optional[OrderPolicy] = None) -> Decision:
    """Work out the change one order needs. Pure: no network, no database."""
    policy = policy or OrderPolicy()
    reference = source.external_reference or source.order_reference
    decision = Decision(order_id=order.get("id", ""), order_name=order.get("name", ""))
    existing = {str(t) for t in (order.get("tags") or [])}

    # A work order that exists but has not been invoiced yet. CoreYard books it the moment
    # the sale is paid and knows its number then, so the reference tag can go on now — an
    # operator filters on it — but the ``invoiced`` tag, the note and any fulfillment all
    # name an invoice that has not happened, so they wait. Any other status (including the
    # empty one a single-state query returns) keeps the original behaviour.
    if source.status.strip().lower() == "booked":
        decision.add_tags = [t for t in policy.reference_tag_for(reference)
                             if t not in existing]
        decision.reason = "work order booked, not yet invoiced"
        return decision

    decision.add_tags = [t for t in policy.tags_for(reference) if t not in existing]

    marker = policy.note_for(reference)
    current_note = order.get("note") or ""
    if marker and marker not in current_note:
        # Append rather than replace: the buyer's own checkout note ("leave at the side
        # door") is the part somebody actually has to read.
        decision.note = (current_note + "\n" if current_note else "") + marker

    fulfillment = policy.fulfillment
    if not fulfillment.enabled:
        decision.reason = "fulfillment not configured"
        return decision
    if str(order.get("displayFulfillmentStatus") or "").upper() == "FULFILLED":
        decision.reason = "already fulfilled"
        return decision

    lines = (order.get("lineItems") or {}).get("nodes") or []
    groups = [fulfillment.group_of(((line.get("product") or {}).get("tags") or []))
              for line in lines]
    if fulfillment.defers(groups):
        decision.reason = "a line ships through another system, which owns the close-out"
        return decision
    open_orders = [f["id"] for f in ((order.get("fulfillmentOrders") or {}).get("nodes") or [])
                   if str(f.get("status") or "").upper() == "OPEN"]
    if not open_orders:
        decision.reason = "no open fulfillment order"
        return decision
    decision.fulfill = open_orders
    decision.reason = "no line is shipped by another system, so nothing else would close it"
    return decision


def source_orders(mapping=None) -> list[SourceOrder]:
    """Read the site's order-status query. ``SELECT`` only."""
    from coreyard.yms import schema as schema_mod
    from coreyard.yms.db import connect, query

    mapping = mapping or schema_mod.load()
    sql = mapping.build_order_status_query()
    with connect() as conn:
        rows = query(conn, sql)
    out = []
    for row in rows:
        record = SourceOrder.from_row(dict(row) if not isinstance(row, dict) else row)
        if record.order_reference:
            out.append(record)
    return out


def find_order(client, name: str) -> Optional[dict]:
    """The Shopify order with exactly this name, or None.

    Shopify's order search is fuzzy — "#104" also returns "#1042" — so the exact name is
    re-checked rather than trusting the first hit.
    """
    nodes = client.graphql(_ORDER_BY_NAME, {"q": f"name:{name}"})["orders"]["nodes"]
    return next((o for o in nodes if o.get("name") == name), None)


def apply(client, decision: Decision, notify_customer: bool = False) -> list[str]:
    """Write one decision. Returns the steps that landed; raises on the first refusal."""
    done: list[str] = []
    if decision.add_tags:
        client.mutate(_TAGS_ADD, {"id": decision.order_id, "tags": decision.add_tags},
                      "tagsAdd")
        done.append("tags")
    if decision.note:
        client.mutate(_ORDER_UPDATE,
                      {"input": {"id": decision.order_id, "note": decision.note}},
                      "orderUpdate")
        done.append("note")
    if decision.fulfill:
        client.mutate(_FULFILL, {"fulfillment": {
            "notifyCustomer": notify_customer,
            "lineItemsByFulfillmentOrder": [{"fulfillmentOrderId": f}
                                            for f in decision.fulfill],
        }}, "fulfillmentCreate")
        done.append("fulfillment")
    return done


def run(args) -> int:
    import sys

    from coreyard.sink.shopify_api import ShopifyClient
    from coreyard.yms import schema as schema_mod
    from coreyard.yms.inventory import is_configured

    if not is_configured():
        print("No schema mapping yet — see README 'Map your database'.", file=sys.stderr)
        return 2
    mapping = schema_mod.load()
    if not mapping.supports_order_status:
        print("This mapping has no 'order_status' query, so there is nothing to read.\n"
              "Add one (see schema.example.json) — the table names are yours, not "
              "CoreYard's.", file=sys.stderr)
        return 2

    policy = resolve()
    client = ShopifyClient()
    rows = source_orders(mapping)
    print(f"{len(rows)} storefront order(s) have progressed in the source system.")
    if not rows:
        return 0

    changed = missing = failed = 0
    for source in rows:
        order = find_order(client, source.order_reference)
        if order is None:
            missing += 1
            print(f"  ! {source.order_reference}: no such Shopify order "
                  f"(source reference {source.external_reference or '-'})")
            continue
        decision = decide(order, source, policy)
        if decision.empty:
            continue
        changed += 1
        mark = "+" if args.apply else "~"
        print(f"  {mark} {decision.order_name}  source {source.external_reference or '-'}"
              f"  [{order.get('displayFulfillmentStatus')}]")
        if decision.add_tags:
            print(f"      tags += {decision.add_tags}")
        if decision.note:
            print(f"      note += {policy.note_for(source.external_reference or source.order_reference)!r}")
        print(f"      fulfil: {'yes' if decision.fulfill else 'no'} — {decision.reason}")
        if not args.apply:
            continue
        try:
            done = apply(client, decision, policy.fulfillment.notify_customer)
            print(f"      wrote: {', '.join(done)}")
        except RuntimeError as exc:
            failed += 1
            print(f"      ! {exc}", file=sys.stderr)

    if not args.apply:
        print(f"\nPlan only — {changed} order(s) would change; re-run with --apply.")
    else:
        print(f"\n{changed - failed} order(s) updated, {failed} failed, {missing} not found.")
    return 1 if failed else 0
