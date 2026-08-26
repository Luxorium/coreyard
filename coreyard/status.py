"""``coreyard status`` — what the pipeline believes right now. Reads; never writes.

The question this answers is the one an operator actually has: *is the storefront currently
telling the truth about the yard, and if not, how far out is it?* Until now that took three
commands and some arithmetic — a reconcile plan for the counts, a log tail for the timings,
and knowing which SQLite file to open for the rest.

Every probe is guarded on its own. A status report that refuses to print anything because
Shopify is unreachable is useless precisely when it is needed: "Shopify FAILED" beside the
counts that *are* known is the useful answer, so a dead dependency becomes a line in the
report rather than a traceback.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from coreyard import ops
from coreyard.state import DEFAULT_STATE_DB, SyncState

UNKNOWN = "—"


def _ago(iso: str | None) -> str:
    """"3h 12m ago" for a stored timestamp, tolerant of anything unparseable."""
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return UNKNOWN
    now = datetime.now(timezone.utc) if then.tzinfo else datetime.now()
    seconds = int((now - then).total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d ago"


def _probe(label: str, fn) -> tuple[str, str]:
    try:
        return label, fn()
    except Exception as exc:
        return label, f"FAILED — {type(exc).__name__}: {str(exc)[:70]}"


def gather(deep: bool = False) -> dict:
    """Everything the report shows. Each section degrades to a message on its own."""
    report: dict = {"reachability": [], "counts": {}, "pending": {}, "runs": {}}

    report["reachability"].append(_probe("Yard database", lambda: (
        __import__("coreyard.yms.db", fromlist=["ping"]).ping().splitlines()[0].split(" - ")[0][:40])))
    report["reachability"].append(_probe("Photo share", lambda: (
        __import__("coreyard.yms.images", fromlist=["SmbImageStore"])
        .SmbImageStore().list_inventory_images("0") is not None and "OK")))
    report["reachability"].append(_probe("Shopify", lambda: (
        __import__("coreyard.sink.shopify_api", fromlist=["ShopifySink"])
        .ShopifySink().check())))

    # Local state first: it is the one source that is always readable, so a report that can
    # show nothing else can still show this.
    try:
        with SyncState(DEFAULT_STATE_DB) as state:
            snapshot = state.load()
            report["counts"]["CoreYard state"] = len(snapshot)
            report["counts"]["Archived, remembered"] = len(state.retired_statuses())
            report["cursor"] = state.get_cursor("delta_modified_at")
    except Exception as exc:
        report["counts"]["CoreYard state"] = f"FAILED — {str(exc)[:60]}"
        snapshot = {}

    try:
        from coreyard.yms.inventory import listable_r_numbers

        listable = set(listable_r_numbers())
        report["counts"]["Yard listable"] = len(listable)
    except Exception as exc:
        report["counts"]["Yard listable"] = f"FAILED — {str(exc)[:60]}"
        listable = None

    if deep:
        try:
            from coreyard.config import load_store
            from coreyard.reconcile.store import scan
            from coreyard.sink.shopify_api import ShopifyClient

            shop = scan(ShopifyClient(), load_store())
            report["counts"]["Shopify managed"] = len(shop)
            if listable is not None:
                active = {r for r, p in shop.items() if p.status == "ACTIVE"}
                report["pending"]["Missing from Shopify"] = len(listable - set(shop))
                report["pending"]["No longer listable"] = len(active - listable)
                report["pending"]["Live but on no channel"] = sum(
                    1 for r in active if not shop[r].published)
                report["pending"]["Quantity differs"] = UNKNOWN
            report["pending"]["State entries with no product"] = len(
                set(snapshot) - set(shop))
        except Exception as exc:
            report["counts"]["Shopify managed"] = f"FAILED — {str(exc)[:60]}"
    elif listable is not None and snapshot:
        # Cheap and honest: the snapshot is what the sync will diff against, so this is
        # exactly what the next run would find without asking the store anything.
        report["pending"]["Not in the snapshot"] = len(listable - set(snapshot))
        report["pending"]["In the snapshot, not listable"] = len(set(snapshot) - listable)

    for label, scope in (("Last full sync", ""), ("Last delta sync", "delta"),
                         ("Last inventory sync", "inventory")):
        run = ops.last("sync", scope)
        report["runs"][label] = ({"when": _ago(run["finished"] or run["started"]),
                                  "ok": run["ok"], "counts": run["counts"]}
                                 if run else None)
    for command in ("reconcile", "repair", "orders"):
        run = ops.last(command)
        if run:
            report["runs"][f"Last {command}"] = {
                "when": _ago(run["finished"] or run["started"]), "ok": run["ok"],
                "counts": run["counts"]}
    return report


def _render(report: dict) -> None:
    print("CoreYard")
    print("─" * 48)
    for label, detail in report["reachability"]:
        print(f"  {label:<32}{detail}")

    if report.get("counts"):
        print()
        for label, value in report["counts"].items():
            shown = f"{value:,}" if isinstance(value, int) else value
            print(f"  {label:<32}{shown:>12}")

    if report.get("pending"):
        print()
        for label, value in report["pending"].items():
            shown = f"{value:,}" if isinstance(value, int) else value
            print(f"  {label:<32}{shown:>12}")

    print()
    for label, run in report["runs"].items():
        if run is None:
            print(f"  {label:<32}{'never':>12}")
            continue
        counts = dict(run["counts"])
        # A dry run must never read as a completed sync. It is the one line here somebody
        # could act on wrongly: "last full sync 13m ago, ok" is exactly what you want to
        # see when deciding the pipeline is healthy, and a dry run did not publish anything.
        dry = counts.pop("dry_run", False)
        counts.pop("scope", None)
        verdict = "dry run" if dry else ("ok" if run["ok"] else "FAILED")
        detail = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v)
        line = f"  {label:<32}{run['when']:>12}  {verdict:<8}"
        print((line + f"  {detail}" if detail and not dry else line).rstrip())

    if report.get("cursor"):
        print(f"\n  delta cursor  {report['cursor']}")


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with the status flags."""
    ap.add_argument("--deep", action="store_true",
                    help="also page the live store, so the counts compare against what "
                         "Shopify actually holds rather than against the snapshot (slow)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.set_defaults(func=run)
    return ap


def run(args) -> int:
    report = gather(deep=args.deep)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _render(report)
    # Read-only and advisory: a stale pipeline is reported by `doctor`, whose job is to have
    # an opinion. Exiting non-zero here would make `status` unusable in a pipeline.
    return 0
