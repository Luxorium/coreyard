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

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from coreyard.models import Part


@dataclass(frozen=True)
class SourceTraits:
    """What a source can and cannot do, declared by the adapter that knows.

    The alternative — and what CoreYard did until this existed — is for the rest of the
    program to compare ``COREYARD_SOURCE`` against the string ``"database"`` wherever a
    decision depends on the answer. That reached about twenty-five comparisons across five
    modules, every one of them phrased as a binary: database, or not. A yard system that is
    neither the one this was written against nor a flat file has to be correct in all of
    them, which is another way of saying it has to be a fork.

    Each field is a question some other module actually asks. Adding a source means
    answering them once, here, rather than being found by grep.
    """

    #: What ``COREYARD_SOURCE`` names, and what diagnostics print.
    kind: str

    #: Does the site supply table and column names in ``schema.json``? A source whose
    #: columns are already ``Part`` field names is its own mapping.
    needs_schema_mapping: bool

    #: Does the ``YMS_DB_*`` block name this source's server? Separate from
    #: :attr:`needs_smb` because a second SQL dialect could want a database block without
    #: reaching it over a named pipe.
    needs_database_config: bool

    #: Is it reached over the SMB named pipe? Decides whether the ``SMB_*`` block is
    #: required configuration or meaningless noise.
    needs_smb: bool

    #: Does it attach ``part.images`` itself? A source that does has no photo share to
    #: list, and asking it to list one shells out to `smbclient` for nothing.
    carries_own_photos: bool

    #: Can it answer "what changed since"? Requires a server clock to anchor a cursor to.
    supports_delta: bool

    #: Can it resolve interchange fitment — which vehicles a part number fits?
    supports_fitment: bool

    #: Can a storefront sale be written back to it as a work order?
    supports_order_booking: bool

    #: Does :meth:`Source.server_now` date the *data* rather than report the current time?
    #: A live database answers with its own clock, so a successful read is by definition a
    #: current one. A file answers with its modification time, which is when something last
    #: wrote an export — and an export that stopped being written reads perfectly while
    #: describing a yard that has moved on. The second kind has a maximum age; the first
    #: cannot have one.
    clock_dates_the_data: bool = False


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

    @classmethod
    def probe(cls, argument: str) -> tuple[str, str, tuple[str, ...]]:
        """Can this source be reached *in principle*, judged from configuration alone?

        Returns ``(state, detail, keys)`` for :mod:`coreyard.capabilities`: one of its
        ``ON``/``OFF``/``MISSING`` states, a line explaining the verdict, and the settings
        that decide it, so a diagnostic can name what to fix.

        Cheap and offline, always. This runs before every command to decide whether the
        command can run at all, so it may look at configuration and at the filesystem but
        must not open a connection, authenticate or query. ``ping`` is where reaching the
        server belongs.
        """


# The one place a yard system is registered. `kind -> (module, class name)`, imported on
# demand so selecting a CSV source does not import the database transport, and so the
# traits of a source can be read without constructing one.
_ADAPTERS: dict[str, tuple[str, str]] = {
    "database": ("coreyard.source.database", "DatabaseSource"),
    "tabular": ("coreyard.source.tabular", "TabularSource"),
}


def kinds() -> list[str]:
    """Every registered source kind, for error messages and diagnostics."""
    return sorted(_ADAPTERS)


@dataclass(frozen=True)
class Freshness:
    """How old this source's data is, and whether that is too old to act on.

    ``age`` is None when the source has no freshness signal — which is not "unknown" in the
    worrying sense but "not applicable": a live database read is current because it happened.
    ``stale`` is never True without an age.
    """

    age: "timedelta | None"
    limit: "timedelta | None"
    stale: bool
    detail: str


DEFAULT_MAX_AGE_HOURS = 24.0


def max_age() -> "timedelta | None":
    """How old a dated source may be before CoreYard stops acting on it.

    ``SOURCE_MAX_AGE_HOURS=0`` turns the check off, for a site whose export is deliberately
    occasional and whose operator would rather have the publish than the guard.
    """
    from datetime import timedelta

    from coreyard.config import _get

    try:
        hours = float(_get("SOURCE_MAX_AGE_HOURS", str(DEFAULT_MAX_AGE_HOURS)))
    except (TypeError, ValueError):
        hours = DEFAULT_MAX_AGE_HOURS
    return timedelta(hours=hours) if hours > 0 else None


def freshness(source=None, spec: str = "") -> Freshness:
    """Age this installation's source, without assuming it has an age.

    Reading a file's modification time is a stat; this never opens a connection, because the
    sources that would need one are exactly the sources whose clock is the current time.
    """
    from datetime import datetime, timedelta

    facts = traits(spec)
    if not facts.clock_dates_the_data:
        return Freshness(None, None, False, f"{facts.kind}: read live, so always current")
    limit = max_age()
    try:
        source = source or load(spec)
        observed = source.server_now()
    except Exception as exc:
        # Unknown is not fresh. A source that cannot say when it was written is exactly the
        # case this criterion names, and it is treated as stale rather than as fine.
        return Freshness(None, limit, True,
                         f"{facts.kind}: cannot read a modification time ({exc})")
    age = datetime.now() - observed
    if age < timedelta(0):
        age = timedelta(0)                      # a clock ahead of ours is not staleness
    hours = age.total_seconds() / 3600
    if limit is None:
        return Freshness(age, None, False, f"{facts.kind}: {hours:.1f}h old (no limit set)")
    stale = age > limit
    return Freshness(age, limit, stale,
                     f"{facts.kind}: {hours:.1f}h old, limit "
                     f"{limit.total_seconds() / 3600:.0f}h")


def _split(spec: Optional[str] = None) -> tuple[str, str]:
    """``COREYARD_SOURCE`` as ``(kind, argument)``, defaulting to the database."""
    from coreyard.config import _get

    text = str(spec if spec is not None else _get("COREYARD_SOURCE", "database")).strip()
    kind, _, argument = text.partition(":")
    return (kind.strip().lower() or "database"), argument.strip()


def _adapter(kind: str) -> type:
    try:
        module_name, class_name = _ADAPTERS[kind]
    except KeyError:
        raise ValueError(
            f"unknown source {kind!r}; expected one of {', '.join(kinds())}"
        ) from None
    import importlib

    return getattr(importlib.import_module(module_name), class_name)


def probe(spec: Optional[str] = None) -> tuple[str, str, tuple[str, ...]]:
    """Ask the configured source's adapter whether it is reachable in principle."""
    kind, argument = _split(spec)
    return _adapter(kind).probe(argument)


def traits(spec: Optional[str] = None) -> SourceTraits:
    """What the configured source can do, without connecting to it or constructing it.

    Read from the adapter class rather than the instance so this is safe to call from
    configuration and diagnostics — including when the source is misconfigured, which is
    exactly when something needs to know whether the `SMB_*` block was supposed to be
    filled in.
    """
    return _adapter(_split(spec)[0]).TRAITS


def load(spec: Optional[str] = None) -> Source:
    """The configured source. ``spec`` is ``database`` (default) or ``tabular:<path>``.

    Read at call time rather than import time so tests and the demo can select a source
    without the environment, and so an installation that never sets ``COREYARD_SOURCE``
    keeps the database it has always used.
    """
    kind, argument = _split(spec)
    adapter = _adapter(kind)
    if kind == "tabular" and not argument:
        raise ValueError(
            "a tabular source needs a path: COREYARD_SOURCE=tabular:/path/parts.csv"
        )
    return adapter(argument) if argument else adapter()
