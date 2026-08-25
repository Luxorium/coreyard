"""Catalog repair: bring products published by older versions back in line with the renderer.

The incremental sync compares the yard against its own fingerprint snapshot, never against
what Shopify is actually showing. That is the right trade for steady-state operation and it
has one consequence: when the renderer improves, products whose yard data has not moved keep
whatever text they were first published with, indefinitely. A dashed year span, a doubled
make in a vehicle tag, a missing shipping weight — none of them look like a change to a
fingerprint diff, because on the yard's side nothing changed.

Repair closes that gap directly. It asks the store what each product says, asks the
canonical renderer what it should say, and rewrites only the differences. It is idempotent
by construction — a second run finds nothing to do — so it needs no progress file and an
interrupted run simply resumes.

Repair is for output that a *previous version of CoreYard* produced. It is deliberately not
a place to fix data: a wrong price or a wrong part type is repaired in the yard system,
where it is the source of truth, and the next sync carries it across.
"""

from coreyard.repair.engine import FIELDS, Change, plan, scan

__all__ = ["FIELDS", "Change", "plan", "scan"]
