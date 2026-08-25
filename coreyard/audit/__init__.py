"""Read-only catalog quality checks: is this catalogue actually ready to sell?

The checks are generic — a missing SKU, an unpriced product, a photo with no alt text, an
ACTIVE product with no stock — because those are broken for any store. The *thresholds* are
not: a yard selling three-dollar clips and one selling engines disagree about what a
suspiciously low price is, and an audit that shouts at every listing gets ignored, which is
worse than no audit. So every limit comes from the site's profile (see
:class:`coreyard.profile.AuditPolicy`).

Nothing here writes. It reads the store and prints what it found.
"""

from coreyard.audit.catalog import CHECKS, Finding, Report, evaluate

__all__ = ["CHECKS", "Finding", "Report", "evaluate"]
