"""Publish parts to Shopify via the Admin GraphQL API (products + photos + inventory).

Idempotent by handle (``<prefix>-<R#>``): looks up the product, then `productSet` creates or
updates it (title, description, tags, price, and inventory quantity at the yard location, with
tracking on). Photos are uploaded from the SMB share via staged uploads and attached — only when the
product has none yet, so re-runs don't duplicate images. Products are created as DRAFT by default
so nothing goes public until you flip the status.
"""

from __future__ import annotations

import mimetypes
import tempfile
import time
from pathlib import Path
from typing import Optional

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.yms.images import SmbImageStore
from coreyard.sink.shopify_api import ShopifyClient, part_to_product_set_input
from coreyard.transform import seo
from coreyard.transform.shopify_product import handle_for

_FIND = """query($h:String!){ productByIdentifier(identifier:{handle:$h}){
  id status media(first:250){ nodes{ id } } } }"""

_RETIRE_FIND = """query($h:String!){ productByIdentifier(identifier:{handle:$h}){
  id status variants(first:1){ nodes{ id inventoryItem{ id tracked } } } } }"""

_PRODUCT_UPDATE = """mutation($input:ProductUpdateInput!){ productUpdate(product:$input){
  product{ id status } userErrors{ field message } } }"""

_SET_QUANTITIES = """mutation($input:InventorySetQuantitiesInput!){
  inventorySetQuantities(input:$input){ userErrors{ field message } } }"""

# productDeleteMedia went the way of productCreateMedia/productUpdateMedia in 2026-07.
# Product media are Files now, so they are removed with fileDelete.
_DELETE_FILES = """mutation($ids:[ID!]!){ fileDelete(fileIds:$ids){
  deletedFileIds userErrors{ field message } } }"""

_SET = """mutation($input:ProductSetInput!){ productSet(synchronous:true, input:$input){
  product{ id handle } userErrors{ field message } } }"""

_STAGE = """mutation($input:[StagedUploadInput!]!){ stagedUploadsCreate(input:$input){
  stagedTargets{ url resourceUrl parameters{ name value } } userErrors{ field message } } }"""

# productCreateMedia was removed from the Admin API; photos are now attached by passing
# `files` to productSet in the same upsert.


class ShopifyPublisher:
    def __init__(
        self,
        store: Optional[StoreProfile] = None,
        status: str = "DRAFT",
        require_images: bool = False,
        retire_status: str = "ARCHIVED",
    ) -> None:
        from coreyard.config import load_store

        self.client = ShopifyClient()
        self.store = store or load_store()
        self.status = status
        # ARCHIVED, not DRAFT: new products are published as DRAFT pending review, so
        # reusing DRAFT for sold parts would make "not looked at yet" and "gone" the same
        # state and there would be no way to filter one from the other in Admin.
        self.retire_status = retire_status
        self.require_images = require_images
        self.location = self.client.primary_location_id()
        self.images = SmbImageStore()

    # -- product ------------------------------------------------------------
    def _find(self, handle: str) -> tuple[Optional[str], list[str], Optional[str]]:
        """Return (product id, existing media ids, current status) for a handle."""
        r = self.client.graphql(_FIND, {"h": handle})["productByIdentifier"]
        if not r:
            return None, [], None
        return r["id"], [n["id"] for n in r["media"]["nodes"]], r.get("status")

    def _upsert(self, part: Part, product_id: Optional[str],
                files: Optional[list] = None, status: Optional[str] = None) -> str:
        inp = part_to_product_set_input(part, self.store)
        if files:
            inp["files"] = files
        # productSet has "set" semantics: an omitted status would be reset to the default,
        # so an existing product's status is read back and re-sent. Otherwise every sync
        # would drag a product the owner had activated by hand back to DRAFT.
        inp["status"] = status or self.status
        if product_id:
            inp["id"] = product_id
        inp["variants"][0]["inventoryItem"] = {"tracked": True}
        inp["variants"][0]["inventoryQuantities"] = [
            {"locationId": self.location, "name": "available", "quantity": max(int(part.quantity), 0)}
        ]
        res = self.client.graphql(_SET, {"input": inp})["productSet"]
        if res["userErrors"]:
            raise RuntimeError(f"productSet R#{part.r_number}: {res['userErrors']}")
        return res["product"]["id"]

    # -- images -------------------------------------------------------------
    def _staged_files(self, part: Part) -> list[dict]:
        """Upload this part's photos to Shopify's staging area.

        Returns FileSetInput dicts ready to hand to productSet — each with alt text, since
        media created without it is invisible to image search.
        """
        import requests

        with tempfile.TemporaryDirectory(prefix=f"coreyard-r{part.r_number}-") as tmp:
            paths = self.images.fetch(part.image_key(), Path(tmp))
            if not paths:
                if self.require_images:
                    raise RuntimeError(f"R#{part.r_number} is marked as having images, but none were found")
                return []

            mime_types = [mimetypes.guess_type(p.name)[0] or "image/jpeg" for p in paths]
            stage_in = [
                {"resource": "IMAGE", "filename": p.name, "mimeType": mime, "httpMethod": "POST"}
                for p, mime in zip(paths, mime_types)
            ]
            staged = self.client.graphql(_STAGE, {"input": stage_in})["stagedUploadsCreate"]
            if staged["userErrors"]:
                raise RuntimeError(f"staged upload R#{part.r_number}: {staged['userErrors']}")
            targets = staged["stagedTargets"]
            if len(targets) != len(paths):
                raise RuntimeError(
                    f"staged upload R#{part.r_number}: expected {len(paths)} targets, got {len(targets)}"
                )

            urls = []
            session = requests.Session()
            for p, mime, target in zip(paths, mime_types, targets):
                form = [(x["name"], x["value"]) for x in target["parameters"]]
                last_error: Exception | None = None
                for attempt in range(4):
                    try:
                        with p.open("rb") as image_file:
                            resp = session.post(
                                target["url"],
                                data=form,
                                files={"file": (p.name, image_file, mime)},
                                timeout=90,
                            )
                        resp.raise_for_status()
                        last_error = None
                        break
                    except (requests.RequestException, OSError) as exc:
                        last_error = exc
                        if attempt < 3:
                            time.sleep(2 ** attempt)
                if last_error:
                    raise RuntimeError(f"image upload R#{part.r_number} ({p.name}): {last_error}")
                urls.append((target["resourceUrl"], p.name))

            return [
                {
                    "originalSource": url,
                    "contentType": "IMAGE",
                    "alt": seo.image_alt(part, i),
                    "filename": name,
                }
                for i, (url, name) in enumerate(urls, start=1)
            ]

    # -- orchestration ------------------------------------------------------
    def publish(self, part: Part, refresh_images: bool = False) -> tuple[str, int]:
        """Create/update one part; returns (product_id, images_added).

        Photos are staged before the upsert so productSet can attach them in the same call.
        A product that already has media keeps it, so re-runs never duplicate images.

        ``refresh_images`` is for the case the state diff says this part's photo set changed
        on the share: the old media are deleted and the current set re-uploaded. It costs a
        full re-upload, so callers should pass it only on a detected change, never blanket.
        """
        product_id, media_ids, status = self._find(handle_for(part, self.store))
        if refresh_images and media_ids:
            res = self.client.graphql(_DELETE_FILES, {"ids": media_ids})["fileDelete"]
            if res["userErrors"]:
                raise RuntimeError(f"fileDelete R#{part.r_number}: {res['userErrors']}")
            media_ids = []
        files = self._staged_files(part) if not media_ids else []
        product_id = self._upsert(part, product_id, files, status)
        return product_id, len(files)

    def retire(self, r_number: str) -> str:
        """Take a sold part off sale: zero its inventory, then set the retire status.

        Returns one of "retired", "already", or "absent". Inventory is zeroed *before* the
        status change so that a failure part-way through leaves the part unbuyable rather
        than visible with stock. Retiring is deliberately not a delete: the product, its
        photos and its URL stay put, so an existing link or search result lands on a real
        page instead of a 404, and the part can be revived if it comes back.
        """
        # Built through handle_for, not by hand: the slugging rules must not drift between
        # the code that publishes a handle and the code that goes looking for it.
        handle = handle_for(Part(r_number=str(r_number), part_type=""), self.store)
        found = self.client.graphql(_RETIRE_FIND, {"h": handle})["productByIdentifier"]
        if not found:
            return "absent"

        variants = found["variants"]["nodes"]
        if variants and variants[0]["inventoryItem"].get("tracked"):
            res = self.client.graphql(_SET_QUANTITIES, {"input": {
                "name": "available",
                "reason": "correction",
                "quantities": [{
                    "inventoryItemId": variants[0]["inventoryItem"]["id"],
                    "locationId": self.location,
                    "quantity": 0,
                }],
            }})["inventorySetQuantities"]
            if res["userErrors"]:
                raise RuntimeError(f"inventorySetQuantities R#{r_number}: {res['userErrors']}")

        if found["status"] == self.retire_status:
            return "already"
        res = self.client.graphql(_PRODUCT_UPDATE, {
            "input": {"id": found["id"], "status": self.retire_status}
        })["productUpdate"]
        if res["userErrors"]:
            raise RuntimeError(f"productUpdate R#{r_number}: {res['userErrors']}")
        return "retired"


def main() -> int:
    import sys

    from coreyard.yms.db import connect
    from coreyard.yms.interchange import InterchangeResolver
    from coreyard.yms.inventory import fetch_parts

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    status = sys.argv[2] if len(sys.argv) > 2 else "DRAFT"
    parts = fetch_parts(limit=limit, images_only=True)
    print(f"Resolving interchange fitment for {len(parts)} parts…")
    with connect() as c:
        InterchangeResolver(c).attach(parts)
    pub = ShopifyPublisher(status=status)
    print(f"Publishing {len(parts)} parts to Shopify as {status} (location {pub.location.split('/')[-1]})…\n")
    for i, part in enumerate(parts, 1):
        pid, imgs = pub.publish(part)
        num = pid.split("/")[-1]
        print(f"  [{i:>2}/{len(parts)}] R#{part.r_number:<10} {part.fitment_label()} {part.part_type[:28]:<28} "
              f"${part.price}  +{imgs} photo(s)  product {num}")
    print("\nDone. Review drafts in Shopify Admin → Products (filter by Status: Draft).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
