"""Transform a normalized :class:`~coreyard.models.Part` into Shopify product CSV rows.

A Shopify product import row set is: one "primary" row carrying all product + default
variant fields (and the first image), followed by one image-only row per additional
image that shares the ``Handle`` and leaves the other columns blank. This module owns
the field mapping and the human-facing text (title, description, tags); the CSV framing
lives in :mod:`coreyard.transform.shopify_csv`.
"""

from __future__ import annotations

import html
import re
from typing import Optional

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.transform.pricing import retail_str

# Shopify's product title maximum is 255 characters.
TITLE_MAX = 255


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def handle_for(part: Part, store: Optional[StoreProfile] = None) -> str:
    """Stable, URL-safe, unique product handle keyed on the never-reused R#.

    The prefix comes from the store profile and must stay fixed for the life of the store —
    it is how every re-run finds the product it already published.
    """
    store = store or StoreProfile()
    return f"{_slug(store.handle_prefix)}-{_slug(part.uid())}"


def build_title(part: Part) -> str:
    """"{year} {make} {model} {side} {part type}", trimmed to Shopify's limit at a word
    boundary. Falls back to the part type alone when fitment is unknown.

    ``side`` is carried on the Part rather than glued onto ``part_type``, so it has to be
    reinserted here or Left/Right would vanish from the CSV listing.
    """
    bits = [part.fitment_label(), part.side or "", part.part_type]
    base = " ".join(b.strip() for b in bits if b and b.strip())
    base = re.sub(r"\s+", " ", base)
    if len(base) <= TITLE_MAX:
        return base
    cut = base[:TITLE_MAX]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.strip()


def build_tags(part: Part) -> list[str]:
    """Filterable storefront tags. Order is stable so the CSV content-hash (used for
    change detection) only moves when the underlying data moves."""
    tags: list[str] = []
    if part.year:
        tags.append(str(part.year))
    if part.make:
        tags.append(part.make)
    if part.model:
        tags.append(part.model)
    if part.make and part.model:
        tags.append(f"{part.make} {part.model}")
    tags.append(part.part_type)
    tags.append("Used OEM")
    if part.interchange_number:
        tags.append(f"Interchange {part.interchange_number}")
    # de-dupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def build_body_html(part: Part, store: Optional[StoreProfile] = None) -> str:
    """A clean, escaped HTML description block. Only includes rows we actually have."""
    store = store or StoreProfile()
    rows: list[tuple[str, Optional[str]]] = [
        ("Vehicle", part.fitment_label() or None),
        ("Part", part.part_type),
        ("Side", part.side),
        ("Condition", f"Grade {part.grade}" if part.grade else "Used, tested"),
        ("Interchange #", part.interchange_number),
        ("Mileage", f"{part.mileage:,}" if part.mileage else None),
        ("Stock #", part.stock_number),
        ("R#", part.r_number),
    ]
    origin = store.origin()
    lines = [
        f"<p>Used OEM part in stock at {html.escape(origin)}.</p>" if origin
        else "<p>Used OEM part in stock.</p>"
    ]
    if part.description:
        lines.append(f"<p>{html.escape(part.description)}</p>")
    lines.append("<ul>")
    for label, value in rows:
        if value:
            lines.append(f"  <li><strong>{html.escape(label)}:</strong> {html.escape(str(value))}</li>")
    lines.append("</ul>")
    return "\n".join(lines)


def image_alt(part: Part) -> str:
    label = part.fitment_label()
    return f"{label} {part.part_type}".strip() if label else part.part_type


def primary_row(part: Part, first_image_url: Optional[str], store: StoreProfile) -> dict[str, str]:
    """The main product row: all product + default-variant fields, plus image #1."""
    row = {
        "Handle": handle_for(part, store),
        "Title": build_title(part),
        "Body (HTML)": build_body_html(part, store),
        "Vendor": store.vendor,
        "Type": part.part_type,
        "Tags": ", ".join(build_tags(part)),
        "Published": "TRUE",
        "Option1 Name": "Title",
        "Option1 Value": "Default Title",
        "Variant SKU": part.r_number,
        "Variant Grams": str(part.weight_grams) if part.weight_grams else "",
        "Variant Inventory Tracker": "shopify",
        "Variant Inventory Qty": str(max(part.quantity, 0)),
        "Variant Inventory Policy": "deny",   # unique salvage parts must not oversell
        "Variant Fulfillment Service": "manual",
        "Variant Price": retail_str(part.price),
        "Variant Requires Shipping": "TRUE",
        "Variant Taxable": "TRUE",
        "Variant Weight Unit": "g" if part.weight_grams else "",
        "Image Src": first_image_url or "",
        "Image Position": "1" if first_image_url else "",
        "Image Alt Text": image_alt(part) if first_image_url else "",
        "SEO Title": build_title(part),
        "Status": "active",
    }
    return row


def extra_image_row(
    part: Part, url: str, position: int, store: Optional[StoreProfile] = None
) -> dict[str, str]:
    """An image-only continuation row (only Handle + image columns are set)."""
    return {
        "Handle": handle_for(part, store),
        "Image Src": url,
        "Image Position": str(position),
        "Image Alt Text": image_alt(part),
    }


def part_to_rows(part: Part, image_urls: list[str], store: StoreProfile) -> list[dict[str, str]]:
    """Full row set for one part: primary row + one row per additional image."""
    first = image_urls[0] if image_urls else None
    rows = [primary_row(part, first, store)]
    for i, url in enumerate(image_urls[1:], start=2):
        rows.append(extra_image_row(part, url, i, store))
    return rows
