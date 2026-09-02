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
import uuid
from pathlib import Path
from typing import Optional

from coreyard.config import StoreProfile, publication_names
from coreyard.models import Part
from coreyard.yms.images import SmbImageStore, donor_photo_limit
from coreyard.sink.shopify_api import ShopifyClient, product_set_input
from coreyard.transform.render import RenderedProduct, handle_for, render

# Tags come back with the product because productSet replaces the whole tag list: without
# knowing what is already there, a routine update deletes whatever another system added.
_FIND = """query($h:String!,$ns:String!){ productByIdentifier(identifier:{handle:$h}){
  id status tags
  media(first:250){ nodes{ id } }
  metafields(first:50, namespace:$ns){ nodes{ id key } } } }"""

_RETIRE_FIND = """query($h:String!){ productByIdentifier(identifier:{handle:$h}){
  id status variants(first:1){ nodes{ id inventoryItem{ id tracked } } } } }"""

_PRODUCT_UPDATE = """mutation($input:ProductUpdateInput!){ productUpdate(product:$input){
  product{ id status } userErrors{ field message } } }"""

_SET_QUANTITIES = """mutation($input:InventorySetQuantitiesInput!,$idempotencyKey:String!){
  inventorySetQuantities(input:$input) @idempotent(key:$idempotencyKey){
    userErrors{ field message }
  }
}"""

# productDeleteMedia went the way of productCreateMedia/productUpdateMedia in 2026-07.
# Product media are Files now, so they are removed with fileDelete.
_DELETE_FILES = """mutation($ids:[ID!]!){ fileDelete(fileIds:$ids){
  deletedFileIds userErrors{ field message } } }"""

_SET = """mutation($input:ProductSetInput!){ productSet(synchronous:true, input:$input){
  product{ id handle } userErrors{ field message } } }"""

_FILE_CREATE = """mutation($files:[FileCreateInput!]!){ fileCreate(files:$files){
  files { id fileStatus } userErrors { field message } } }"""
_STAGE = """mutation($input:[StagedUploadInput!]!){ stagedUploadsCreate(input:$input){
  stagedTargets{ url resourceUrl parameters{ name value } } userErrors{ field message } } }"""

_PUBLISH = """mutation($id:ID!,$input:[PublicationInput!]!){
  publishablePublish(id:$id, input:$input){ userErrors{ field message } } }"""

# productSet leaves a metafield it was not told about untouched, so a value that vanished
# from the yard would keep showing on the storefront forever. Deleting is explicit.
_METAFIELDS_DELETE = """mutation($ids:[MetafieldIdentifierInput!]!){
  metafieldsDelete(metafields:$ids){ deletedMetafields{ key } userErrors{ field message } } }"""

# productCreateMedia was removed from the Admin API; photos are now attached by passing
# `files` to productSet in the same upsert.


class ShopifyPublisher:
    def __init__(
        self,
        store: Optional[StoreProfile] = None,
        status: str = "DRAFT",
        require_images: bool = False,
        retire_status: str = "ARCHIVED",
        publications: Optional[list[str]] = None,
        donor_files=None,
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
        # Remembers which donor frame is already a Shopify file, so the same photograph is
        # not uploaded once per part off that donor. Optional: without it the donor frames
        # are simply staged per product, which is correct but far slower.
        self.donor_files = donor_files
        # Status and channel publication are different things: an ACTIVE product that is on
        # no channel is a 404 to every shopper and to Google. Configured names are resolved
        # once, here, so a typo fails at startup instead of per product.
        self.publications = self._resolve_publications(
            publications if publications is not None else publication_names())

    def _resolve_publications(self, names: list[str]) -> list[str]:
        ids = []
        for name in names:
            found = self.client.publication_id(name)
            if not found:
                raise RuntimeError(
                    f"STORE_PUBLICATIONS names {name!r}, which is not a sales channel on "
                    f"this store."
                )
            ids.append(found)
        return ids

    # -- product ------------------------------------------------------------
    def _find(self, handle: str):
        """Return (product id, media ids, status, tags, metafield keys) for a handle."""
        r = self.client.graphql(
            _FIND, {"h": handle, "ns": self.store.catalog.metafield_namespace}
        )["productByIdentifier"]
        if not r:
            return None, [], None, [], []
        metafields = [n["key"] for n in (r.get("metafields") or {}).get("nodes", [])]
        return (r["id"], [n["id"] for n in r["media"]["nodes"]], r.get("status"),
                list(r.get("tags") or []), metafields)

    def _prune_metafields(self, product_id: str, present: list[str],
                          product: RenderedProduct) -> None:
        """Delete metafields in our namespace that this render no longer produces."""
        wanted = {m.key for m in product.metafields}
        stale = [key for key in present if key not in wanted]
        if not stale:
            return
        namespace = self.store.catalog.metafield_namespace
        self.client.mutate(_METAFIELDS_DELETE, {"ids": [
            {"ownerId": product_id, "namespace": namespace, "key": key} for key in stale
        ]}, "metafieldsDelete")

    def _upsert(self, product: RenderedProduct, quantity: int, product_id: Optional[str],
                files: Optional[list] = None, status: Optional[str] = None,
                existing_tags: Optional[list[str]] = None) -> str:
        policy = self.store.catalog
        inp = product_set_input(
            product, status or self.status, existing_tags,
            tuple(policy.preserved_tag_prefixes), policy.preserve_namespaced_tags,
            self.store.shipping.owned_prefixes,
        )
        if files:
            inp["files"] = files
        if product_id:
            inp["id"] = product_id
        item = inp["variants"][0].setdefault("inventoryItem", {})
        item["tracked"] = True
        inp["variants"][0]["inventoryQuantities"] = [
            {"locationId": self.location, "name": "available", "quantity": max(quantity, 0)}
        ]
        res = self.client.mutate(_SET, {"input": inp}, "productSet")
        return res["product"]["id"]

    def publish_to_channels(self, product_id: str) -> None:
        if not self.publications:
            return
        self.client.mutate(
            _PUBLISH,
            {"id": product_id, "input": [{"publicationId": p} for p in self.publications]},
            "publishablePublish",
        )

    # -- images -------------------------------------------------------------
    def _fetch_photos(self, part: Part, dest: Path) -> list[Path]:
        """This part's own photographs, or its donor vehicle's when it has none.

        Which of the two applies was decided by the image resolver, over the same manifests
        the fingerprint was taken from — so what is published here is what the diff said had
        changed, rather than a second opinion formed at publish time.
        """
        if part.uses_donor_photos and part.donor_image_key:
            return self.images.fetch_vehicle(part.donor_image_key, dest)
        return self.images.fetch(part.image_key(), dest)

    def _staged_files(self, part: Part, alt_for) -> list[dict]:
        """Upload this part's photos to Shopify's staging area.

        Returns FileSetInput dicts ready to hand to productSet — each with alt text, since
        media created without it is invisible to image search. ``alt_for`` is the canonical
        renderer's alt text for photo *n*, so the alt a photo is created with and the alt
        the audit and repair paths expect are one string, not two.
        """
        if part.uses_donor_photos and part.donor_image_key and self.donor_files is not None:
            return self._donor_file_inputs(part, alt_for)

        with tempfile.TemporaryDirectory(prefix=f"coreyard-r{part.r_number}-") as tmp:
            paths = self._fetch_photos(part, Path(tmp))
            if not paths:
                if self.require_images:
                    raise RuntimeError(f"R#{part.r_number} is marked as having images, but none were found")
                return []

            urls = self._stage_and_put(paths, f"R#{part.r_number}")

            return [
                {
                    "originalSource": url,
                    "contentType": "IMAGE",
                    "alt": alt_for(i),
                    "filename": name,
                }
                for i, (url, name) in enumerate(urls, start=1)
            ]

    def _stage_and_put(self, paths: list[Path], label: str) -> list[tuple[str, str]]:
        """Upload local files to Shopify's staging area; returns (resourceUrl, filename)."""
        import requests

        mime_types = [mimetypes.guess_type(p.name)[0] or "image/jpeg" for p in paths]
        stage_in = [
            {"resource": "IMAGE", "filename": p.name, "mimeType": mime, "httpMethod": "POST"}
            for p, mime in zip(paths, mime_types)
        ]
        staged = self.client.graphql(_STAGE, {"input": stage_in})["stagedUploadsCreate"]
        if staged["userErrors"]:
            raise RuntimeError(f"staged upload {label}: {staged['userErrors']}")
        targets = staged["stagedTargets"]
        if len(targets) != len(paths):
            raise RuntimeError(
                f"staged upload {label}: expected {len(paths)} targets, got {len(targets)}"
            )
        urls: list[tuple[str, str]] = []
        session = requests.Session()
        for p, mime, target in zip(paths, mime_types, targets):
            form = [(x["name"], x["value"]) for x in target["parameters"]]
            last_error: Exception | None = None
            for attempt in range(4):
                try:
                    with p.open("rb") as image_file:
                        resp = session.post(target["url"], data=form,
                                            files={"file": (p.name, image_file, mime)},
                                            timeout=90)
                    resp.raise_for_status()
                    last_error = None
                    break
                except (requests.RequestException, OSError) as exc:
                    last_error = exc
                    if attempt < 3:
                        time.sleep(2 ** attempt)
            if last_error:
                raise RuntimeError(f"image upload {label} ({p.name}): {last_error}")
            urls.append((target["resourceUrl"], p.name))
        return urls

    def _donor_file_inputs(self, part: Part, alt_for) -> list[dict]:
        """Attach the donor vehicle's frames, uploading each one only the first time.

        About seven parts come off each donor, so staging its frames per product would send
        the same photograph seven times. ``FileSetInput`` takes an ``id``, so a frame is
        uploaded once, remembered against (donor, filename), and referenced by every part
        after it. The alt text describes the donor vehicle rather than the part, which is
        why one shared file carrying one alt is correct here and not a compromise.
        """
        key = str(part.donor_image_key or "")
        names = self.images.list_vehicle_images(key)[:donor_photo_limit()]
        if not names:
            return []
        known = {name: self.donor_files.donor_file_id(key, name) for name in names}
        missing = [name for name in names if not known[name]]
        if missing:
            with tempfile.TemporaryDirectory(prefix=f"coreyard-donor{key}-") as tmp:
                paths = self.images.fetch_vehicle(key, Path(tmp))
                wanted = [p for p in paths if p.name in set(missing)]
                if wanted:
                    staged = self._stage_and_put(wanted, f"donor {key}")
                    order = {name: i for i, name in enumerate(names)}
                    created = self.client.graphql(_FILE_CREATE, {"files": [
                        {"originalSource": url, "contentType": "IMAGE", "filename": name,
                         "alt": alt_for(order.get(name, 0) + 1)}
                        for url, name in staged
                    ]})["fileCreate"]
                    if created["userErrors"]:
                        raise RuntimeError(f"fileCreate donor {key}: {created['userErrors']}")
                    for (_url, name), created_file in zip(staged, created["files"]):
                        file_id = created_file.get("id") or ""
                        if file_id:
                            known[name] = file_id
                            self.donor_files.record_donor_file(key, name, file_id)
        # A frame whose upload produced no id is skipped rather than guessed at; the parts
        # that follow will try it again.
        return [{"id": known[name]} for name in names if known.get(name)]

    # -- orchestration ------------------------------------------------------
    def publish(self, part: Part, refresh_images: bool = False,
                revive_status: Optional[str] = None) -> tuple[str, int]:
        """Create/update one part; returns (product_id, images_added).

        Photos are staged before the upsert so productSet can attach them in the same call.
        A product that already has media keeps it, so re-runs never duplicate images.

        ``refresh_images`` is for the case the state diff says this part's photo set changed
        on the share: the current set is uploaded and attached, and the superseded media are
        deleted only once that has succeeded. It costs a full re-upload, so callers should
        pass it only on a detected change, never blanket.

        ``revive_status`` is the status this product held before it was retired. A part can
        come back — a voided work order returns it to the yard — and the status read-back
        that stops a sync from un-publishing a live product would otherwise re-send
        ARCHIVED, restoring the part's stock onto a product no shopper can see. It is
        applied only to an archived product, so it can never override a hand-set status.
        """
        rendered = render(part, part.images, self.store)
        product_id, media_ids, status, existing_tags, metafield_keys = self._find(
            rendered.handle)
        if revive_status and status == self.retire_status:
            status = revive_status
        alt_for = self._alt_text(part)

        # Stage the replacements before removing anything, and remove the old media only
        # once the write that attached the new set has come back clean.
        #
        # The old order deleted first. Everything after that point can fail — the share can
        # be unreachable, a staged upload can be rejected, an HTTP PUT can time out four
        # times, productSet can return userErrors — and each of those left a live product
        # with no photographs at all, which is worse than the stale photo the refresh was
        # called to correct.
        stale_media: list[str] = []
        if refresh_images and media_ids:
            files = self._staged_files(part, alt_for)
            if files:
                stale_media = media_ids
            # No replacement could be staged: keep what the product already has rather
            # than stripping it. The next run tries again.
        elif not media_ids:
            files = self._staged_files(part, alt_for)
        else:
            files = []

        product_id = self._upsert(rendered, part.quantity, product_id, files, status,
                                  existing_tags)
        if stale_media:
            self.client.mutate(_DELETE_FILES, {"ids": stale_media}, "fileDelete")
        # Only a product that is meant to be visible is put on a channel: publishing a DRAFT
        # would make the holding state for "not ready yet" mean nothing.
        if (status or self.status) == "ACTIVE":
            self.publish_to_channels(product_id)
        if metafield_keys:
            self._prune_metafields(product_id, metafield_keys, rendered)
        return product_id, len(files)

    def _alt_text(self, part: Part):
        from coreyard.transform import seo

        return lambda index: seo.image_alt(part, index, self.store)

    def published_r_numbers(self) -> set[str]:
        """Every R# currently on the store under this installation's handle prefix.

        Already-archived products are excluded: they are what retirement produces, and
        re-retiring them every run would be a pointless write per part per tick.

        The scan itself lives in :mod:`coreyard.reconcile.store` so the sync's ``--reconcile``
        and the standalone reconcile command read the store exactly the same way. Imported
        here rather than at module scope because reconciliation publishes through this class.
        """
        from coreyard.reconcile.store import published_r_numbers

        return published_r_numbers(self.client, self.store, self.retire_status)

    def retire(self, r_number: str, record_prior=None, skip_draft: bool = False) -> str:
        """Take a sold part off sale: zero its inventory, then set the retire status.

        Returns one of "retired", "already", "draft", or "absent". Inventory is zeroed
        *before* the status change so that a failure part-way through leaves the part
        unbuyable rather than visible with stock. Retiring is deliberately not a delete: the
        product, its photos and its URL stay put, so an existing link or search result lands
        on a real page instead of a 404, and the part can be revived if it comes back.

        ``record_prior`` is called with (R#, status) for a product that is actually being
        archived now, so a caller holding the sync state can put the part back the way it
        was if it returns. It is deliberately not called for a product that is already
        archived: re-retiring a part must not overwrite the remembered status with
        ARCHIVED and strand it.

        ``skip_draft`` is for callers retiring a part because it is *no longer listable*
        rather than because it sold. A DRAFT product is already invisible to shoppers — it
        is the holding state for "not ready", typically a part with no photos yet — so
        archiving it writes to the store, zeroes a stock figure that was correct, and
        destroys the difference between "not ready" and "gone". A sale is different: that
        is positive evidence, so the order pipeline leaves this off.
        """
        # Built through handle_for, not by hand: the slugging rules must not drift between
        # the code that publishes a handle and the code that goes looking for it.
        handle = handle_for(Part(r_number=str(r_number), part_type=""), self.store)
        found = self.client.graphql(_RETIRE_FIND, {"h": handle})["productByIdentifier"]
        if not found:
            return "absent"
        if skip_draft and found["status"] == "DRAFT":
            return "draft"

        # Recorded before anything is changed: a crash between here and the status update
        # leaves a part that is still sellable and merely has a note about it, which the
        # next revival check discards. Recording afterwards could lose the only copy of
        # the status the moment it stops being readable.
        if record_prior is not None and found["status"] != self.retire_status:
            record_prior(str(r_number), found["status"])

        variants = found["variants"]["nodes"]
        if variants and variants[0]["inventoryItem"].get("tracked"):
            res = self.client.graphql(_SET_QUANTITIES, {
                "idempotencyKey": str(uuid.uuid4()),
                "input": {
                    "name": "available",
                    "reason": "correction",
                    "quantities": [{
                        "inventoryItemId": variants[0]["inventoryItem"]["id"],
                        "locationId": self.location,
                        "quantity": 0,
                        # Admin API 2026-07 requires this field even when deliberately
                        # opting out of compare-and-swap. The yard database is the source
                        # of truth for availability, so a sold part must end at zero
                        # regardless of Shopify's previously cached quantity.
                        "changeFromQuantity": None,
                    }],
                },
            })["inventorySetQuantities"]
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
    parts = fetch_parts(limit=limit, images_only=True)  # a hand-run smoke test
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
