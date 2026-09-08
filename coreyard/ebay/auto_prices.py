"""Bounded price saves and explicit revisions of already-listed marketplace items."""

from __future__ import annotations

import json
from pathlib import Path

from coreyard.config import load_env, load_store, out_dir
from coreyard.ebay import link, workflow
from coreyard.ebay.client import PortalClient
from coreyard.ebay.portal import load as load_portal
from coreyard.ebay.pricing import PricePolicy, quote
from coreyard.transform.pricing import money_str, parse_money


def filter_query(template, **values):
    """Substitute values into installation-owned structured portal filters."""
    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
            return values[value[1:-1]]
        return value
    return replace(json.loads(template))


def collect(client, tab, code, freight):
    """Determine shipping from filtered grids, avoiding per-listing form requests."""
    found = {}
    filters = [("pickup", "shipping_pickup", ""), ("free", "shipping_free", "")]
    filters += [("paid", "shipping_policy", value) for value in set(freight.values())]
    for mode, name, policy_id in filters:
        query = filter_query(client.portal.filters[name], part_type=int(code),
                             policy_id=str(policy_id))
        rows = list(client.iter_listings(tab=tab, rows=3000, search=query))
        if any(str(row.get("part_type")) != str(code) for row in rows):
            raise RuntimeError("shipping filter returned a different part type")
        for row in rows:
            key = str(row["listing_id"])
            if key in found:
                raise RuntimeError("shipping filters overlap; refusing ambiguous prices")
            found[key] = {**row, "shipping_mode": mode, "shipping_policy": policy_id}
    return list(found.values())


def plan(rows, catalogue, store, policy, freight):
    resolved, held = link.resolve(rows, catalogue)
    ready = []
    for row in rows:
        key = str(row["listing_id"])
        if key not in resolved:
            continue
        part = resolved[key]
        try:
            desired = quote(part, store, row.get("shipping_mode"), policy, freight)
        except ValueError as exc:
            held.append({"listing_id": key, "reason": str(exc)})
            continue
        current = parse_money(row.get("price"))
        old = money_str(current) if current is not None else ""
        policy_changed = (desired["shipping_policy"] and
                          desired["shipping_policy"] != row.get("shipping_policy"))
        if old != desired["new_price"] or policy_changed:
            ready.append({**desired, "listing_id": key, "r_number": part.uid(),
                          "old_price": old, "title": row.get("title", ""),
                          "part_type": str(row["part_type"])})
    return ready, held


def run(args):
    from coreyard.ebay.cli import _read_json, _write_json, push
    from coreyard.yms.inventory import fetch_parts
    from types import SimpleNamespace

    if args.batch_size < 1 or args.types_per_run < 1:
        raise ValueError("batch size and types per run must be positive")
    workflow.check_cap(args.batch_size, args.cap, False)
    load_env()
    policy, store = PricePolicy.configured(), load_store()
    client = PortalClient(load_portal(args.portal))
    freight = json.loads(client.portal.filters["freight_policy_by_group"])
    catalogue = fetch_parts(images_only=False)
    codes = sorted({str(part.part_type_code) for part in catalogue
                    if part.part_type_code is not None}, key=int)
    state_path = Path(args.state)
    state = _read_json(state_path) if state_path.exists() else {"cursor": 0, "pending": {}}
    work = [(tab, code) for code in codes for tab in ("listed", "unlisted")]
    if args.part_type:
        work = [(tab, args.part_type) for tab in ("listed", "unlisted")]
    start = state["cursor"] % len(work) if work else 0
    chosen = [work[(start + n) % len(work)] for n in range(min(args.types_per_run, len(work)))]
    remaining, saved, submitted, walked = args.batch_size, 0, 0, 0
    touched = set()
    report = {"saved": [], "held": [], "submitted": [], "errors": 0}
    for tab, code in chosen:
        walked += 1
        rows = collect(client, tab, code, freight)
        ready, held = plan(rows, catalogue, store, policy, freight)
        report["held"].extend(held)
        ready = ready[:remaining]
        print(f"{tab} type {code}: {len(rows)} listings; {len(ready)} price/policy changes", flush=True)
        if args.apply and ready:
            # Re-read membership, policy and price before acting on an explicit selection.
            fresh = {row["listing_id"]: row for row in collect(client, tab, code, freight)}
            original = {row["listing_id"]: row for row in rows}
            ready = [item for item in ready if fresh.get(item["listing_id"]) == original[item["listing_id"]]]
            workflow.check_cap(len(ready), remaining, False)
            groups = {}
            for item in ready:
                if tab == "listed":
                    # Persist revision intent first, so interrupted saves are recoverable.
                    state["pending"][item["listing_id"]] = {**item, "price": item["new_price"]}
                groups.setdefault((item["new_price"], item["shipping_policy"]), []).append(item["listing_id"])
            if tab == "listed":
                _write_json(state_path, state)
            for (price, shipping_policy), ids in groups.items():
                changes = [{"field": "fixed_price", "action": "change_to", "value": price}]
                if shipping_policy:
                    changes.append({"field": "shipping_policy", "action": "change_to", "value": shipping_policy})
                error = client.bulk_update(changes, ids, tab=tab)
                if error:
                    report["errors"] += len(ids)
                    report["held"].append({"listing_ids": ids, "reason": error})
            after = {row["listing_id"]: row for row in collect(client, tab, code, freight)}
            for item in ready:
                row = after.get(item["listing_id"])
                if (row and money_str(parse_money(row["price"])) == item["new_price"]
                        and (not item["shipping_policy"] or row["shipping_policy"] == item["shipping_policy"])):
                    report["saved"].append(item)
                    saved += 1
                else:
                    report["errors"] += 1
                    report["held"].append({"listing_id": item["listing_id"], "reason": "price/policy read-back failed"})
            remaining -= len(ready)
            touched.update((tab, item["listing_id"]) for item in ready)
        elif not args.apply:
            report["saved"].extend(ready)
            remaining -= len(ready)
        if args.apply and args.revise_listed and tab == "listed":
            live_rows = collect(client, tab, code, freight)
            source_parts, _ = link.resolve(live_rows, catalogue)
            live_by_id = {row["listing_id"]: row for row in live_rows}
            records = []
            for item in state["pending"].values():
                key = item["listing_id"]
                row = live_by_id.get(key)
                if item["part_type"] != code or not row or key not in source_parts:
                    continue
                expected = quote(source_parts[key], store, row["shipping_mode"], policy, freight)
                if (item["price"] == expected["new_price"] == money_str(parse_money(row["price"]))
                        and (not expected["shipping_policy"] or row["shipping_policy"] == expected["shipping_policy"])):
                    if (tab, key) not in touched:
                        if remaining <= 0:
                            continue
                        remaining -= 1
                        touched.add((tab, key))
                    records.append({**item, "title": row["title"]})
            if records:
                path = Path(args.out).with_name("ebay-price-revision-" + code + ".json")
                _write_json(path, records)
                # Publication remains the existing explicit push surface, with its preflight.
                status = push(SimpleNamespace(listings=str(path), tab="listed", rows=3000,
                              part_type=code, portal=args.portal, apply=True, cap=args.cap,
                              yes_i_mean_it=False, list_as_new=False))
                if status == 0:
                    for item in records:
                        state["pending"].pop(item["listing_id"], None)
                    report["submitted"].extend(records)
                    submitted += len(records)
                    _write_json(state_path, state)
                else:
                    report["errors"] += len(records)
        if remaining <= 0:
            break
    if args.apply and not args.part_type:
        state["cursor"] = (start + walked) % len(work) if work else 0
        _write_json(state_path, state)
    _write_json(Path(args.out), report)
    print(f"Saved and verified {saved}; submitted {submitted} listed revisions; "
          f"{len(report['held'])} held.", flush=True)
    return 1 if report["errors"] else 0


def add_arguments(parser):
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--revise-listed", action="store_true",
                        help="submit verified price changes for already-listed items")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--cap", type=int, default=100)
    parser.add_argument("--types-per-run", type=int, default=12)
    parser.add_argument("--part-type")
    parser.add_argument("--portal")
    parser.add_argument("--state", default=str(out_dir() / "ebay-price-state.json"))
    parser.add_argument("--out", default=str(out_dir() / "ebay-auto-prices.json"))
    parser.set_defaults(func=run)
    return parser
