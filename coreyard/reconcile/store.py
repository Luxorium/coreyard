"""Read the store's side of the reconciliation: every product this installation published.

Keyed by handle prefix rather than by SKU. The handle is CoreYard's own identity for a part
and nothing else on the store carries it, so a store that also sells merchandise, or was
migrated from another system, cannot have its products mistaken for parts and archived.
"""

from __future__ import annotations

from typing import Optional

from coreyard.config import StoreProfile
from coreyard.reconcile.planner import ShopProduct
from coreyard.sink.shopify_api import ShopifyClient
from coreyard.transform.render import r_number_from_handle

_SCAN = """query($cursor:String,$first:Int!){
  products(first:$first, after:$cursor){
    pageInfo{ hasNextPage endCursor }
    nodes{
      id handle title status publishedAt
      variants(first:1){ nodes{ id sku inventoryQuantity
        inventoryItem{ id tracked } } }
    }
  }
}"""


def scan(client: ShopifyClient, store: StoreProfile,
         page_size: int = 250) -> dict[str, ShopProduct]:
    """Every product on the store under this installation's handle prefix, keyed by R#."""
    found: dict[str, ShopProduct] = {}
    for node in client.paginate(_SCAN, "products", page_size=page_size):
        r_number = r_number_from_handle(node["handle"], store)
        if not r_number:
            continue
        variants = node["variants"]["nodes"]
        variant = variants[0] if variants else {}
        item = variant.get("inventoryItem") or {}
        found[r_number] = ShopProduct(
            r_number=r_number,
            product_id=node["id"],
            status=node["status"],
            # publishedAt is null exactly when a product is on no channel that makes it
            # visible, which is the condition worth repairing.
            published=bool(node.get("publishedAt")),
            quantity=int(variant.get("inventoryQuantity") or 0),
            inventory_item_id=item.get("id") or "",
            tracked=bool(item.get("tracked")),
            handle=node["handle"],
            title=node.get("title") or "",
        )
    return found


def published_r_numbers(client: ShopifyClient, store: StoreProfile,
                        exclude_status: Optional[str] = "ARCHIVED") -> set[str]:
    """R#s currently on the store, skipping the status retirement produces.

    The state file only knows what *it* published, which is not the same as what is on the
    store: the bulk path tracks progress in its own log, and a fresh state file knows nothing
    at all. Asking Shopify directly is what lets a first incremental run retire parts that
    sold before the state existed.
    """
    return {r for r, product in scan(client, store).items()
            if product.status != exclude_status}
