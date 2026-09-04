"""Refresh the exact source-inventory count displayed by the storefront.

    coreyard counts                 # read the source database and report; writes nothing
    coreyard counts --apply         # update the Shopify shop metafield

This command never publishes, archives or edits a product. It reads one aggregate from the
source database and writes two shop-level metafields: the count and its observation time.
Shopify Liquid supplies the separate listed-online count directly on each page render.
"""

from __future__ import annotations

import argparse


def refresh(apply: bool = False) -> int:
    from coreyard.yms.inventory import source_inventory_count

    count = source_inventory_count(images_only=False)
    print(f"Source in-stock inventory: {count:,} part(s)")
    if not apply:
        print("Dry run — storefront metafield not changed. Re-run with --apply to publish it.")
        return count

    from coreyard.sink.shopify_write import set_source_inventory_count

    set_source_inventory_count(count)
    print("Storefront inventory counter updated.")
    return count


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    ap.add_argument("--apply", action="store_true",
                    help="write the observed count and timestamp to Shopify")
    ap.set_defaults(func=run)
    return ap


def run(args) -> int:
    refresh(apply=args.apply)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard counts",
                                 description=__doc__.splitlines()[0])
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
