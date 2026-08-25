"""``coreyard audit catalog`` — read-only listing-quality report.

    bin/coreyard audit catalog
    bin/coreyard audit catalog --show 20        # more offenders per finding
    bin/coreyard audit catalog --all-products   # include products CoreYard did not publish
    bin/coreyard audit catalog --json out/audit.json

Thresholds come from the site's profile (``STORE_PROFILE_FILE``), so what counts as a
suspiciously low price or a thin description is the store's decision, not this tool's.
Writes nothing to Shopify.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from coreyard.audit.catalog import evaluate, scan
from coreyard.config import load_store


def run(args) -> int:
    from coreyard.sink.shopify_api import ShopifyClient

    store = load_store()
    client = ShopifyClient()
    print("Scanning the catalogue ...", flush=True)
    products = scan(client, store, ours_only=not args.all_products)
    report = evaluate(products, store.catalog.audit)

    print(f"\n{report.total} product(s) audited\n")
    if report.clean:
        print("No issues found.")
        return 0

    print(f"{'finding':<32}{'count':>8}{'% of catalog':>14}")
    print("-" * 54)
    for name, items in report.ordered():
        share = len(items) * 100 / report.total if report.total else 0
        print(f"{name:<32}{len(items):>8}{share:>13.1f}%")

    for name, items in report.ordered():
        print(f"\n{name} — {len(items)} total, showing {min(args.show, len(items))}:")
        for finding in items[: args.show]:
            detail = f"  ({finding.detail})" if finding.detail else ""
            print(f"    {finding.label}{detail}")

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"total": report.total,
             "findings": {name: [f.label for f in items]
                          for name, items in report.ordered()}},
            indent=1), encoding="utf-8")
        print(f"\nFull report written to {path}")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard audit", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="what", required=True)
    c = sub.add_parser("catalog", help="listing-quality report")
    c.add_argument("--show", type=int, default=6, help="offenders to print per finding")
    c.add_argument("--all-products", action="store_true",
                   help="audit every product on the store, not only CoreYard's")
    c.add_argument("--json", default=None, help="also write the full report here")
    c.set_defaults(func=run)
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
