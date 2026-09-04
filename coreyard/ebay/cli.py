"""Pull and manage the eBay catalogue through the configured listing portal."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from coreyard.config import REPO_ROOT, _get, load_env
from coreyard.ebay.client import PortalClient
from coreyard.ebay.portal import load as load_portal
from coreyard.ebay import link
from coreyard.ebay.util import TITLE_MAX as EBAY_TITLE_MAX
from coreyard.ebay.util import groups_by_interchange, truthy
from coreyard.transform.pricing import money_str, parse_money

DEFAULT_LISTINGS = REPO_ROOT / "out" / "ebay-listings.json"
DEFAULT_CAP = 25
TABS = ("unlisted", "listed", "flagged", "sold")


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _listing_records(value: str) -> list[dict]:
    """Load a plan/grid JSON or a comma-separated explicit ID list."""
    path = Path(value)
    if path.is_file():
        loaded = _read_json(path)
        if not isinstance(loaded, list):
            raise SystemExit(f"{path} must contain a JSON array")
        records = []
        for item in loaded:
            if isinstance(item, (str, int)):
                records.append({"listing_id": str(item)})
            elif isinstance(item, dict) and item.get("listing_ids"):
                records.extend({**item, "listing_id": str(listing_id)}
                               for listing_id in item["listing_ids"])
            elif isinstance(item, dict) and (item.get("listing_id") or
                                             item.get("ListingDetailsID")):
                records.append({**item, "listing_id": str(
                    item.get("listing_id") or item["ListingDetailsID"]
                )})
            else:
                raise SystemExit(f"{path} contains an entry with no listing ID")
        return records
    return [{"listing_id": item.strip()} for item in value.split(",") if item.strip()]


def pull(args) -> int:
    """Download one portal tab to a neutral, inspectable JSON checkpoint."""
    load_env()
    portal = load_portal(args.portal)
    client = PortalClient(portal)
    listings = list(client.iter_listings(
        tab=args.tab, rows=args.rows, limit=args.limit, part_type=args.part_type
    ))
    output = Path(args.out)
    _write_json(output, listings)
    groups = {
        item.get("interchange_number") or f"_listing_{item['listing_id']}"
        for item in listings
    }
    narrowed = f", part type {args.part_type}" if args.part_type else ""
    print(f"Pulled {len(listings)} {args.tab} listings{narrowed} in "
          f"{len(groups)} interchange groups.")
    print(f"  -> {output}")
    return 0


def details(args) -> int:
    """Fetch normalized edit-form details, resuming an existing checkpoint."""
    load_env()
    listings = json.loads(Path(args.listings).read_text(encoding="utf-8"))
    output = Path(args.out)
    if output.is_file() and not args.no_resume:
        existing = json.loads(output.read_text(encoding="utf-8"))
    else:
        existing = {}
    wanted = {str(item["listing_id"]) for item in listings}
    existing = {key: value for key, value in existing.items() if key in wanted}
    todo = [key for key in sorted(wanted) if key not in existing]
    print(f"{len(wanted)} listings: {len(existing)} details cached, {len(todo)} to fetch")
    if not todo:
        return 0
    client = PortalClient(load_portal(args.portal))
    for number, listing_id in enumerate(todo, 1):
        existing[listing_id] = client.listing_detail(listing_id, tab=args.tab)
        if number % 10 == 0:
            _write_json(output, existing)
            print(f"  {number}/{len(todo)} (checkpointed)")
    _write_json(output, existing)
    print(f"  -> {output}")
    return 0


def apply_plans(args) -> int:
    """Dry-run or save a reviewed title plan in the portal."""
    from coreyard.ebay import workflow

    title_plan = _read_json(args.titles) if args.titles else []
    if not title_plan:
        raise SystemExit("pass --titles")
    client = None
    if args.apply:
        load_env()
        client = PortalClient(load_portal(args.portal))
        client.grid(tab=args.tab, rows=1)
    try:
        title_results = workflow.apply_titles(
            client, title_plan, tab=args.tab, dry_run=not args.apply,
            cap=args.cap, force=args.yes_i_mean_it,
        )
    except workflow.ApplyGuard as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    errors = [item for item in title_results
              if str(item["status"]).startswith("ERROR")]
    print(f"Titles: {sum(item['n'] for item in title_results)} listings.")
    print("Dry run: nothing written." if not args.apply else
          "Saved in the portal only; nothing pushed live to eBay.")
    return 1 if errors else 0


def marketplace_titles(args) -> int:
    """Title portal listings with the same renderer that titles the Shopify catalogue.

    The listings are joined to their parts by R#, so this reads the yard database. That is
    the point: the alternative is guessing at facts the database already holds.
    """
    from coreyard.config import load_store
    from coreyard.ebay import titles as titler

    from coreyard.ebay import link

    load_env()
    listings = _read_json(args.listings)
    details_by_id = _read_json(args.details) if Path(args.details).is_file() else {}
    if args.parts:
        from coreyard.yms.inventory import row_to_part
        catalogue = [row_to_part(item) for item in _read_json(args.parts)]
    else:
        from coreyard.yms.inventory import fetch_parts
        # Every part, not only the listable ones: the portal lists parts without photos,
        # and a listing whose part this refuses to identify is a listing left alone.
        catalogue = fetch_parts(images_only=False)
    resolved, unresolved = link.resolve(listings, catalogue)
    resolved, unresolved = link.merge_details(
        resolved, unresolved, details_by_id,
        {str(part.r_number).strip(): part for part in catalogue},
    )
    print(f"{len(listings)} listings; {len(resolved)} resolved to a yard part, "
          f"{len(unresolved)} not")
    if resolved and not args.parts:
        from coreyard.yms.db import connect
        from coreyard.yms.interchange import InterchangeResolver

        # Fitment is what turns a donor vehicle into the list of vehicles a part fits, and
        # the renderer titles from it. Resolving it here is not an embellishment: without
        # it this path would render a *different* title from the one the sync publishes,
        # which is the entire thing this command exists to avoid.
        with connect() as conn:
            InterchangeResolver(conn).attach(list(resolved.values()))
    accepted, held = titler.decisions_for(
        listings, resolved, load_store(), limit=args.limit,
    )
    held.extend({**item, "reason": item["reason"]} for item in unresolved)
    grouped = titler.group_for_portal(accepted)
    _write_json(Path(args.out), grouped)
    held_path = Path(args.out).with_name(Path(args.out).stem + "-held.json")
    _write_json(held_path, held)
    decisions_path = Path(args.out).with_name(Path(args.out).stem + "-decisions.json")
    _write_json(decisions_path, accepted)
    print(f"{len(accepted)} listings retitled in {len(grouped)} portal writes; "
          f"{len(held)} held.")
    for reason, number in Counter(item["reason"].split(" (")[0]
                                  for item in held).most_common():
        print(f"  held: {number:>3} {reason}")
    dropped = Counter(name for item in accepted for name in item.get("dropped") or ())
    for name, number in dropped.most_common():
        print(f"  budget dropped {name} on {number} listing(s)")
    print(f"  -> {args.out}\n  -> {decisions_path}\n  -> {held_path}")
    return 0


def aspects(args) -> int:
    """Derive and optionally save eBay item specifics for one live portal slice."""
    from coreyard.ebay import aspects as aspect_engine
    from coreyard.ebay import workflow

    load_env()
    portal = load_portal(args.portal)
    client = PortalClient(portal)
    filter_name, filter_value = portal.part_type_filter(args.part_type)
    data = client.grid(tab=args.tab, rows=args.rows,
                       **{filter_name: filter_value})
    rows = data["rows"]
    if len(rows) < data["records"]:
        print(f"REFUSED: grid returned {len(rows)} of {data['records']}; raise --rows.",
              file=sys.stderr)
        return 1
    details_by_id = {}
    detail_path = Path(args.details_out)
    if detail_path.is_file():
        details_by_id = _read_json(detail_path)
    if args.deep:
        todo = [row for row in rows if str(row["listing_id"]) not in details_by_id]
        for number, row in enumerate(todo, 1):
            listing_id = str(row["listing_id"])
            details_by_id[listing_id] = client.listing_detail(listing_id, tab=args.tab)
            if number % 10 == 0:
                _write_json(detail_path, details_by_id)
        _write_json(detail_path, details_by_id)
    metadata = aspect_engine.vocab(args.aspect_metadata, required=args.apply)
    coverage = aspect_engine.coverage(rows, details_by_id, metadata)
    for name, number in coverage.items():
        print(f"  {name:<26} {number:>4}/{len(rows)}")
    warranty = args.warranty
    if warranty is None:
        warranty = (_get("EBAY_DEFAULT_WARRANTY", "") or "").strip()
    try:
        results = workflow.apply_aspects(
            client, rows, details=details_by_id, warranty=warranty, tab=args.tab,
            metadata=metadata,
            dry_run=not args.apply, cap=args.cap, force=args.yes_i_mean_it,
        )
    except workflow.ApplyGuard as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    errors = [item for item in results if str(item["status"]).startswith("ERROR")]
    print(f"{len(results)} aspect sets across {sum(item['n'] for item in results)} listings.")
    print("Dry run: nothing written." if not args.apply else
          "Saved in the portal only; nothing pushed live to eBay.")
    return 1 if errors else 0


def _preflight(client, records: list[dict], *, tab: str, rows: int,
               part_type: str | None = None) -> list[str]:
    """Re-read the tab and confirm every target is still what the plan reviewed.

    ``part_type`` narrows the re-read. It is not an optimisation: a whole tab can be larger
    than the portal will serve, and a preflight that cannot see a listing reports it as
    gone. Narrowing to the scope the plan came from is what makes the check complete.
    """
    live = {str(item["listing_id"]): item
            for item in client.iter_listings(tab=tab, rows=rows, part_type=part_type)}
    errors = []
    for record in records:
        listing_id = str(record["listing_id"])
        row = live.get(listing_id)
        if row is None:
            errors.append(f"{listing_id}: no longer present on {tab}")
            continue
        valid = row.get("valid_for_submit")
        if valid not in (None, "") and not truthy(valid):
            errors.append(f"{listing_id}: portal reports invalid for submission")
        if record.get("title") and str(row.get("title") or "").strip() != str(
            record["title"]
        ).strip():
            errors.append(f"{listing_id}: portal title differs from reviewed plan")
        if record.get("price"):
            planned = parse_money(record["price"])
            current = parse_money(row.get("price"))
            if planned != current:
                errors.append(f"{listing_id}: portal price differs from reviewed plan")
    return errors


def push(args) -> int:
    """Preflight and explicitly submit reviewed listings to eBay."""
    from coreyard.ebay.workflow import check_cap, ApplyGuard

    records = _listing_records(args.listings)
    if not records:
        print("Nothing to submit.")
        return 0
    load_env()
    client = PortalClient(load_portal(args.portal))
    failures = _preflight(client, records, tab=args.tab, rows=args.rows,
                          part_type=args.part_type)
    if failures:
        print(f"REFUSED: {len(failures)} preflight failure(s):", file=sys.stderr)
        for failure in failures[:20]:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"Preflight passed for {len(records)} explicit listings on {args.tab}.")
    if not args.apply:
        print("Dry run: nothing submitted to eBay.")
        return 0
    try:
        check_cap(len(records), args.cap, args.yes_i_mean_it)
    except ApplyGuard as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    message = client.submit_listings(
        [item["listing_id"] for item in records], tab=args.tab,
        list_as_new=args.list_as_new,
    )
    print(message or f"Submitted {len(records)} listings to eBay.")
    return 0


def delist(args) -> int:
    """Resolve and optionally end explicit live eBay listings."""
    from coreyard.ebay.workflow import check_cap, ApplyGuard

    load_env()
    portal = load_portal(args.portal)
    client = PortalClient(portal)
    if args.part_type:
        filter_name, filter_value = portal.part_type_filter(args.part_type)
        data = client.grid(tab=args.tab, rows=args.rows,
                           **{filter_name: filter_value})
        if len(data["rows"]) < data["records"]:
            print("REFUSED: the whole filtered set is not visible; raise --rows.",
                  file=sys.stderr)
            return 1
        # Confirm the filter actually filtered. Ending live listings cannot be undone, so
        # this does not take the portal's word for it: one part type was asked for, and
        # anything else in the answer means the target set is not what was reviewed.
        leaked = {str(row.get("part_type")) for row in data["rows"]} - {str(args.part_type)}
        if leaked:
            print(f"REFUSED: the part-type filter leaked {sorted(leaked)}; the result is "
                  f"not the set you asked for.", file=sys.stderr)
            return 1
        records = [{"listing_id": str(row["listing_id"])} for row in data["rows"]]
    else:
        records = _listing_records(args.listings)
        client.grid(tab=args.tab, rows=1)
    if not records:
        print("Nothing to end.")
        return 0
    print(f"Would END {len(records)} live listings; this is irreversible on eBay.")
    if not args.apply:
        print("Dry run: nothing ended.")
        return 0
    try:
        check_cap(len(records), args.cap, args.yes_i_mean_it)
    except ApplyGuard as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    message = client.end_listings([item["listing_id"] for item in records], tab=args.tab)
    print(message or f"Ended {len(records)} listings.")
    return 0


def undo(args) -> int:
    """Dry-run or reset selected portal values to their source-system defaults."""
    from coreyard.ebay import workflow

    records = _listing_records(args.listings)
    listing_ids = [item["listing_id"] for item in records]
    if not args.apply:
        print(f"Dry run: would reset {args.what} for {len(listing_ids)} listings.")
        return 0
    try:
        workflow.check_cap(len(listing_ids), args.cap, args.yes_i_mean_it)
    except workflow.ApplyGuard as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    load_env()
    client = PortalClient(load_portal(args.portal))
    client.grid(tab=args.tab, rows=1)
    if args.what in {"titles", "both"}:
        print(workflow.reset_titles(client, listing_ids, tab=args.tab) or "Titles reset.")
    if args.what in {"prices", "both"}:
        print(workflow.reset_prices(client, listing_ids, tab=args.tab) or "Prices reset.")
    return 0


def daily(args) -> int:
    """Read the tab and offer canonical titles plus exact source prices.

    Walks part types rather than the tab as a whole, because the portal's grid will not
    serve a whole tab (see ``PortalClient.iter_listings``) but answers a filtered read
    completely. Which types to walk comes from the yard, not from a hand-kept list, so a
    type a worker files a part under tomorrow is walked tomorrow.
    """
    from coreyard.config import load_store
    from coreyard.ebay import daily as driver
    from coreyard.ebay import workflow
    from coreyard.yms.db import connect
    from coreyard.yms.interchange import InterchangeResolver
    from coreyard.yms.inventory import fetch_parts

    load_env()
    store = load_store()
    portal = load_portal(args.portal)
    client = PortalClient(portal)

    # Every part, not only the listable ones: the portal lists parts CoreYard would not
    # publish to Shopify, and a listing whose part cannot be identified is left alone.
    catalogue = fetch_parts(images_only=False)
    print(f"{len(catalogue)} yard parts")

    if args.part_type:
        codes = [args.part_type]
    else:
        codes = sorted({str(part.part_type_code) for part in catalogue
                        if part.part_type_code is not None},
                       key=lambda code: (len(code), code))
    print(f"walking {len(codes)} part type(s) of the {args.tab} tab")

    rows: list[dict] = []
    for number, code in enumerate(codes, 1):
        try:
            found = list(client.iter_listings(tab=args.tab, rows=args.rows,
                                              part_type=code))
        except Exception as exc:
            # One part type the portal refuses must not abandon the types after it.
            print(f"  part type {code}: {type(exc).__name__}: {str(exc)[:120]}",
                  file=sys.stderr)
            continue
        rows.extend(found)
        if found:
            print(f"  [{number}/{len(codes)}] part type {code}: {len(found)} listing(s)")
        if args.limit and len(rows) >= args.limit:
            rows = rows[:args.limit]
            print(f"  stopping at --limit {args.limit}")
            break
    if not rows:
        print(f"Nothing in the {args.tab} tab.")
        return 0

    details_path = Path(args.details)
    details = _read_json(details_path) if details_path.is_file() else {}
    resolved, _ = link.resolve(rows, catalogue)
    resolved, _ = link.merge_details(
        resolved, [], details, {str(p.r_number).strip(): p for p in catalogue}
    )
    if resolved:
        # Fitment is what the renderer titles from; without it this would write a
        # different title from the one the Shopify sync publishes for the same part.
        with connect() as conn:
            InterchangeResolver(conn).attach(list(resolved.values()))

    result = driver.plan(
        rows, catalogue, store, details=details, title_limit=args.title_limit,
    )
    print(result.summary())

    title_path = Path(args.out_prefix + "-titles.json")
    price_path = Path(args.out_prefix + "-prices.json")
    held_path = Path(args.out_prefix + "-held.json")
    _write_json(title_path, result.titles)
    _write_json(price_path, result.prices)
    _write_json(held_path, result.held)
    print(f"  -> {title_path}\n  -> {price_path}\n  -> {held_path}")

    if not (result.titles or result.prices):
        return 0
    dry_run = not args.apply
    try:
        if result.titles:
            for item in workflow.apply_titles(
                client, result.titles, tab=args.tab, dry_run=dry_run, cap=args.cap,
                force=args.yes_i_mean_it,
            ):
                print(f"  titles {item['n']:>4} x {item['status']:<8} {item['new'][:60]}")
        if result.prices:
            for item in workflow.apply_prices(
                client, result.prices, tab=args.tab, dry_run=dry_run, cap=args.cap,
                force=args.yes_i_mean_it,
            ):
                print(f"  price  {item['n']:>4} x {item['status']:<8} {item['price']}")
    except workflow.ApplyGuard as exc:
        # The cap refuses before the first write, so nothing is half-applied. In a
        # scheduled run this is the expected outcome of a batch larger than anyone
        # reviewed, and it has to read as a refusal rather than a crash: the plan files
        # above are still on disk, and the run that follows will make the same offer.
        print(f"Refused: {exc}", file=sys.stderr)
        print(f"The plan is on disk ({args.out_prefix}-*.json); nothing was written.",
              file=sys.stderr)
        return 1
    if args.apply and resolved:
        # The renderer consumes reviewed titles from this file. Price is deliberately not
        # represented here; both channels read it from the source database.
        overrides_path = Path(args.overrides_out)
        prior = _read_json(overrides_path) if overrides_path.is_file() else {}
        _write_json(overrides_path, driver.overrides_for(result, resolved, prior))
        print(f"  -> {overrides_path}  (set STORE_CATALOG_OVERRIDES_FILE to this)")
    print("Saved in the portal only." if args.apply else
          "Dry run: nothing was written. Pass --apply to save these in the portal.")
    print("Nothing reaches eBay until `coreyard ebay push --apply`.")
    return 0


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    sub = ap.add_subparsers(dest="ebay_action", required=True)
    command = sub.add_parser(
        "daily", help="offer canonical titles and source prices to the portal")
    command.add_argument("--tab", choices=TABS, default="unlisted")
    command.add_argument("--part-type", help="walk only this part type")
    command.add_argument("--rows", type=int, default=1000, help="portal page size")
    command.add_argument("--limit", type=int, help="stop after this many listings")
    command.add_argument("--title-limit", type=int, default=EBAY_TITLE_MAX)
    command.add_argument("--details",
                         default=str(REPO_ROOT / "out" / "ebay-details.json"),
                         help="edit-form cache, read if present and never fetched")
    command.add_argument("--out-prefix", default=str(REPO_ROOT / "out" / "ebay-daily"))
    command.add_argument("--overrides-out",
                         default=str(REPO_ROOT / "out" / "catalog-overrides.json"))
    command.add_argument("--portal")
    command.add_argument("--apply", action="store_true",
                         help="save in the portal; omit for a dry run. Never pushes.")
    command.add_argument("--cap", type=int, default=DEFAULT_CAP)
    command.add_argument("--yes-i-mean-it", action="store_true")
    command.set_defaults(func=daily)

    command = sub.add_parser("pull", help="download a portal tab (read-only)")
    command.add_argument("--tab", choices=TABS, default="unlisted")
    command.add_argument("--part-type", help="limit to one configured portal part type")
    command.add_argument("--rows", type=int, default=1000, help="portal page size")
    command.add_argument("--limit", type=int, help="stop after this many listings")
    command.add_argument("--portal", help="portal map JSON (default: EBAY_PORTAL_FILE)")
    command.add_argument("--out", default=str(DEFAULT_LISTINGS), help="output JSON path")
    command.set_defaults(func=pull)

    command = sub.add_parser("details", help="cache normalized listing edit forms")
    command.add_argument("listings", help="JSON produced by `coreyard ebay pull`")
    command.add_argument("--tab", choices=TABS, default="unlisted")
    command.add_argument("--portal", help="portal map JSON (default: EBAY_PORTAL_FILE)")
    command.add_argument("--no-resume", action="store_true")
    command.add_argument("--out", default=str(REPO_ROOT / "out" / "ebay-details.json"))
    command.set_defaults(func=details)


    command = sub.add_parser("apply", help="save a reviewed title plan in the portal")
    command.add_argument("--titles")
    command.add_argument("--tab", choices=TABS, default="unlisted")
    command.add_argument("--portal")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--cap", type=int, default=DEFAULT_CAP)
    command.add_argument("--yes-i-mean-it", action="store_true")
    command.set_defaults(func=apply_plans)

    # One title command for every part type. "engine-titles" is the same command under the
    # name the engine work used before there was only one renderer behind both storefronts.
    for name, help_text in (
        ("titles", "title portal listings with the Shopify renderer"),
        ("engine-titles", "alias of `titles`, kept for existing engine scripts"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("listings", nargs="?",
                             default=str(REPO_ROOT / "out" / "ebay-engines.json"),
                             help="normalized listing checkpoint from `ebay pull`")
        command.add_argument("--details",
                             default=str(REPO_ROOT / "out" / "ebay-engine-details.json"),
                             help="edit-form cache; supplies the R# each title is keyed to")
        command.add_argument("--parts",
                             help="JSON of yard rows instead of reading the database")
        command.add_argument("--limit", type=int, default=EBAY_TITLE_MAX,
                             help="marketplace title budget, measured after escaping")
        command.add_argument("--out",
                             default=str(REPO_ROOT / "out" / "ebay-engine-titles.json"))
        command.set_defaults(func=marketplace_titles)

    command = sub.add_parser("aspects", help="derive and save eBay item specifics")
    command.add_argument("--part-type", required=True)
    command.add_argument("--tab", choices=TABS, default="unlisted")
    command.add_argument("--rows", type=int, default=1000)
    command.add_argument("--deep", action="store_true")
    command.add_argument("--details-out",
                         default=str(REPO_ROOT / "out" / "ebay-details.json"))
    command.add_argument("--aspect-metadata")
    command.add_argument("--warranty")
    command.add_argument("--portal")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--cap", type=int, default=DEFAULT_CAP)
    command.add_argument("--yes-i-mean-it", action="store_true")
    command.set_defaults(func=aspects)

    command = sub.add_parser("push", help="preflight and submit explicit listings to eBay")
    command.add_argument("listings", help="ready-plan JSON or comma-separated IDs")
    command.add_argument("--tab", choices=TABS, default="unlisted")
    command.add_argument("--part-type",
                         help="narrow the preflight re-read to one part type; needed "
                              "whenever the tab is larger than the portal will serve")
    command.add_argument("--rows", type=int, default=1000)
    command.add_argument("--portal")
    command.add_argument("--list-as-new", action="store_true")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--cap", type=int, default=DEFAULT_CAP)
    command.add_argument("--yes-i-mean-it", action="store_true")
    command.set_defaults(func=push)

    command = sub.add_parser("delist", help="end explicit live eBay listings")
    command.add_argument("listings", nargs="?", default="")
    command.add_argument("--part-type")
    command.add_argument("--tab", choices=TABS, default="listed")
    command.add_argument("--rows", type=int, default=1000)
    command.add_argument("--portal")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--cap", type=int, default=DEFAULT_CAP)
    command.add_argument("--yes-i-mean-it", action="store_true")
    command.set_defaults(func=delist)

    command = sub.add_parser("undo", help="reset portal titles/prices to source defaults")
    command.add_argument("listings", help="plan JSON or comma-separated IDs")
    command.add_argument("--what", choices=("titles", "prices", "both"), default="both")
    command.add_argument("--tab", choices=TABS, default="unlisted")
    command.add_argument("--portal")
    command.add_argument("--apply", action="store_true")
    command.add_argument("--cap", type=int, default=DEFAULT_CAP)
    command.add_argument("--yes-i-mean-it", action="store_true")
    command.set_defaults(func=undo)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard ebay", description=__doc__)
    add_arguments(ap)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
