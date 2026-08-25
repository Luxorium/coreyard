"""Shopify Admin API client and the serializer that turns a rendered product into input.

The direct-publish path: instead of a manual CSV upload, the translator writes straight to
the store over the Admin GraphQL API. It also uploads part photos as media, so no public
image host is required.

Auth: a custom-app Admin API access token (``shpat_…``) in ``SHOPIFY_ADMIN_TOKEN`` and the
store domain in ``SHOPIFY_STORE`` (``your-store.myshopify.com``). Only the ``requests``
library is needed (already present / pinned in requirements.txt).

:class:`ShopifyClient` is the one Shopify client in this project. Everything that talks to
the store — sync, bulk load, reconciliation, repair, audit, order polling, webhook
registration — goes through it, so retry, throttle handling, pagination and the API version
are decided in one place and tested once. Adding a second client is how those quietly drift
into five different retry policies, four of which are wrong.

The sink keys products by the unique R# via a stable handle (``<prefix>-<R#>``). It uses
``productSet`` (idempotent upsert by handle) so re-running updates rather than duplicates.
Network calls are guarded so the module imports without credentials.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from coreyard.config import StoreProfile, _get, load_env
from coreyard.models import Part
from coreyard.transform import tags as tag_policy
from coreyard.transform.render import RenderedProduct, render

DEFAULT_API_VERSION = "2026-07"

# Shopify's GraphQL cost budget refills continuously; dropping below this many points means
# the next few calls are about to be throttled, so a bulk walk pauses briefly instead of
# earning a 429 and a full backoff.
THROTTLE_FLOOR = 200


@dataclass(frozen=True)
class ShopifyCreds:
    store: str          # your-store.myshopify.com
    token: str          # shpat_...
    api_version: str = DEFAULT_API_VERSION

    @property
    def endpoint(self) -> str:
        return f"https://{self.store}/admin/api/{self.api_version}/graphql.json"

    def rest(self, path: str) -> str:
        return f"https://{self.store}/admin/api/{self.api_version}/{path.lstrip('/')}"


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
        self._resume_at = 0.0

    # -- transport ----------------------------------------------------------
    def _wait_for_budget(self) -> None:
        delay = self._resume_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _note_cost(self, payload: dict[str, Any]) -> None:
        """Pause the client when the store's query budget is nearly spent."""
        status = (((payload.get("extensions") or {}).get("cost") or {})
                  .get("throttleStatus") or {})
        available = status.get("currentlyAvailable")
        if available is not None and available < THROTTLE_FLOOR:
            self._resume_at = max(self._resume_at, time.monotonic() + 1.0)

    def graphql(self, query: str, variables: dict[str, Any] | None = None,
                tries: int = 5) -> dict[str, Any]:
        """POST a GraphQL op, retrying on throttling, raising on errors."""
        for attempt in range(tries):
            self._wait_for_budget()
            try:
                resp = self._session.post(
                    self.creds.endpoint,
                    json={"query": query, "variables": variables or {}},
                    timeout=60,
                )
            except self._requests.RequestException:
                # A dropped connection mid-catalogue-walk is ordinary; only give up once the
                # retries are spent, so a long run survives a blip.
                if attempt < tries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 500 and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            self._note_cost(data)
            if data.get("errors"):
                # THROTTLED shows up here too on GraphQL
                msg = str(data["errors"])
                if "THROTTLED" in msg and attempt < tries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"Shopify GraphQL errors: {msg}")
            return data["data"]
        raise RuntimeError("Shopify GraphQL: exhausted retries (throttled)")

    def mutate(self, document: str, variables: dict[str, Any], root: str) -> dict[str, Any]:
        """Run a mutation and raise on ``userErrors`` rather than returning a quiet failure.

        Shopify reports business-rule refusals as a 200 with ``userErrors`` populated, so a
        caller that only checks for exceptions treats "I did not do that" as success.
        """
        result = self.graphql(document, variables)[root] or {}
        errors = result.get("userErrors") or []
        if errors:
            raise RuntimeError(f"{root}: {json.dumps(errors)[:400]}")
        return result

    def paginate(self, query: str, connection: str,
                 variables: dict[str, Any] | None = None,
                 page_size: int = 250, max_pages: int | None = None) -> Iterator[dict]:
        """Walk a Relay connection, yielding each node.

        ``query`` must accept ``$cursor`` and ``$first`` and select ``pageInfo`` plus
        ``nodes``; ``connection`` names the field to walk (``"products"``).
        """
        cursor = None
        pages = 0
        while True:
            page = self.graphql(query, {**(variables or {}),
                                        "cursor": cursor, "first": page_size})[connection]
            for node in page["nodes"]:
                yield node
            pages += 1
            if not page["pageInfo"]["hasNextPage"] or (max_pages and pages >= max_pages):
                return
            cursor = page["pageInfo"]["endCursor"]

    def rest_get(self, path: str, tries: int = 5) -> dict[str, Any]:
        """GET one REST resource.

        The Admin API answers GraphQL in camelCase and REST in snake_case, and CoreYard's
        order pipeline parses the snake_case shape Shopify POSTs to a webhook. Fetching an
        order over GraphQL and feeding it to that pipeline is not an error, it is worse: the
        parser finds no ``line_items``, decides the order contains nothing of ours, and
        reports success having booked nothing. So orders are re-read over REST.
        """
        url = self.creds.rest(path)
        for attempt in range(tries):
            self._wait_for_budget()
            try:
                resp = self._session.get(url, timeout=60)
            except self._requests.RequestException:
                if attempt < tries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise
            if resp.status_code == 429 or (resp.status_code >= 500 and attempt < tries - 1):
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Shopify REST: exhausted retries for {path}")

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

    def access_scopes(self) -> set[str]:
        data = self.graphql("{ currentAppInstallation { accessScopes { handle } } }")
        install = data.get("currentAppInstallation") or {}
        return {s["handle"] for s in (install.get("accessScopes") or [])}

    def publication_id(self, name: str) -> Optional[str]:
        """The id of a sales channel publication by name, or None if the store has none."""
        nodes = self.graphql(
            "{ publications(first: 50) { nodes { id name } } }"
        )["publications"]["nodes"]
        for node in nodes:
            if node["name"].strip().lower() == name.strip().lower():
                return node["id"]
        return None


# --- GraphQL documents -----------------------------------------------------
_PRODUCT_SET = """
mutation Upsert($input: ProductSetInput!) {
  productSet(synchronous: true, input: $input) {
    product { id handle }
    userErrors { field message }
  }
}
"""


def product_set_input(
    product: RenderedProduct,
    status: str = "DRAFT",
    existing_tags: Optional[list[str]] = None,
    preserved_prefixes: tuple[str, ...] = (),
    preserve_namespaced: bool = True,
) -> dict[str, Any]:
    """Serialize the canonical rendered product into a ``ProductSetInput``.

    ``productSet`` replaces the fields it is given, so the tag list has to be the *whole*
    intended list. ``existing_tags`` is the live product's current tags: tags another system
    owns are merged back in, because sending only CoreYard's would delete them. See
    :mod:`coreyard.transform.tags`.
    """
    variant: dict[str, Any] = {
        "sku": product.sku,
        "price": product.price,
        "inventoryPolicy": "DENY",
        "optionValues": [{"optionName": "Title", "name": "Default Title"}],
    }
    if product.weight_value is not None and product.weight_unit:
        # Shipping weight belongs to the inventory item, and it is set here on every publish
        # rather than by a later backfill: a product that reaches a shopper weighing nothing
        # quotes a carrier rate for an empty box.
        variant["inventoryItem"] = {
            "measurement": {
                "weight": {"value": float(product.weight_value), "unit": product.weight_unit}
            }
        }
    return {
        "handle": product.handle,
        "title": product.title,
        "descriptionHtml": product.description_html,
        "vendor": product.vendor,
        "productType": product.product_type,
        "status": status,
        "tags": tag_policy.merge(product.tags, existing_tags, preserved_prefixes,
                                 preserve_namespaced),
        "seo": {"title": product.seo_title, "description": product.seo_description},
        "productOptions": [
            {"name": "Title", "values": [{"name": "Default Title"}]}
        ],
        "variants": [variant],
    }


def part_to_product_set_input(
    part: Part, store: StoreProfile, status: str = "DRAFT",
    existing_tags: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Render a part and build the ``ProductSetInput`` that upserts it by handle.

    Defaults to DRAFT because publishing is the irreversible-ish direction: a draft that
    should have been live is a click away, whereas an unreviewed catalogue that went live
    is already in front of customers and in search results.
    """
    return product_set_input(
        render(part, part.images, store), status, existing_tags,
        tuple(store.catalog.preserved_tag_prefixes),
        store.catalog.preserve_namespaced_tags,
    )


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
        result = self.client.mutate(
            _PRODUCT_SET, {"input": part_to_product_set_input(part, self.store)}, "productSet"
        )
        return result["product"]

    # Photos, inventory quantity, status preservation and sold-part retirement live in
    # sink/shopify_write.ShopifyPublisher, which is what run_sync --sink api drives. This
    # class stays the thin client/connectivity layer.


if __name__ == "__main__":
    # Connectivity check only: prints the shop name if creds are set.
    print("Connected to Shopify store:", ShopifySink().check())
