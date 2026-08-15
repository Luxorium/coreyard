"""CSV sink: write the Shopify product-import CSV to ``out/products.csv``."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.transform.shopify_csv import ImageResolver, write_csv


def write(
    parts: Iterable[Part],
    out_path: Path,
    resolver: ImageResolver,
    store: StoreProfile,
) -> tuple[int, int]:
    """Write CSV; returns (products, rows)."""
    return write_csv(parts, out_path, resolver, store)
