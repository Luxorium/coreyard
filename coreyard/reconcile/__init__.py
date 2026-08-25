"""Reconcile the storefront against the yard, rather than against a state file.

The incremental sync compares the yard with its own fingerprint snapshot. That is fast and
right for steady-state operation, and it is blind in one specific way: it never asks Shopify
what it actually holds. A part the snapshot calls "published" is invisible to every later
run, whether or not the product exists, whether or not it is a draft, and whether or not it
went to a sales channel. That is how a state file comes to record thousands more parts than
the store contains, with nothing in either system able to notice.

Reconciliation asks both sides directly and reports the differences it can safely close:

    create    listable in the yard, absent from Shopify        (sync creates these)
    activate  listable, sitting in Shopify as DRAFT            (policy-gated)
    revive    listable, ARCHIVED in Shopify                    (came back to the yard)
    retire    ACTIVE in Shopify, no longer listable            (guarded)
    publish   ACTIVE but on no sales channel — a 404 to shoppers
    drift     the state file and the store disagree

Retirement keeps the same guards as the sync path, because a yard database that answers
slowly, partially, or not at all looks exactly like a yard that sold out.
"""

from coreyard.reconcile.planner import Actions, ShopProduct, StateDrift, plan
from coreyard.reconcile.store import scan

__all__ = ["Actions", "ShopProduct", "StateDrift", "plan", "scan"]
