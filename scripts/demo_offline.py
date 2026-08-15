"""Offline end-to-end demo (no database needed).

Builds a few realistic parts for R#s whose photos exist on the server, fetches
those real images over SMB, and writes a Shopify CSV — proving transform + image fetch +
CSV framing before the live SQL port is open. Image URLs use a placeholder host to show
the exact format; swap in the real image base (or use the API sink) later.

    python scripts/demo_offline.py
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.yms.images import SmbImageStore
from coreyard.transform.shopify_csv import write_csv

DEMO_STORE = StoreProfile(
    vendor="Demo Salvage Yard", city="Springfield, IL", warranty="90-day warranty"
)

DEMO_IMAGE_BASE = "https://YOUR-IMAGE-HOST/coreyard"  # placeholder to illustrate CSV format

PARTS = [
    Part(r_number="10002", stock_number="251001", interchange_number="590-00123",
         part_type="Engine Control Module ECM",
         year=2014, make="Ford", model="Fusion", price=Decimal("129.95"),
         interchange_code="00123", grade="A", mileage=78000),
    Part(r_number="10015", stock_number="251002", interchange_number="105-01234",
         part_type="Front Bumper Assembly",
         year=2016, make="Chevrolet", model="Silverado 1500", price=Decimal("349.00"),
         grade="B"),
    Part(r_number="10016", stock_number="251003", interchange_number="114-00567L",
         part_type="Headlamp Assembly, Left",
         year=2013, make="Toyota", model="Camry", price=Decimal("89.50"), grade="A"),
]


def main() -> int:
    store = SmbImageStore()
    dest_root = Path("out/images")

    def resolver(part: Part) -> list[str]:
        # Fetch the real photos (proves SMB path) and emit hosted-style URLs.
        key = part.image_key()
        store.fetch(key, dest_root / key)
        names = store.list_inventory_images(key)
        return [f"{DEMO_IMAGE_BASE}/{key}/{n}" for n in names]

    out = Path("out/products.demo.csv")
    products, rows = write_csv(PARTS, out, resolver, DEMO_STORE)
    print(f"Wrote {products} products / {rows} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
