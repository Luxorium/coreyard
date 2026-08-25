"""Write a Shopify product-import CSV from normalized parts.

Shopify's importer matches columns by header name, ignores unknown columns, and tolerates
omitted optional ones — so we emit exactly the subset we populate, in a stable order.
Image URLs are supplied by a pluggable ``resolver`` so the same writer serves the
"products first, no images" default and a hosted-image resolver without
changing the transform.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable, Iterable, Optional

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.transform.shopify_product import part_to_rows

# Stable header order. Every value we set in shopify_product.py appears here.
SHOPIFY_COLUMNS: list[str] = [
    "Handle",
    "Title",
    "Body (HTML)",
    "Vendor",
    "Type",
    "Tags",
    "Published",
    "Option1 Name",
    "Option1 Value",
    "Variant SKU",
    "Variant Grams",
    "Variant Inventory Tracker",
    "Variant Inventory Qty",
    "Variant Inventory Policy",
    "Variant Fulfillment Service",
    "Variant Price",
    "Variant Requires Shipping",
    "Variant Taxable",
    "Variant Weight Unit",
    "Image Src",
    "Image Position",
    "Image Alt Text",
    "SEO Title",
    "SEO Description",
    "Status",
]

# A resolver maps a Part to an ordered list of public image URLs (empty = no images).
ImageResolver = Callable[[Part], list[str]]


def _no_images(_: Part) -> list[str]:
    return []


def rows_for_parts(
    parts: Iterable[Part],
    resolver: ImageResolver = _no_images,
    store: Optional[StoreProfile] = None,
) -> Iterable[dict[str, str]]:
    """Yield CSV row dicts for all listable parts (skips non-priced rows defensively)."""
    store = store or StoreProfile()
    for part in parts:
        if not part.is_listable():
            continue
        for row in part_to_rows(part, resolver(part), store):
            yield row


def write_csv(
    parts: Iterable[Part],
    path: Path,
    resolver: ImageResolver = _no_images,
    store: Optional[StoreProfile] = None,
) -> tuple[int, int]:
    """Write the Shopify CSV. Returns (product_count, row_count)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    products = 0
    rows = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SHOPIFY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows_for_parts(parts, resolver, store):
            # A primary row always carries a Title; continuation image rows do not.
            if row.get("Title"):
                products += 1
            writer.writerow(row)
            rows += 1
    return products, rows
