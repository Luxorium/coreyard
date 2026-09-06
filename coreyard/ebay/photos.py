"""Photo eligibility from the portal's configured no-image filter."""

import json

from coreyard.ebay.client import PortalError


def no_image_listings(client, *, tab="listed", rows=3000):
    """Read a complete set; a partial response cannot establish eligibility."""
    template = client.portal.filters.get("no_images")
    if not template:
        raise PortalError("portal filters.no_images is required for photo checks")
    query = json.loads(template)
    if not isinstance(query, dict) or not query:
        raise PortalError("portal filters.no_images must be a nonempty JSON object")
    result = list(client.iter_listings(tab=tab, rows=rows, search=query))
    ids = [str(row["listing_id"]) for row in result]
    if len(ids) != len(set(ids)):
        raise PortalError("no-image filter returned duplicate listings")
    return result
