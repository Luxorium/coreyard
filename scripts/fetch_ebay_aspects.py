#!/usr/bin/env python3
"""Fetch eBay's item-aspect vocabulary for the categories this yard lists into.

``coreyard ebay aspects`` refuses to write without this file, deliberately: eBay ranks on
aspect *match* and buyers filter on it, so a wrong aspect is worse than a missing one. The
vocabulary is what lets :func:`coreyard.ebay.aspects.canonical` snap a derived value to
eBay's own spelling and drop anything eBay would not recognise.

This is a live call to eBay's Taxonomy API, so it lives here rather than in the offline
package. It needs a developer keyset (``EBAY_APP_ID``/``EBAY_CERT_ID``), which is free and
unrelated to any listing-portal or partner arrangement.

The category ids come from the portal's own category plan rather than being embedded here,
for the same reason routes and field ids live in ``portal.json``: they are installation data.

Responses are cached per category, so a re-run after adding a category costs one request
rather than eighty-one.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coreyard.config import REPO_ROOT, load_env, _get  # noqa: E402

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
TAXONOMY = "https://api.ebay.com/commerce/taxonomy/v1"
SCOPE = "https://api.ebay.com/oauth/api_scope"


class AspectFetchError(RuntimeError):
    """The vocabulary could not be fetched; nothing is written."""


def _post_form(url: str, data: dict, headers: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def _get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def application_token(app_id: str, cert_id: str) -> str:
    """A client-credentials token. The secret is never logged or echoed."""
    basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    try:
        payload = _post_form(TOKEN_URL, {"grant_type": "client_credentials", "scope": SCOPE},
                             {"Authorization": f"Basic {basic}",
                              "Content-Type": "application/x-www-form-urlencoded"})
    except Exception as exc:                       # noqa: BLE001 - message must not carry the secret
        raise AspectFetchError(
            f"could not obtain an eBay application token ({type(exc).__name__}); "
            "check EBAY_APP_ID and EBAY_CERT_ID"
        ) from None
    token = payload.get("access_token")
    if not token:
        raise AspectFetchError("eBay returned no access_token")
    return token


def tree_id(token: str, marketplace: str) -> str:
    """Ask eBay for the tree id rather than hardcoding it; it is marketplace-specific."""
    url = f"{TAXONOMY}/get_default_category_tree_id?marketplace_id={marketplace}"
    found = _get_json(url, token).get("categoryTreeId")
    if not found:
        raise AspectFetchError(f"no category tree id for marketplace {marketplace}")
    return str(found)


def category_ids(path: Path) -> list[str]:
    """The distinct categories the yard actually lists into."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("categories") or []
    found = {str(row.get("suggested") or row.get("category") or "").strip()
             for row in rows if isinstance(row, dict)}
    return sorted(x for x in found if x.isdigit())


def merge(into: dict, aspects: list[dict]) -> None:
    """Union one category's aspects into the flat vocabulary the renderer reads.

    Values union across categories; ``SELECTION_ONLY`` wins over free text if *any* category
    constrains the aspect. That is the conservative pairing: a derived value still passes
    when eBay recognises it somewhere in the categories this yard uses, and is dropped when
    eBay recognises it nowhere — which is the outcome the channel wants for a value it
    cannot stand behind.
    """
    for aspect in aspects:
        name = (aspect.get("localizedAspectName") or "").strip()
        if not name:
            continue
        constraint = aspect.get("aspectConstraint") or {}
        entry = into.setdefault(name, {"values": [], "mode": "FREE_TEXT"})
        seen = {v.lower() for v in entry["values"]}
        for value in aspect.get("aspectValues") or ():
            text = (value.get("localizedValue") or "").strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                entry["values"].append(text)
        if constraint.get("aspectMode") == "SELECTION_ONLY":
            entry["mode"] = "SELECTION_ONLY"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--categories", default=str(REPO_ROOT / "out" / "ebay-category-suggestions.json"),
                    help="category plan to read ids from")
    ap.add_argument("--category", action="append", default=[],
                    help="fetch only this category id (repeatable)")
    ap.add_argument("--marketplace", default="EBAY_MOTORS")
    ap.add_argument("--out", default=str(REPO_ROOT / "out" / "ebay-item-aspects.json"))
    ap.add_argument("--cache", default=str(REPO_ROOT / "out" / "ebay-aspect-pages"))
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    args = ap.parse_args(argv)

    load_env()
    app_id, cert_id = _get("EBAY_APP_ID", ""), _get("EBAY_CERT_ID", "")
    if not (app_id and cert_id):
        print("Set EBAY_APP_ID and EBAY_CERT_ID (a free keyset from developer.ebay.com).",
              file=sys.stderr)
        return 2

    wanted = args.category or category_ids(Path(args.categories))
    if not wanted:
        print(f"No category ids in {args.categories}", file=sys.stderr)
        return 2

    token = application_token(app_id, cert_id)
    tree = tree_id(token, args.marketplace)
    print(f"marketplace {args.marketplace}, category tree {tree}, {len(wanted)} category(s)")

    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    vocabulary: dict[str, dict] = {}
    failed: list[str] = []
    for number, category in enumerate(wanted, 1):
        page = cache / f"{category}.json"
        if page.is_file() and not args.refresh:
            payload = json.loads(page.read_text(encoding="utf-8"))
        else:
            url = (f"{TAXONOMY}/category_tree/{tree}"
                   f"/get_item_aspects_for_category?category_id={category}")
            try:
                payload = _get_json(url, token)
            except Exception as exc:              # noqa: BLE001
                # One refused category must not abandon the categories after it.
                print(f"  category {category}: {type(exc).__name__}", file=sys.stderr)
                failed.append(category)
                continue
            page.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            if args.delay:
                time.sleep(args.delay)
        merge(vocabulary, payload.get("aspects") or [])
        if number % 10 == 0 or number == len(wanted):
            print(f"  {number}/{len(wanted)} categories, {len(vocabulary)} aspect(s)")

    if not vocabulary:
        raise AspectFetchError("no aspects fetched; nothing written")
    Path(args.out).write_text(json.dumps({
        "marketplace": args.marketplace,
        "category_tree_id": tree,
        "categories": wanted,
        "aspects": dict(sorted(vocabulary.items())),
    }, indent=1), encoding="utf-8")
    constrained = sum(1 for v in vocabulary.values() if v["mode"] == "SELECTION_ONLY")
    print(f"-> {args.out}\n   {len(vocabulary)} aspect(s), {constrained} selection-only"
          + (f", {len(failed)} category(s) failed" if failed else ""))
    print("   set EBAY_ASPECTS_FILE to this path")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AspectFetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
