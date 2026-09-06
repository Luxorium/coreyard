"""Marketplace pricing derived from source prices and configured shipping policy."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from coreyard.config import _get
from coreyard.transform import seo
from coreyard.transform.render import resolve_shipping


@dataclass(frozen=True)
class PricePolicy:
    markup_percent: Decimal = Decimal("0")
    include_shipping: bool = False

    @classmethod
    def configured(cls):
        try:
            percent = Decimal(_get("EBAY_PRICE_MARKUP_PERCENT", "0") or "0")
        except InvalidOperation as exc:
            raise ValueError("EBAY_PRICE_MARKUP_PERCENT must be a number") from exc
        if not percent.is_finite() or percent < 0:
            raise ValueError("EBAY_PRICE_MARKUP_PERCENT must be finite and nonnegative")
        include = (_get("EBAY_INCLUDE_SHOPIFY_SHIPPING", "false") or "").lower()
        return cls(percent, include in {"1", "true", "yes", "on"})


def quote(part, store, mode, policy, freight_policies=None):
    """Return exact cents and any required freight policy; shipping is not marked up."""
    if part.price is None or not part.price.is_finite() or part.price <= 0:
        raise ValueError("source system has no positive price")
    base = part.price * (Decimal("1") + policy.markup_percent / Decimal("100"))
    shipping = Decimal("0")
    target_policy = ""
    group = resolve_shipping(part, store, seo.expand_part_type(part.part_type, store))
    freight_policies = freight_policies or {}
    if group and group.id in freight_policies:
        target_policy = str(freight_policies[group.id])
    elif policy.include_shipping:
        if mode == "free":
            if not group or not group.shippable:
                raise ValueError("free-shipping listing has no Shopify shipping rate")
            shipping = Decimal(group.price)
            if not shipping.is_finite() or shipping < 0:
                raise ValueError("Shopify shipping rate must be finite and nonnegative")
        elif mode not in {"pickup", "paid"}:
            raise ValueError("listing shipping policy is unknown")
    amount = (base + shipping).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"new_price": format(amount, ".2f"),
            "source_price": format(part.price, "f"),
            "markup_percent": format(policy.markup_percent, "f"),
            "shipping_included": format(shipping, ".2f"),
            "shipping_group": group.id if group else "",
            "shipping_policy": target_policy}
