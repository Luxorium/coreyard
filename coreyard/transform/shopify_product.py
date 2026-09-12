"""Serialize a :class:`~coreyard.transform.render.RenderedProduct` into Shopify CSV rows.

A Shopify product import row set is: one "primary" row carrying all product + default
variant fields (and the first image), followed by one image-only row per additional image
that shares the ``Handle`` and leaves the other columns blank. This module owns that
framing and nothing else.

It deliberately renders no text of its own. Titles, tags, descriptions and SEO metadata come
from the canonical renderer, the same object the Admin API sink serializes and the same one
the sync fingerprints — see :mod:`coreyard.transform.render` for why that matters.
"""

from __future__ import annotations

from typing import Optional

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.transform.render import (
    TITLE_MAX,
    RenderedProduct,
    handle_for,
    handle_prefix,
    r_number_from_handle,
    render,
)

__all__ = [
    "TITLE_MAX",
    "extra_image_row",
    "handle_for",
    "handle_prefix",
    "part_to_rows",
    "primary_row",
    "r_number_from_handle",
    "rows_for",
]


def primary_row(product: RenderedProduct, status: str = "DRAFT") -> dict[str, str]:
    """The main product row: all product + default-variant fields, plus image #1.

    ``status`` is the intended status for a *new* product, and it defaults to DRAFT for the
    same reason the Admin API sink does: publishing is the irreversible-ish direction, and a
    draft that should have been live is one click away, while an unreviewed catalogue that
    went live is already in front of customers and in search results. This column used to
    say ``active`` unconditionally, so the two sinks disagreed about the one decision the
    operator is asked to make — and `--status`, which the CLI offers on every sync, changed
    nothing at all on this path.
    """
    first_image = product.images[0] if product.images else ""
    first_alt = product.image_alts[0] if product.image_alts else ""
    # Shopify's CSV importer takes grams in "Variant Grams"; the unit column selects how the
    # value is *displayed*, so both are emitted from the one resolved weight rather than
    # letting the two disagree.
    grams = str(product.weight_grams) if product.weight_grams else ""
    return {
        "Handle": product.handle,
        "Title": product.title,
        "Body (HTML)": product.description_html,
        "Vendor": product.vendor,
        "Type": product.product_type,
        "Tags": ", ".join(product.tags),
        # Shopify's importer reads both: `Status` is the product's state and `Published`
        # decides the Online Store channel. A draft that says TRUE here is a contradiction
        # the importer resolves by publishing it, which is the outcome DRAFT exists to
        # prevent.
        "Published": "TRUE" if status.upper() == "ACTIVE" else "FALSE",
        "Option1 Name": "Title",
        "Option1 Value": "Default Title",
        "Variant SKU": product.sku,
        "Variant Grams": grams,
        "Variant Inventory Tracker": "shopify",
        "Variant Inventory Qty": str(product.inventory),
        "Variant Inventory Policy": "deny",   # unique salvage parts must not oversell
        "Variant Fulfillment Service": "manual",
        "Variant Price": product.price,
        "Variant Requires Shipping": "TRUE",
        "Variant Taxable": "TRUE",
        "Variant Weight Unit": "g" if grams else "",
        "Image Src": first_image,
        "Image Position": "1" if first_image else "",
        "Image Alt Text": first_alt if first_image else "",
        "SEO Title": product.seo_title,
        "SEO Description": product.seo_description,
        "Status": status.lower(),
    }


def extra_image_row(product: RenderedProduct, position: int) -> dict[str, str]:
    """An image-only continuation row (only Handle + image columns are set)."""
    index = position - 1
    return {
        "Handle": product.handle,
        "Image Src": product.images[index],
        "Image Position": str(position),
        "Image Alt Text": (product.image_alts[index]
                           if index < len(product.image_alts) else ""),
    }


def rows_for(product: RenderedProduct, status: str = "DRAFT") -> list[dict[str, str]]:
    """Full row set for one rendered product: primary row + one row per extra image."""
    rows = [primary_row(product, status)]
    for position in range(2, len(product.images) + 1):
        rows.append(extra_image_row(product, position))
    return rows


def part_to_rows(
    part: Part, image_urls: list[str], store: Optional[StoreProfile] = None,
    status: str = "DRAFT",
) -> list[dict[str, str]]:
    """Render one part and serialize it to CSV rows."""
    return rows_for(render(part, image_urls, store), status)
