"""``coreyard links`` — put each part's storefront address on the part, in the yard system.

    bin/coreyard links                  # plan only; writes nothing
    bin/coreyard links --apply          # write the links into the yard
    bin/coreyard links --apply --limit 25        # a cautious first pass
    bin/coreyard links --apply --r-number 44004  # one part

The pipeline publishes a part and then forgets to tell the yard where it put it. Everybody
who works from the yard system — the counter, the phones, whoever is asked "do you have a
picture of it" — has the part open and no way to reach its listing except to search the
store by hand for a title they would have to guess.

So this walks the store, works out each published part's address, and writes it into the
part's own e-commerce description field, where the yard system already shows it beside the
part. It is a reconciliation, not a one-off: run it again and it stamps what is newly
listed, leaves what is already right, and clears the address from parts whose listing has
gone, so a salesman is never sent to a page that 404s.

What it will not do is overwrite a description somebody at the yard typed. Those rows are
reported as skipped and left alone; a backlink is not worth a person's own words.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

#: Shopify's storefront path for a product. A convention of the platform, not of any yard
#: system, so unlike a table name it belongs here rather than in ``schema.json``.
PRODUCT_PATH = "/products/"

_SCAN = """query($cursor:String,$first:Int!){
  products(first:$first, after:$cursor){
    pageInfo{ hasNextPage endCursor }
    nodes{ id handle status onlineStoreUrl }
  }
}"""


@dataclass
class Plan:
    """What a run would do, and what it is deliberately leaving alone."""

    stamp: list[tuple[str, str]] = field(default_factory=list)   # (R#, address)
    clear: list[str] = field(default_factory=list)
    correct: int = 0
    #: Parts whose column holds something a person wrote. Never touched.
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return len(self.stamp) + len(self.clear)


def address_base(client=None) -> str:
    """The storefront's own address, without a trailing slash.

    Asked of the store rather than configured, because the answer is already authoritative
    there and a link built from a stale setting is a link that does not work. A site that
    fronts its storefront with something else can still say so with
    ``STORE_STOREFRONT_URL``.
    """
    from coreyard.config import _get, load_env

    load_env()
    configured = (_get("STORE_STOREFRONT_URL", "") or "").strip()
    if configured:
        return configured.rstrip("/")

    if client is None:
        from coreyard.sink.shopify_api import ShopifyClient

        client = ShopifyClient()
    shop = client.graphql("{ shop { primaryDomain { url } } }")["shop"]
    url = ((shop or {}).get("primaryDomain") or {}).get("url") or ""
    if not url:
        raise RuntimeError(
            "Shopify did not report a primary domain for this store, so there is no "
            "address to write. Set STORE_STOREFRONT_URL to the storefront's address."
        )
    return str(url).rstrip("/")


def ours(base: str, handle_prefix: str) -> str:
    """The address prefix every link this installation writes begins with.

    Both the guard on what may be overwritten and the test for "already correct", so it is
    derived once here. It includes the handle prefix, which makes it as narrow as it can
    be: a link to some other product on the same store is somebody else's and is left alone.
    """
    return f"{base}{PRODUCT_PATH}{handle_prefix}-"


def listed(client, store) -> dict[str, str]:
    """``{R#: address}`` for every part a shopper can actually open on the store.

    ``onlineStoreUrl`` is null exactly when the product is not reachable — archived, or on
    no channel that publishes it — which is the same question as "is this link worth
    writing", already answered by the store. Deriving the address from the handle instead
    would produce a link for a product that 404s.
    """
    from coreyard.transform.render import r_number_from_handle

    found: dict[str, str] = {}
    for node in client.paginate(_SCAN, "products", page_size=250):
        r_number = r_number_from_handle(node["handle"], store)
        if not r_number:
            continue
        url = node.get("onlineStoreUrl") or ""
        if url and node.get("status") == "ACTIVE":
            found[r_number] = str(url).rstrip("/")
    return found


def plan(live: dict[str, str], held: dict[str, str], prefix: str,
         only: "set[str] | None" = None) -> Plan:
    """Diff what the store publishes against what the yard holds.

    ``held`` is every part whose column holds anything at all, so the three outcomes are
    decided here and not in SQL: ours and right, ours and wrong (or gone), or somebody
    else's and therefore untouchable.
    """
    result = Plan()
    for r_number, url in sorted(live.items()):
        if only is not None and r_number not in only:
            continue
        existing = held.get(r_number, "")
        if existing and not existing.startswith(prefix):
            result.skipped.append((r_number, existing))
        elif existing == url:
            result.correct += 1
        else:
            result.stamp.append((r_number, url))

    for r_number, existing in sorted(held.items()):
        if only is not None and r_number not in only:
            continue
        if r_number not in live and existing.startswith(prefix):
            result.clear.append(r_number)
    return result


def _sample(label: str, items) -> None:
    if items:
        shown = [i if isinstance(i, str) else i[0] for i in items[:8]]
        print(f"    {label} sample: {shown}")


def run(args) -> int:
    from coreyard.config import load_store
    from coreyard.sink.shopify_api import ShopifyClient
    from coreyard.yms import backlinks as writer
    from coreyard.yms.db import connect
    from coreyard.yms.inventory import is_configured

    if not is_configured():
        print("No schema mapping yet — see README 'Map your database'.", file=sys.stderr)
        return 2
    try:
        write = writer.load()
    except writer.BacklinkWriteError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    store = load_store()
    client = ShopifyClient()
    base = address_base(client)
    prefix = ours(base, store.handle_prefix)
    print(f"Storefront addresses look like {prefix}<R#>")

    print("Asking Shopify what a shopper can open ...")
    live = listed(client, store)
    print(f"  {len(live)} published product(s) under handle prefix {store.handle_prefix!r}")

    print("Asking the yard what it already holds ...")
    with connect() as conn:
        held = writer.current(conn, write)
    print(f"  {len(held)} part(s) with an e-commerce description of any kind")

    only = set(args.r_number) if args.r_number else None
    actions = plan(live, held, prefix, only=only)
    if args.limit and len(actions.stamp) > args.limit:
        print(f"  (--limit {args.limit}: stamping the first {args.limit} of "
              f"{len(actions.stamp)})")
        actions.stamp = actions.stamp[:args.limit]

    print()
    print(f"  stamp   (no link, or the wrong one)  {len(actions.stamp)}")
    print(f"  clear   (listing has gone)           {len(actions.clear)}")
    print(f"  correct (already right)              {actions.correct}")
    print(f"  skipped (somebody's own wording)     {len(actions.skipped)}")
    _sample("stamp", actions.stamp)
    _sample("clear", actions.clear)
    _sample("skipped", actions.skipped)

    if not actions.changes:
        print("\nThe yard already agrees with the store. Nothing to write.")
        return 0
    if not args.apply:
        print(f"\nDry run — the yard was not changed. Re-run with --apply to write "
              f"{actions.changes} link(s).")
        return 0

    try:
        result = writer.apply(actions.stamp, actions.clear, prefix,
                              dry_run=args.show_sql)
    except writer.BacklinkWriteError as exc:
        print(f"\nRefused: {exc}", file=sys.stderr)
        return 1
    if args.show_sql:
        return 0
    print(f"\nWrote {result.stamped} link(s); cleared {result.cleared}.")
    return 0


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    ap.add_argument("--apply", action="store_true",
                    help="write the links into the yard's own database")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="stamp at most N parts this run (0 = no limit)")
    ap.add_argument("--r-number", action="append", default=[], metavar="R",
                    help="act on this part only; repeatable")
    ap.add_argument("--show-sql", action="store_true",
                    help="with --apply, print the batches instead of running them")
    ap.set_defaults(func=run)
    return ap


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard links",
                                 description=__doc__.splitlines()[0])
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
