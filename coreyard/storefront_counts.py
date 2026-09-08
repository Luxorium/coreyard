"""Refresh the exact count of listed parts displayed by the storefront.

    coreyard counts                 # read the source database and report; writes nothing
    coreyard counts --apply         # update the Shopify shop metafield

This command never publishes, archives or edits a product. It reads one aggregate from the
source database and writes two shop-level metafields: the count and its observation time.

It counts what is **listable** — the same scope and photo policy `fetch_parts` and
`listable_r_numbers` apply — and not merely what is in stock. Those are different numbers
(26,729 against 26,920 here) because a part with no photograph is in the yard but not on
the storefront, and the figure the storefront prints has to be the one a shopper could
actually find. Reconcile keeps the listed set equal to the listable set, so this is the
live catalogue size.

Liquid can report `collections.all.products_count` without any of this, and that is what
the hero used to do. It is a denormalised counter Shopify updates lazily: it read 25,001
against a store holding 26,735 published products, understating the catalogue by 1,700
parts on the page whose whole job is to say how much there is.
"""

from __future__ import annotations

import argparse


def refresh(apply: bool = False) -> int:
    from coreyard.yms.inventory import source_inventory_count

    # images_only=True applies the site's photo policy, so this is the listable count —
    # what the storefront actually holds — rather than everything in stock.
    count = source_inventory_count(images_only=True)
    print(f"Listable inventory: {count:,} part(s)")
    if not apply:
        print("Dry run — storefront metafield not changed. Re-run with --apply to publish it.")
        return count

    from coreyard.sink.shopify_write import set_source_inventory_count

    set_source_inventory_count(count)
    print("Storefront listed-parts counter updated.")
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
