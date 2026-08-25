"""The audit engine: one pure evaluator plus the store scan that feeds it.

:func:`evaluate` takes plain dicts, so every check is unit-testable without a store, and the
same function serves a live audit and a test fixture.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from coreyard.config import StoreProfile
from coreyard.profile import AuditPolicy
from coreyard.transform.render import r_number_from_handle

# Every check, in the order a report reads best. The key is what a caller filters on.
CHECKS = (
    "missing sku",
    "duplicate sku",
    "price is zero",
    "price below the floor",
    "no photos",
    "photo missing alt text",
    "description thin or empty",
    "no SEO title",
    "no SEO description",
    "no product type",
    "no vendor",
    "too few tags",
    "no shipping weight",
    "active with zero inventory",
    "suspicious title",
    "title over the limit",
)

_SCAN = """query($cursor:String,$first:Int!){
  products(first:$first, after:$cursor){
    pageInfo{ hasNextPage endCursor }
    nodes{
      id title handle status productType vendor tags descriptionHtml
      seo{ title description }
      totalInventory
      mediaCount{ count }
      media(first:1){ nodes{ alt } }
      variants(first:1){ nodes{ id sku price
        inventoryItem{ measurement{ weight{ value } } } } }
    }
  }
}"""


@dataclass(frozen=True)
class Finding:
    check: str
    label: str
    detail: str = ""


@dataclass
class Report:
    total: int = 0
    findings: dict[str, list[Finding]] = field(default_factory=dict)

    def add(self, check: str, label: str, detail: str = "") -> None:
        self.findings.setdefault(check, []).append(Finding(check, label, detail))

    def count(self, check: str) -> int:
        return len(self.findings.get(check, ()))

    def ordered(self) -> list[tuple[str, list[Finding]]]:
        return sorted(self.findings.items(), key=lambda kv: -len(kv[1]))

    @property
    def clean(self) -> bool:
        return not self.findings


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate(products: Iterable[dict], policy: Optional[AuditPolicy] = None) -> Report:
    """Run every check over a sequence of normalized product dicts.

    Each dict carries the keys :func:`scan` produces. Anything absent is treated as missing,
    which is the honest reading: a field the API did not return is a field a shopper does
    not see either.
    """
    policy = policy or AuditPolicy()
    report = Report()
    skus: collections.Counter = collections.Counter()

    for product in products:
        report.total += 1
        label = f"{(product.get('title') or '')[:56]} [{product.get('handle') or '?'}]"
        sku = (product.get("sku") or "").strip()
        title = product.get("title") or ""
        price = _number(product.get("price"))
        tags = product.get("tags") or []
        seo_title = (product.get("seo_title") or "").strip()
        seo_description = (product.get("seo_description") or "").strip()
        description = (product.get("description_html") or "").strip()

        if policy.require_sku and not sku:
            report.add("missing sku", label)
        if sku:
            skus[sku] += 1
        if price <= 0:
            report.add("price is zero", label)
        elif price < policy.min_price:
            report.add("price below the floor", label, f"${price:,.2f}")
        if policy.require_photos and not int(product.get("media_count") or 0):
            report.add("no photos", label)
        elif policy.require_alt_text and not (product.get("first_media_alt") or "").strip():
            report.add("photo missing alt text", label)
        if len(description) < policy.min_description_chars:
            report.add("description thin or empty", label, f"{len(description)} chars")
        if policy.require_seo and not seo_title:
            report.add("no SEO title", label)
        if policy.require_seo and not seo_description:
            report.add("no SEO description", label)
        if policy.require_product_type and not (product.get("product_type") or "").strip():
            report.add("no product type", label)
        if policy.require_vendor and not (product.get("vendor") or "").strip():
            report.add("no vendor", label)
        if len(tags) < policy.min_tags:
            report.add("too few tags", label, f"{len(tags)} tag(s)")
        if policy.require_weight and not _number(product.get("weight")):
            report.add("no shipping weight", label)
        if (policy.flag_active_zero_inventory and product.get("status") == "ACTIVE"
                and not int(product.get("inventory") or 0)):
            report.add("active with zero inventory", label)
        lowered = title.lower()
        if len(title) < policy.min_title_chars or any(
                word in lowered for word in policy.suspicious_title_words):
            report.add("suspicious title", label)
        if len(title) > policy.max_title_chars:
            report.add("title over the limit", label, f"{len(title)} chars")

    for sku, count in skus.items():
        if count > 1:
            # Two products sharing a SKU means two listings for one physical part, so one of
            # them will be sold and never pulled.
            report.add("duplicate sku", sku, f"x{count}")
    return report


def scan(client, store: StoreProfile, ours_only: bool = True,
         page_size: int = 100) -> list[dict]:
    """Read the catalogue into the flat dicts :func:`evaluate` expects."""
    out: list[dict] = []
    for node in client.paginate(_SCAN, "products", page_size=page_size):
        if ours_only and r_number_from_handle(node["handle"], store) is None:
            continue
        variants = node["variants"]["nodes"]
        variant = variants[0] if variants else {}
        item = variant.get("inventoryItem") or {}
        seo = node.get("seo") or {}
        media_nodes = (node.get("media") or {}).get("nodes") or []
        out.append({
            "id": node["id"],
            "handle": node["handle"],
            "title": node.get("title") or "",
            "status": node.get("status") or "",
            "product_type": node.get("productType") or "",
            "vendor": node.get("vendor") or "",
            "tags": node.get("tags") or [],
            "description_html": node.get("descriptionHtml") or "",
            "seo_title": seo.get("title") or "",
            "seo_description": seo.get("description") or "",
            "inventory": node.get("totalInventory") or 0,
            "media_count": (node.get("mediaCount") or {}).get("count", 0),
            "first_media_alt": (media_nodes[0].get("alt") if media_nodes else "") or "",
            "sku": variant.get("sku") or "",
            "price": variant.get("price") or "0",
            "weight": (((item.get("measurement") or {}).get("weight") or {}).get("value") or 0),
        })
    return out
