"""Where a yard's inventory comes from.

CoreYard was written against one yard's SQL Server, reached over an SMB named pipe, with
its table and column names supplied at runtime by ``schema.json``. That made the *schema*
portable and left the *transport* welded in place: every module that wanted a part imported
``coreyard.yms.db`` directly, so a yard on Postgres, on MySQL, or with nothing but a nightly
CSV export could not run CoreYard at all, however neutral :class:`~coreyard.models.Part`
was.

This is the seam that fixes that. A source answers a handful of questions about parts and
knows nothing about Shopify, listings or state; everything downstream consumes
:class:`~coreyard.models.Part` and cannot tell the implementations apart.

Two ship today:

``database``
    The original: the site's SQL Server, over the named pipe, mapped by ``schema.json``.
    Still the default, so no existing installation changes behaviour.
``tabular``
    A CSV file or SQLite database whose columns are named after ``Part`` fields. This is
    what makes CoreYard runnable by a yard on a different system, by a contributor with no
    yard at all, and by the offline demo.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from coreyard.models import Part


@runtime_checkable
class Source(Protocol):
    """The questions CoreYard asks about a yard's inventory.

    ``images_only`` narrows to parts that have at least one photograph, where the source
    can tell; a source that cannot may ignore it, because :func:`photos_required` is the
    single definition of "listable" and it is enforced again downstream.
    """

    def parts(self, limit: Optional[int] = None,
              images_only: Optional[bool] = None) -> list[Part]:
        """Every listable part, newest identity rules applied."""

    def parts_by_r_number(self, r_numbers: list[str]) -> dict[str, Part]:
        """Specific parts by R#, listable or not — this is how a sold part is looked up."""

    def listable_r_numbers(self, images_only: Optional[bool] = None) -> set[str]:
        """Just the identities, for reconciliation and retirement decisions."""

    def server_now(self) -> datetime:
        """The source's own clock. Delta runs anchor their cursor to it, never to ours."""

    def ping(self) -> str:
        """A short human-readable liveness line, for ``doctor``."""


def load(spec: Optional[str] = None) -> Source:
    """The configured source. ``spec`` is ``database`` (default) or ``tabular:<path>``.

    Read at call time rather than import time so tests and the demo can select a source
    without the environment, and so an installation that never sets ``COREYARD_SOURCE``
    keeps the database it has always used.
    """
    from coreyard.config import _get

    spec = str(spec or _get("COREYARD_SOURCE", "database")).strip()
    kind, _, argument = spec.partition(":")
    kind = kind.strip().lower() or "database"

    if kind == "database":
        from coreyard.source.database import DatabaseSource
        return DatabaseSource()
    if kind == "tabular":
        from coreyard.source.tabular import TabularSource
        if not argument:
            raise ValueError(
                "a tabular source needs a path: COREYARD_SOURCE=tabular:/path/parts.csv"
            )
        return TabularSource(argument.strip())
    raise ValueError(f"unknown source {kind!r}; expected 'database' or 'tabular:<path>'")
