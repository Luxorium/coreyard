"""Compare live products against the canonical renderer and produce the minimal rewrite.

Everything a repair could want to change is a field of
:class:`~coreyard.transform.render.RenderedProduct`, so there is exactly one place that
decides what a product *should* say and this module only decides what to do about the
difference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from coreyard.config import StoreProfile
from coreyard.sink.shopify_api import ShopifyClient
from coreyard.transform import tags as tag_policy
from coreyard.transform.render import RenderedProduct, r_number_from_handle
from coreyard.transform.weights import GRAMS_PER_UNIT, Weight

# Repairable fields, in the order a report reads best.
FIELDS = ("title", "type", "tags", "seo", "description", "weight", "metafields")

_SCAN = """query($cursor:String,$first:Int!,$ns:String!){
  products(first:$first, after:$cursor){
    pageInfo{ hasNextPage endCursor }
    nodes{
      id handle title status productType tags descriptionHtml
      seo{ title description }
      metafields(first:50, namespace:$ns){ nodes{ key type value } }
      variants(first:1){ nodes{ id sku
        inventoryItem{ id measurement{ weight{ value unit } } } } }
    }
  }
}"""

_PRODUCT_UPDATE = """mutation($input:ProductUpdateInput!){
  productUpdate(product:$input){ product{ id } userErrors{ field message } } }"""

_ITEM_UPDATE = """mutation($id:ID!,$w:Float!,$u:WeightUnit!){
  inventoryItemUpdate(id:$id, input:{ measurement:{ weight:{ value:$w, unit:$u } } }){
    inventoryItem{ id } userErrors{ field message } } }"""

_METAFIELDS_SET = """mutation($fields:[MetafieldsSetInput!]!){
  metafieldsSet(metafields:$fields){ metafields{ key } userErrors{ field message } } }"""


@dataclass(frozen=True)
class LiveProduct:
    """A product as the store currently renders it."""

    r_number: str
    product_id: str
    status: str
    handle: str
    title: str = ""
    product_type: str = ""
    tags: tuple[str, ...] = ()
    description_html: str = ""
    seo_title: str = ""
    seo_description: str = ""
    inventory_item_id: str = ""
    weight_value: Optional[float] = None
    weight_unit: str = ""
    # key -> (type, value), in this installation's configured namespace.
    metafields: tuple[tuple[str, str, str], ...] = ()


@dataclass
class Change:
    """One product's worth of repair."""

    r_number: str
    product_id: str
    handle: str
    fields: dict[str, tuple[object, object]] = field(default_factory=dict)
    inventory_item_id: str = ""
    namespace: str = ""
    dropped_tags: list[str] = field(default_factory=list)

    def names(self) -> list[str]:
        return sorted(self.fields)


def scan(client: ShopifyClient, store: StoreProfile,
         page_size: int = 100) -> dict[str, LiveProduct]:
    """Every product under this installation's handle prefix, with its current copy."""
    found: dict[str, LiveProduct] = {}
    variables = {"ns": store.catalog.metafield_namespace}
    for node in client.paginate(_SCAN, "products", variables, page_size=page_size):
        r_number = r_number_from_handle(node["handle"], store)
        if not r_number:
            continue
        variants = node["variants"]["nodes"]
        variant = variants[0] if variants else {}
        item = variant.get("inventoryItem") or {}
        weight = ((item.get("measurement") or {}).get("weight") or {})
        seo = node.get("seo") or {}
        found[r_number] = LiveProduct(
            r_number=r_number,
            product_id=node["id"],
            status=node["status"],
            handle=node["handle"],
            title=node.get("title") or "",
            product_type=node.get("productType") or "",
            tags=tuple(node.get("tags") or ()),
            description_html=node.get("descriptionHtml") or "",
            seo_title=(seo.get("title") or ""),
            seo_description=(seo.get("description") or ""),
            inventory_item_id=item.get("id") or "",
            weight_value=weight.get("value"),
            weight_unit=(weight.get("unit") or ""),
            metafields=tuple(
                (m["key"], m.get("type") or "", m.get("value") or "")
                for m in (node.get("metafields") or {}).get("nodes", [])),
        )
    return found


def _same_weight(live: LiveProduct, wanted: Optional[Weight]) -> bool:
    """Weights match when they mean the same mass, whatever unit each is expressed in."""
    if wanted is None:
        return True                      # no rule for this part: leave whatever is there
    if live.weight_value is None or not live.weight_unit:
        return False
    if live.weight_unit not in GRAMS_PER_UNIT:
        return False
    live_grams = float(live.weight_value) * GRAMS_PER_UNIT[live.weight_unit]
    return abs(live_grams - wanted.grams) < 1.0


def plan(
    live: Mapping[str, LiveProduct],
    desired: Mapping[str, RenderedProduct],
    fields: Sequence[str] = FIELDS,
    *,
    store: Optional[StoreProfile] = None,
    weights_by_type: Optional[Mapping[str, Weight]] = None,
) -> list[Change]:
    """Differences worth writing, one entry per product.

    ``desired`` holds the canonical render for parts still in the yard's extract.
    ``weights_by_type`` supplies a weight for products that are not — an archived part still
    needs a sane shipping weight if it is ever revived, and its part type is enough to look
    one up without going near the database.
    """
    store = store or StoreProfile()
    policy = store.catalog
    wanted_fields = [f for f in fields if f in FIELDS]
    changes: list[Change] = []

    for r_number in sorted(live):
        current = live[r_number]
        product = desired.get(r_number)
        change = Change(r_number=r_number, product_id=current.product_id,
                        handle=current.handle,
                        inventory_item_id=current.inventory_item_id,
                        namespace=policy.metafield_namespace)

        if product is not None:
            if "title" in wanted_fields and current.title != product.title:
                change.fields["title"] = (current.title, product.title)
            if "type" in wanted_fields and current.product_type != product.product_type:
                change.fields["type"] = (current.product_type, product.product_type)
            if "description" in wanted_fields and (
                    current.description_html != product.description_html):
                change.fields["description"] = (current.description_html,
                                                product.description_html)
            if "seo" in wanted_fields and (
                    current.seo_title != product.seo_title
                    or current.seo_description != product.seo_description):
                change.fields["seo"] = (
                    (current.seo_title, current.seo_description),
                    (product.seo_title, product.seo_description),
                )
            if "tags" in wanted_fields:
                merged = tag_policy.merge(
                    product.tags, current.tags,
                    tuple(policy.preserved_tag_prefixes), policy.preserve_namespaced_tags,
                    store.shipping.owned_prefixes)
                if merged != list(current.tags):
                    change.fields["tags"] = (list(current.tags), merged)
                    change.dropped_tags = tag_policy.dropped(
                        product.tags, current.tags,
                        tuple(policy.preserved_tag_prefixes),
                        policy.preserve_namespaced_tags,
                        store.shipping.owned_prefixes)

            if "metafields" in wanted_fields:
                live = {k: (t, v) for k, t, v in current.metafields}
                wanted = {m.key: (m.type, m.value) for m in product.metafields}
                # Only additions and corrections. Removing a metafield whose value vanished
                # is the publisher's job during a normal upsert, where it knows the value
                # really is gone rather than merely unasked-for here.
                differing = {k: v for k, v in wanted.items() if live.get(k) != v}
                if differing:
                    change.fields["metafields"] = (
                        {k: live.get(k) for k in differing}, differing)

        if "weight" in wanted_fields and current.inventory_item_id:
            wanted = (product.weight if product is not None
                      else (weights_by_type or {}).get(current.product_type.lower()))
            if not _same_weight(current, wanted) and wanted is not None:
                change.fields["weight"] = (
                    (current.weight_value, current.weight_unit),
                    (wanted.value, wanted.unit),
                )
        if change.fields:
            changes.append(change)
    return changes


def apply(client: ShopifyClient, change: Change) -> None:
    """Write one product's repairs. Raises on the first refusal."""
    product: dict = {}
    if "title" in change.fields:
        product["title"] = change.fields["title"][1]
    if "type" in change.fields:
        product["productType"] = change.fields["type"][1]
    if "description" in change.fields:
        product["descriptionHtml"] = change.fields["description"][1]
    if "tags" in change.fields:
        product["tags"] = change.fields["tags"][1]
    if "seo" in change.fields:
        title, description = change.fields["seo"][1]
        product["seo"] = {"title": title, "description": description}
    if product:
        product["id"] = change.product_id
        client.mutate(_PRODUCT_UPDATE, {"input": product}, "productUpdate")
    if "weight" in change.fields:
        value, unit = change.fields["weight"][1]
        client.mutate(
            _ITEM_UPDATE,
            {"id": change.inventory_item_id, "w": float(value), "u": unit},
            "inventoryItemUpdate",
        )
    if "metafields" in change.fields:
        client.mutate(_METAFIELDS_SET, {"fields": [
            {"ownerId": change.product_id, "namespace": change.namespace,
             "key": key, "type": kind, "value": value}
            for key, (kind, value) in change.fields["metafields"][1].items()
        ]}, "metafieldsSet")


def weights_by_product_type(store: StoreProfile,
                            product_types: Iterable[str]) -> dict[str, Weight]:
    """Resolve the site's weight table once per distinct product type."""
    table: dict[str, Weight] = {}
    for product_type in {(t or "").strip() for t in product_types if (t or "").strip()}:
        found = store.weights.lookup(product_type)
        if found is not None:
            table[product_type.lower()] = found
    return table
