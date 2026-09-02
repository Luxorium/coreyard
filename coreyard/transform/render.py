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
from coreyard.transform.shipping import ShippingGroup
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
class Metafield:
    """One structured value published alongside the product.

    Shopper-visible, so it is part of the rendered product and therefore of the
    fingerprint: a theme that renders a grade or a fitment table is showing catalogue
    content, and content that can change without moving the hash is content that goes
    stale forever.
    """

    namespace: str
    key: str
    type: str
    value: str

    @property
    def qualified(self) -> str:
        return f"{self.namespace}.{self.key}"


def fitment_rows(part: Part, store: Optional[StoreProfile] = None) -> list[dict]:
    """Every vehicle this part fits, as structured rows rather than a printed sentence.

    ``{years, make, model, note, label}``. The first four are what a storefront table
    shows; ``label`` is the exact vehicle string CoreYard also emits as a tag, so a theme
    can link to that tag without parsing a title back apart.

    A part with no catalogue fitment still fits the car it was pulled from, so the donor
    vehicle is emitted as a single row. Without that, structured fitment would be empty for
    every part the interchange catalogue does not cover, and anything built from it — a
    vehicle picker, a related-parts link — would silently lose them.
    """
    rows: list[dict] = []
    for entry in part.fitment or []:
        make = seo.clean_make(entry.make)
        model = seo.clean_model(entry.model)
        label = seo._vehicle_label(make, model)
        if not label:
            continue
        rows.append({
            "years": entry.year_label(),
            "make": make or "",
            "model": model or "",
            # The qualifier text only. `Application.label()` prefixes its own year span,
            # which the row's own `years` column already states.
            "note": "; ".join(a.note for a in entry.qualifiers()[:4] if a.note),
            "label": label,
        })
    if rows:
        return rows
    make = seo.clean_make(part.make)
    model = seo.clean_model(part.model)
    label = seo._vehicle_label(make, model)
    if not label:
        return []
    return [{
        "years": str(part.year) if part.year else "",
        "make": make or "",
        "model": model or "",
        "note": "",
        "label": label,
    }]


def build_metafields(part: Part, store: StoreProfile) -> tuple[Metafield, ...]:
    """The structured half of a listing: grade, mileage, condition, fitment.

    Only values the source actually supplied are emitted. An absent one is left out rather
    than published empty, and the publisher removes any it previously wrote — a grade that
    disappears from the yard must not keep showing on the storefront.
    """
    namespace = store.catalog.metafield_namespace
    fields: list[Metafield] = []
    if part.grade:
        fields.append(Metafield(namespace, "grade", "single_line_text_field",
                                str(part.grade).strip()))
    if part.mileage:
        fields.append(Metafield(namespace, "mileage", "number_integer", str(int(part.mileage))))
    condition = store.catalog.condition_text(part.grade)
    if condition:
        fields.append(Metafield(namespace, "condition", "single_line_text_field", condition))
    rows = fitment_rows(part, store)
    if rows:
        fields.append(Metafield(namespace, "fitment", "json",
                                json.dumps(rows, ensure_ascii=False, separators=(",", ":"))))
    return tuple(fields)


# What each sync scope owns, so a scoped run can tell an availability change from a copy
# change. ``catalog`` is deliberately "everything else": a field added to RenderedProduct
# lands in the catalog scope automatically rather than falling silently outside every scope.
INVENTORY_FIELDS = ("inventory", "price")
# The media *set*, not its alt text. Alt text is generated from the part's copy, so folding
# it in here would make a retitled part look like it needed its photos torn down and
# re-uploaded — the one expensive thing a scoped run exists to avoid. It is copy, and it
# lands in the catalog scope with the rest of the copy.
IMAGE_FIELDS = ("images",)
SCOPE_FIELDS: dict = {
    "inventory": INVENTORY_FIELDS,
    "photos": IMAGE_FIELDS,
    "catalog": None,
}


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
    metafields: tuple[Metafield, ...] = ()
    # Which shipping class this part falls in. The tag is already in ``tags``; the id is
    # kept so repair and audit can report a classification without re-deriving it.
    shipping_group: str = ""
    # The fitment key drives the entire "Fits" section but is not itself printed, so
    # re-keying a part to a different interchange would otherwise be an invisible change.
    fitment_key: str = ""

    def payload(self) -> dict:
        """The canonical dict this product hashes and serializes from."""
        data = asdict(self)
        data["tags"] = list(self.tags)
        data["images"] = list(self.images)
        data["image_alts"] = list(self.image_alts)
        data["metafields"] = [asdict(m) for m in self.metafields]
        return data

    def metafield(self, key: str) -> Optional[Metafield]:
        return next((m for m in self.metafields if m.key == key), None)

    def fingerprint(self) -> str:
        """Stable SHA-256 over everything above."""
        blob = json.dumps(self.payload(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def scope_fingerprint(self, scope: str) -> str:
        """SHA-256 over the fields one sync scope owns.

        A projection of the *same* payload :meth:`fingerprint` hashes, never a second
        rendering — which is what stops a domain fingerprint from disagreeing with the
        renderer that actually publishes. These never decide what gets written (``productSet``
        has set semantics, so a publish always sends the whole product); they only answer
        *why* a part changed, which is what lets ``coreyard sync inventory`` push a repriced
        part while leaving a merely retitled one alone.
        """
        payload = self.payload()
        keys = SCOPE_FIELDS[scope]
        if keys is None:                       # catalog: everything nobody else owns
            owned = set(INVENTORY_FIELDS) | set(IMAGE_FIELDS)
            keys = tuple(k for k in payload if k not in owned)
        blob = json.dumps({k: payload[k] for k in sorted(keys)},
                          sort_keys=True, ensure_ascii=False).encode("utf-8")
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


def resolve_shipping(part: Part, store: StoreProfile,
                     product_type: str = "") -> Optional[ShippingGroup]:
    """Which shipping class this part falls in, or None if the site configured none."""
    return store.shipping.classify(product_type or None, part.part_type)


def render(
    part: Part,
    images: Optional[Sequence[str]] = None,
    store: Optional[StoreProfile] = None,
) -> RenderedProduct:
    """Render one part into the canonical product both sinks publish."""
    store = store or StoreProfile()
    urls = [str(u) for u in (images or [])]
    product_type = seo.expand_part_type(part.part_type, store)
    override = store.overrides.for_r_number(part.r_number)
    # ``compact`` spans the years ("1998-2000") instead of listing them ("1998 1999 2000").
    # The builder's own note argues the listed form tokenizes better for web search, and that
    # is a real trade — but the two storefronts were publishing the same part under two
    # different-looking titles, and the site chose the spanned form for both. One title, both
    # channels, which is the whole point of a single renderer.
    title = override.title or seo.build_title(part, store, compact=True)
    weight = resolve_weight(part, store, product_type)
    # Classified here, not by a later pass over the catalogue: a product that is live and
    # sellable before anything has said how it ships is a product that can be bought with
    # the wrong shipping attached to it.
    shipping = resolve_shipping(part, store, product_type)
    tags = list(seo.build_tags(part, store))
    if shipping:
        tags.append(shipping.tag)
    return RenderedProduct(
        handle=handle_for(part, store),
        title=title,
        description_html=seo.build_body_html(part, store),
        vendor=store.vendor,
        product_type=product_type,
        tags=tuple(tags),
        seo_title=(seo.reviewed_meta_title(title) if override.title
                   else seo.meta_title(part, store)),
        seo_description=seo.meta_description(part, store),
        sku=str(part.r_number),
        # A researched price is already a deliberate shopper-facing amount; storefront
        # charm rounding must not silently move it after the research decision.
        price=(f"{override.price:.2f}" if override.price is not None
               else retail_str(part.price, default="0.00")),
        inventory=max(int(part.quantity or 0), 0),
        images=tuple(urls),
        image_alts=tuple(seo.image_alt(part, i, store) for i in range(1, len(urls) + 1)),
        weight_value=weight.value if weight else None,
        weight_unit=weight.unit if weight else "",
        weight_grams=weight.grams if weight else None,
        metafields=build_metafields(part, store),
        shipping_group=shipping.id if shipping else "",
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
    "Metafield",
    "RenderedProduct",
    "TITLE_MAX",
    "build_metafields",
    "fingerprint",
    "fitment_rows",
    "handle_for",
    "handle_prefix",
    "r_number_from_handle",
    "render",
    "resolve_shipping",
    "resolve_weight",
]
