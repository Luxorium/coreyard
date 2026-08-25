"""Domain models shared across the pipeline.

`Part` is the neutral contract between the source extract layer and the Shopify
transform layer. The extract layer (``coreyard.yms.inventory``) is responsible for
mapping real source columns onto these fields; everything downstream depends only
on this shape, never on the database schema. That keeps the schema-specific SQL in one
place and lets the transform/CSV/state code be built and unit-tested with synthetic
parts before the live database is even reachable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

# Donor-vehicle detail keys carried on ``Part.vehicle``, in the order they are rendered.
# Defined here, on the neutral contract, so the extract layer that fills the dict and the
# transform layer that renders it agree without either importing the other.
VEHICLE_FIELDS = ("engine", "transmission", "drivetrain", "body", "trim", "doors")


@dataclass
class Part:
    """A single salvage part, normalized from the source inventory.

    Only ``r_number`` and ``part_type`` are strictly required to produce a listing.
    The remaining fields are optional so a sparse source row still yields a usable
    (if plainer) Shopify product. ``images`` holds resolved image references (local
    paths when writing CSV, hosted URLs once an image host is wired in).
    """

    # The source system's unique, never-reused part id ("R#"). It is the Shopify SKU,
    # product-handle key, sync-state key, and photo filename stem.
    r_number: str
    part_type: str

    # The donor vehicle's stock number. Every part removed from the same donor shares
    # this value, so it must never be used as a product identity or SKU.
    stock_number: Optional[str] = None

    # The customer-facing interchange number, e.g. "545-01883".
    interchange_number: Optional[str] = None

    # The part-type code plus the fitment key form the internal lookup used to resolve full fitment.
    # Keep this short code separate from the full customer-facing interchange number.
    part_type_code: Optional[int] = None
    interchange_code: Optional[str] = None

    # "Left" or "Right" from the source system's side code. Kept out of ``part_type`` so the
    # part-type expansion table still matches, and so the renderer can phrase it for
    # shoppers ("Driver Side Left") instead of leaking the raw code.
    side: Optional[str] = None

    # Consolidated interchange fitment (which vehicles fit), filled by the interchange resolver.
    fitment: list = field(default_factory=list)

    # Fitment (make/model resolved by joins declared in the site's schema mapping)
    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None

    # Commercial
    price: Optional[Decimal] = None
    quantity: int = 1
    status: Optional[str] = None  # raw source status string, e.g. "A"/"Available"

    # Descriptive / SEO signal
    description: Optional[str] = None      # freeform source description, if any
    grade: Optional[str] = None            # condition/grade (A/B/C or text)
    mileage: Optional[int] = None
    location: Optional[str] = None         # bin/row/yard location
    vin: Optional[str] = None              # donor VIN (not published; internal)
    weight_grams: Optional[int] = None

    # Alternate shopper vocabulary for this part type ("taillamp" for "Tail Light"), used to
    # widen search/tag coverage. Empty unless the site maps a source for it *and* enables it:
    # these strings reach the rendered product, so filling them in moves every fingerprint.
    aliases: list[str] = field(default_factory=list)

    # Donor-vehicle specifics (engine, transmission, drivetrain, body, trim, doors) when the
    # source system decoded them. Same fingerprint caveat as ``aliases``.
    vehicle: dict = field(default_factory=dict)

    # Media — resolved image references (paths or URLs), ordered as they should appear
    images: list[str] = field(default_factory=list)

    def image_key(self) -> str:
        """Filename stem for this part's photos: its unique R#."""
        return str(self.r_number).strip()

    def uid(self) -> str:
        """Stable unique identity for this physical part: its R#."""
        return str(self.r_number).strip()

    def fitment_label(self) -> str:
        """Human-readable "2014 Ford Fusion" style prefix, omitting missing pieces."""
        parts = [str(self.year) if self.year else None, self.make, self.model]
        return " ".join(p for p in parts if p).strip()

    def is_listable(self) -> bool:
        """Stage-scope gate: we only publish parts that have a positive price.

        Availability is enforced upstream in the SQL WHERE clause; this is the final
        transform-side guard so a mispriced row can never reach Shopify.
        """
        return bool(self.uid()) and self.price is not None and self.price > 0
