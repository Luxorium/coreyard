"""The canonical rendered product: one description of what a shopper will see.

Every publishing path goes through here. A ``Part`` plus its photos and the store profile
render to exactly one :class:`RenderedProduct`, and that object is what gets fingerprinted,
serialized to the Admin API, and serialized to CSV.

That is not tidiness, it is a correctness property. The two sinks used to render their own
titles, tags and descriptions from the same ``Part`` through different code, and the sync
fingerprint covered only one of them. Changing the renderer the API sink actually publishes
through therefore left the stored fingerprint identical, so the next run classified every
stale product as "unchanged" and the storefront kept showing output no current version of
the code would produce. A single representation makes that impossible by construction: if a
change is visible to a shopper it is in this object, and if it is in this object it moves
the fingerprint.

The renderer is a pure function of its arguments. Site-specific policy arrives on the
``StoreProfile`` rather than being read from the environment here, which is what keeps the
fingerprint reproducible and lets the whole render path be unit-tested with no ``.env``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Optional, Sequence

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.transform import seo
from coreyard.transform.pricing import retail_str
from coreyard.transform.weights import GRAMS_PER_UNIT, Weight

# Shopify's product title maximum. Enforced by the SEO builder; restated here because the
# CSV serializer has to promise it too.
TITLE_MAX = seo.TITLE_MAX


def _slug(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def handle_for(part: Part, store: Optional[StoreProfile] = None) -> str:
    """Stable, URL-safe, unique product handle keyed on the never-reused R#.

    The prefix comes from the store profile and must stay fixed for the life of the store —
    it is how every re-run finds the product it already published.
    """
    store = store or StoreProfile()
    return f"{_slug(store.handle_prefix)}-{_slug(part.uid())}"


def handle_prefix(store: Optional[StoreProfile] = None) -> str:
    """The ``<prefix>-`` every handle this installation publishes begins with."""
    return handle_for(Part(r_number="", part_type=""), store)


def r_number_from_handle(handle: str, store: Optional[StoreProfile] = None) -> Optional[str]:
    """The R# a handle encodes, or None if the handle is not one of ours."""
    prefix = handle_prefix(store)
    return handle[len(prefix):] if handle.startswith(prefix) else None


@dataclass(frozen=True)
class RenderedProduct:
    """Everything CoreYard considers shopper-visible and sync-controlled for one part.

    Anything a shopper, a search engine, or a shipping calculator can see belongs in here.
    Anything CoreYard deliberately does not own — the product's status, its sales-channel
    publication, tags another system added — deliberately does not, because those are
    preserved from the live product rather than generated, and folding them in would make
    the fingerprint depend on the store's state instead of the yard's.
    """

    handle: str
    title: str
    description_html: str
    vendor: str
    product_type: str
    tags: tuple[str, ...]
    seo_title: str
    seo_description: str
    sku: str
    price: str
    inventory: int
    images: tuple[str, ...] = ()
    image_alts: tuple[str, ...] = ()
    weight_value: Optional[float] = None
    weight_unit: str = ""
    weight_grams: Optional[int] = None
    # The fitment key drives the entire "Fits" section but is not itself printed, so
    # re-keying a part to a different interchange would otherwise be an invisible change.
    fitment_key: str = ""

    def payload(self) -> dict:
        """The canonical dict this product hashes and serializes from."""
        data = asdict(self)
        data["tags"] = list(self.tags)
        data["images"] = list(self.images)
        data["image_alts"] = list(self.image_alts)
        return data

    def fingerprint(self) -> str:
        """Stable SHA-256 over everything above."""
        blob = json.dumps(self.payload(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    @property
    def weight(self) -> Optional[Weight]:
        if self.weight_value is None or not self.weight_unit:
            return None
        return Weight(self.weight_value, self.weight_unit)


def resolve_weight(part: Part, store: StoreProfile, product_type: str = "") -> Optional[Weight]:
    """This part's shipping weight: the source system's, else the site's rules table.

    A weight recorded against the actual part always wins — it was measured, and the table
    is an estimate by part type. The table is consulted against both the published product
    type and the raw source wording, so a site's rules keep matching whichever of the two
    they were written from.
    """
    if part.weight_grams:
        return Weight(float(part.weight_grams), "GRAMS", "source")
    return store.weights.lookup(product_type or None, part.part_type)


def render(
    part: Part,
    images: Optional[Sequence[str]] = None,
    store: Optional[StoreProfile] = None,
) -> RenderedProduct:
    """Render one part into the canonical product both sinks publish."""
    store = store or StoreProfile()
    urls = [str(u) for u in (images or [])]
    product_type = seo.expand_part_type(part.part_type, store)
    weight = resolve_weight(part, store, product_type)
    return RenderedProduct(
        handle=handle_for(part, store),
        title=seo.build_title(part, store),
        description_html=seo.build_body_html(part, store),
        vendor=store.vendor,
        product_type=product_type,
        tags=tuple(seo.build_tags(part, store)),
        seo_title=seo.meta_title(part, store),
        seo_description=seo.meta_description(part, store),
        sku=str(part.r_number),
        price=retail_str(part.price, default="0.00"),
        inventory=max(int(part.quantity or 0), 0),
        images=tuple(urls),
        image_alts=tuple(seo.image_alt(part, i, store) for i in range(1, len(urls) + 1)),
        weight_value=weight.value if weight else None,
        weight_unit=weight.unit if weight else "",
        weight_grams=weight.grams if weight else None,
        fitment_key=part.interchange_code or "",
    )


def fingerprint(
    part: Part,
    images: Optional[Sequence[str]] = None,
    store: Optional[StoreProfile] = None,
) -> str:
    """Convenience: render ``part`` and hash the result."""
    return render(part, images, store).fingerprint()


__all__ = [
    "GRAMS_PER_UNIT",
    "RenderedProduct",
    "TITLE_MAX",
    "fingerprint",
    "handle_for",
    "handle_prefix",
    "r_number_from_handle",
    "render",
    "resolve_weight",
]
