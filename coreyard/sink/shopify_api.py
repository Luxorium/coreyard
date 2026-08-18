"""Shopify Admin GraphQL sink — create/update products, inventory, and images directly.

The direct-publish path: instead of a manual CSV upload, the translator writes straight to
the store over the Admin GraphQL API. It also uploads part photos as media, so no public
image host is required.

Auth: a custom-app Admin API access token (``shpat_…``) in ``SHOPIFY_ADMIN_TOKEN`` and the
store domain in ``SHOPIFY_STORE`` (``your-store.myshopify.com``). Only the ``requests``
library is needed (already present / pinned in requirements.txt).

The sink keys products by the unique R# via a stable handle (``<prefix>-<R#>``). It uses
``productSet`` where available (idempotent upsert by handle) so re-running updates rather
than duplicates. Network calls are guarded so the module imports without credentials.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from coreyard.config import StoreProfile, _get, load_env
from coreyard.models import Part
from coreyard.transform import seo
from coreyard.transform.pricing import retail_str
from coreyard.transform.shopify_product import handle_for

DEFAULT_API_VERSION = "2026-07"


@dataclass(frozen=True)
class ShopifyCreds:
    store: str          # your-store.myshopify.com
    token: str          # shpat_...
    api_version: str = DEFAULT_API_VERSION

    @property
    def endpoint(self) -> str:
        return f"https://{self.store}/admin/api/{self.api_version}/graphql.json"


def load_creds() -> ShopifyCreds:
    load_env()
    store = (_get("SHOPIFY_STORE", "") or "").strip()
    token = (_get("SHOPIFY_ADMIN_TOKEN", "") or "").strip()
    api_version = (_get("SHOPIFY_API_VERSION", DEFAULT_API_VERSION)
                   or DEFAULT_API_VERSION).strip()
    if not store or not token:
        raise RuntimeError(
            "Set SHOPIFY_STORE and SHOPIFY_ADMIN_TOKEN in .env to use the Admin API sink."
        )
    return ShopifyCreds(store=store, token=token, api_version=api_version)


class ShopifyClient:
    def __init__(self, creds: Optional[ShopifyCreds] = None) -> None:
        self.creds = creds or load_creds()
        import requests  # lazy; keeps the package importable without the dep

        self._requests = requests
        self._session = requests.Session()
        self._session.headers.update(
            {
                "X-Shopify-Access-Token": self.creds.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST a GraphQL op, retrying on throttling, raising on userErrors."""
        for attempt in range(5):
            resp = self._session.post(
                self.creds.endpoint,
                json={"query": query, "variables": variables or {}},
                timeout=60,
            )
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data and data["errors"]:
                # THROTTLED shows up here too on GraphQL
                msg = str(data["errors"])
                if "THROTTLED" in msg and attempt < 4:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"Shopify GraphQL errors: {msg}")
            return data["data"]
        raise RuntimeError("Shopify GraphQL: exhausted retries (throttled)")

    # -- helpers ------------------------------------------------------------
    def shop_name(self) -> str:
        return self.graphql("{ shop { name myshopifyDomain } }")["shop"]["name"]

    def primary_location_id(self) -> str:
        data = self.graphql(
            "{ locations(first: 1, query: \"active:true\") { edges { node { id name } } } }"
        )
        edges = data["locations"]["edges"]
        if not edges:
            raise RuntimeError("No active Shopify location found.")
        return edges[0]["node"]["id"]


# --- GraphQL documents -----------------------------------------------------
_PRODUCT_SET = """
mutation Upsert($input: ProductSetInput!) {
  productSet(synchronous: true, input: $input) {
    product { id handle }
    userErrors { field message }
  }
}
"""


def part_to_product_set_input(
    part: Part, store: StoreProfile, status: str = "DRAFT"
) -> dict[str, Any]:
    """Build a ProductSetInput that upserts by handle (idempotent).

    Defaults to DRAFT because publishing is the irreversible-ish direction: a draft that
    should have been live is a click away, whereas an unreviewed catalogue that went live
    is already in front of customers and in search results.
    """
    return {
        "handle": handle_for(part, store),
        "title": seo.build_title(part),
        "descriptionHtml": seo.build_body_html(part, store),
        "vendor": store.vendor,
        "productType": seo.expand_part_type(part.part_type),
        "status": status,
        "tags": seo.build_tags(part, store),
        "seo": {
            "title": seo.meta_title(part),
            "description": seo.meta_description(part, store),
        },
        "productOptions": [
            {"name": "Title", "values": [{"name": "Default Title"}]}
        ],
        "variants": [
            {
                "sku": part.r_number,
                "price": retail_str(part.price, default="0.00"),
                "inventoryPolicy": "DENY",
                "optionValues": [{"optionName": "Title", "name": "Default Title"}],
            }
        ],
    }


class ShopifySink:
    """High-level operations the orchestrator calls."""

    def __init__(
        self, client: Optional[ShopifyClient] = None, store: Optional[StoreProfile] = None
    ) -> None:
        from coreyard.config import load_store

        self.client = client or ShopifyClient()
        self.store = store or load_store()

    def check(self) -> str:
        return self.client.shop_name()

    def upsert_part(self, part: Part) -> dict[str, Any]:
        """Create or update a product for one part. Returns {id, handle}."""
        data = self.client.graphql(
            _PRODUCT_SET, {"input": part_to_product_set_input(part, self.store)}
        )
        result = data["productSet"]
        errs = result["userErrors"]
        if errs:
            raise RuntimeError(f"productSet userErrors for R#{part.r_number}: {errs}")
        return result["product"]

    # Photos, inventory quantity, status preservation and sold-part retirement live in
    # sink/shopify_write.ShopifyPublisher, which is what run_sync --sink api drives. This
    # class stays the thin client/connectivity layer.


if __name__ == "__main__":
    # Connectivity check only: prints the shop name if creds are set.
    print("Connected to Shopify store:", ShopifySink().check())
