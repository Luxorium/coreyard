"""Site policy for order lifecycle synchronization, supplied as configuration.

When the source system invoices a storefront sale, the storefront should say so. What
"saying so" means is partly generic — tag the order, note the source reference — and partly
a decision only the site can make, because it depends on who actually ships the parcel.

That last part matters more than it looks. Shopify has no "picked but not shipped" state:
creating a fulfillment closes the fulfillment order. If a third-party shipping app is going
to buy the label and attach the tracking number, it needs that fulfillment order left open,
and fulfilling early costs the buyer their tracking email. If nothing else will ever touch
the order — a freight shipment booked by phone, a counter pickup — then nobody but this job
will ever close it, and leaving it open means it stays "unfulfilled" forever.

CoreYard cannot know which is which, because it depends on the site's shipping arrangements.
So the site describes them, keyed on the product tags its own storefront already uses:

    STORE_ORDER_POLICY_FILE=/path/to/order-sync.json

    {
      "tags": ["invoiced"],
      "reference_tag": "wo-{reference}",
      "note": "Source work order {reference} invoiced",
      "fulfillment": {
        "groups": ["ship:pickup-only", "ship:freight-heavy", "ship:parcel"],
        "default_group": "ship:parcel",
        "defer_groups": ["ship:parcel"],
        "notify_customer": false
      }
    }

``groups`` are the product tags that classify how a line ships, most specific first;
``default_group`` is what a line with none of them counts as; ``defer_groups`` are the
groups an external shipper owns. An order with any deferred line is left open — a mixed
order still has parcels to ship, and closing it early would cost the buyer their tracking.
With no ``groups`` configured, nothing is ever fulfilled and the job only tags and notes.

**Omit the whole ``fulfillment`` block if a shipping policy is configured.** The shipping
policy already names every group, its tag, and — through each group's ``fulfillment`` key —
who ships it, so restating that here is a second copy free to drift from the first. When
this block is absent it is derived from :mod:`coreyard.transform.shipping`; when it is
present it wins, so an existing configuration keeps working unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from coreyard.transform.shipping import EXTERNAL


class OrderPolicyError(RuntimeError):
    """The configured order policy file is missing, unreadable, or the wrong shape."""


@dataclass(frozen=True)
class FulfillmentPolicy:
    groups: tuple[str, ...] = ()
    default_group: str = ""
    defer_groups: tuple[str, ...] = ()
    notify_customer: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.groups)

    def group_of(self, product_tags: Sequence[str]) -> str:
        """Which shipping group a line belongs to, by its product's tags."""
        lowered = {str(t).strip().lower() for t in product_tags or ()}
        for group in self.groups:
            if group.lower() in lowered:
                return group
        return self.default_group

    def defers(self, groups: Sequence[str]) -> bool:
        """True when some line is somebody else's to ship, so the order stays open."""
        deferred = {g.lower() for g in self.defer_groups}
        return any((g or "").lower() in deferred for g in groups)


@dataclass(frozen=True)
class OrderPolicy:
    """What to write onto a Shopify order once the source system has invoiced it."""

    tags: tuple[str, ...] = ("invoiced",)
    # Optional second tag carrying the source reference, e.g. "wo-{reference}".
    reference_tag: str = ""
    note: str = "Source order {reference} invoiced"
    fulfillment: FulfillmentPolicy = field(default_factory=FulfillmentPolicy)
    # True when the file declared its own fulfillment block, so derivation must not
    # overwrite a deliberate choice.
    explicit_fulfillment: bool = False

    def with_shipping(self, shipping) -> "OrderPolicy":
        """Fill in the fulfillment rules from the shipping policy, if it did not say.

        The shipping policy is the one place a group's tag and its shipper are declared, so
        an order policy that stays quiet inherits them rather than repeating them.
        """
        if self.explicit_fulfillment or not getattr(shipping, "configured", False):
            return self
        return replace(self, fulfillment=fulfillment_from_shipping(shipping))

    def tags_for(self, reference: str) -> list[str]:
        wanted = [t for t in self.tags if t]
        if self.reference_tag and reference:
            wanted.append(self.reference_tag.format(reference=reference))
        return wanted

    def note_for(self, reference: str) -> str:
        return self.note.format(reference=reference) if self.note else ""


def fulfillment_from_shipping(shipping) -> FulfillmentPolicy:
    """Derive fulfillment rules from the shipping policy's own group declarations."""
    groups = tuple(g.tag for g in shipping.groups if not g.default)
    default = next((g.tag for g in shipping.groups if g.default), "")
    if default:
        groups = groups + (default,)
    defer = tuple(g.tag for g in shipping.groups if g.fulfillment == EXTERNAL)
    return FulfillmentPolicy(groups=groups, default_group=default, defer_groups=defer)


DEFAULT_POLICY = OrderPolicy()


def from_dict(data: Mapping[str, Any]) -> OrderPolicy:
    if not isinstance(data, Mapping):
        raise OrderPolicyError("an order policy must be a JSON object")
    values = {k: v for k, v in data.items() if not str(k).startswith("_")}
    known = set(OrderPolicy.__dataclass_fields__) - {"explicit_fulfillment"}
    unknown = [k for k in values if k not in known]
    if unknown:
        raise OrderPolicyError("unknown key(s) in order policy: " + ", ".join(sorted(unknown)))
    kwargs: dict[str, Any] = {}
    if "tags" in values:
        kwargs["tags"] = tuple(str(t).strip() for t in values["tags"] if str(t).strip())
    if "reference_tag" in values:
        kwargs["reference_tag"] = str(values["reference_tag"]).strip()
    if "note" in values:
        kwargs["note"] = str(values["note"])
    if "fulfillment" in values:
        raw = values["fulfillment"] or {}
        extra = set(raw) - set(FulfillmentPolicy.__dataclass_fields__)
        if extra:
            raise OrderPolicyError(
                "unknown key(s) in order policy 'fulfillment': " + ", ".join(sorted(extra)))
        kwargs["fulfillment"] = FulfillmentPolicy(
            groups=tuple(str(g).strip() for g in raw.get("groups", ()) if str(g).strip()),
            default_group=str(raw.get("default_group", "")).strip(),
            defer_groups=tuple(str(g).strip() for g in raw.get("defer_groups", ())
                               if str(g).strip()),
            notify_customer=bool(raw.get("notify_customer", False)),
        )
        kwargs["explicit_fulfillment"] = True
    return replace(DEFAULT_POLICY, **kwargs)


def load(path: "str | Path | None") -> OrderPolicy:
    """Read the order policy at ``path``; with no path, tag-and-note only."""
    if not path:
        return DEFAULT_POLICY
    target = Path(path).expanduser()
    if not target.exists():
        raise OrderPolicyError(
            f"STORE_ORDER_POLICY_FILE points at {target}, which does not exist."
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OrderPolicyError(f"{target} is not valid JSON: {exc}") from exc
    return from_dict(data)


def load_configured() -> OrderPolicy:
    from coreyard.config import _get, load_env, load_store

    from coreyard import store

    load_env()
    policy = store.resolve("orders", "STORE_ORDER_POLICY_FILE", load, from_dict)
    return policy.with_shipping(load_store().shipping)


def resolve(policy: Optional[OrderPolicy] = None) -> OrderPolicy:
    return policy if policy is not None else load_configured()
