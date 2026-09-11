"""Write each published part's storefront address back into the yard record.

The third module in CoreYard that writes to the yard system, and much the smallest. The
other two — :mod:`coreyard.yms.orders` and :mod:`coreyard.yms.invoices` — exist because a
sale has to become a document in the system of record. This one exists for a person: the
salesman at the counter has the part open in the yard system and no way to reach its
listing, so he searches the store by hand for something the pipeline has known the address
of all along.

What makes the write safe is narrower than what makes an order safe, because it is a much
smaller thing to get wrong:

* **One column, one statement.** Each batch is a single set-based UPDATE against a column
  the yard system uses for nothing else. No counters are allocated, no rows are inserted,
  and nothing about a part's price, quantity or location is touched — so there is no state
  to leave half-written and nothing to roll back to.
* **The guard is in the site's own SQL.** ``stamp`` and ``clear`` only ever change rows
  holding nothing, or an address this installation wrote. A description somebody at the
  yard typed is left exactly as they typed it; :class:`~coreyard.yms.schema.BacklinkWrite`
  refuses a template that does not say so.
* **Truncation is an error, not a shortening.** A link cut off at the column width is a
  link that goes nowhere, which is worse than no link, so an over-long address is refused
  rather than written.

One consequence worth knowing before the first run: the yard system stamps its own
"last modified" on any row it sees change, so the parts this touches will look changed to
the next incremental sync. That costs an extract, not a republish — the renderer never sees
a backlink (:func:`coreyard.yms.inventory.row_to_part` drops it), so no fingerprint moves
and nothing is pushed to the store.

No table or column name appears here. They belong to the yard system's vendor and come from
the site's own ``schema.json`` — see ``backlink_write`` in ``schema.example.json``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional

from coreyard.yms import schema as schema_mod
# One implementation of each, deliberately shared with the order writer: the quoting rule is
# the only thing between a text column and arbitrary SQL, and "impacket reports server
# errors as reply tokens rather than raising" has to be handled identically everywhere a
# write happens or the second copy is the one that silently succeeds.
from coreyard.yms.orders import _errors, _literal

#: Rows per batch. A SQL Server table-value constructor takes at most 1000, and half that
#: keeps the statement text small enough to stay comfortable over the named pipe.
CHUNK = 500

#: The same strict character class the lookup query validates R#s with. These arrive from
#: Shopify product handles, so they are store input and are never interpolated unchecked.
_R_NUMBER = re.compile(r"[A-Za-z0-9_-]{1,32}")


class BacklinkWriteError(RuntimeError):
    """A write was refused or failed. Nothing was changed by the failing batch."""


@dataclass(frozen=True)
class BacklinkResult:
    """What one run actually changed."""

    stamped: int = 0
    cleared: int = 0

    @property
    def changed(self) -> int:
        return self.stamped + self.cleared


def _key(value: Any) -> str:
    """A validated, quoted R# literal."""
    key = str(value).strip()
    if not _R_NUMBER.fullmatch(key):
        raise BacklinkWriteError(f"refusing to write against implausible R# {value!r}")
    return f"'{key}'"


def like_pattern(prefix: str) -> str:
    """A quoted ``LIKE`` pattern matching every address under ``prefix``.

    The wildcards are bracket-escaped rather than declared with ``ESCAPE``, so the pattern
    is self-contained and the site's template needs no extra clause to be correct. Without
    this a ``_`` in a store's handle prefix would quietly match one character of anything.
    """
    escaped = prefix.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    return _literal(escaped + "%", len(escaped) + 8)


def chunks(items: list, size: int = CHUNK) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def build_stamp(pairs: Iterable[tuple[str, str]], write: "schema_mod.BacklinkWrite",
                prefix: str) -> str:
    """One batch setting the column for each ``(R#, address)`` pair."""
    rows = []
    for r_number, url in pairs:
        text = str(url).strip()
        if len(text) > write.limit:
            raise BacklinkWriteError(
                f"the address for R#{r_number} is {len(text)} characters and the column "
                f"holds {write.limit}; a truncated link goes nowhere, so it is not written"
            )
        rows.append(f"({_key(r_number)}, {_literal(text, write.limit)})")
    if not rows:
        raise BacklinkWriteError("no backlinks to write")
    statement = write.stamp.format(rows=", ".join(rows), ours=like_pattern(prefix))
    return _batch(statement)


def build_clear(r_numbers: Iterable[str], write: "schema_mod.BacklinkWrite",
                prefix: str) -> str:
    """One batch emptying the column for parts whose listing has gone."""
    keys = [_key(r) for r in r_numbers]
    if not keys:
        raise BacklinkWriteError("no backlinks to clear")
    statement = write.clear.format(r_numbers=", ".join(keys), ours=like_pattern(prefix))
    return _batch(statement)


def _batch(statement: str) -> str:
    """Wrap one site statement so a failure aborts it and the row count comes back.

    ``@@ROWCOUNT`` is what the caller reports, and it is the honest number: the guard in the
    site's predicate means a batch can legitimately change fewer rows than it names, and a
    run that says "stamped 500" when it stamped 3 is the kind of reassurance that stops
    anybody looking.
    """
    return ("SET NOCOUNT ON;\nSET XACT_ABORT ON;\n\n"
            f"{statement.rstrip().rstrip(';')};\nSELECT @@ROWCOUNT AS changed;")


def execute(conn: Any, batch: str) -> int:
    """Run one built batch on an open connection; return the rows it changed."""
    conn.sql_query(batch)
    problems = _errors(conn)
    if problems:
        raise BacklinkWriteError("; ".join(problems))
    rows = [dict(r) for r in conn.rows]
    if not rows:
        raise BacklinkWriteError("the batch returned no row count; treat it as unwritten")
    value = rows[-1].get("changed")
    return int(value) if value not in (None, "NULL") else 0


def load() -> "schema_mod.BacklinkWrite":
    """This installation's backlink mapping, or a refusal naming what is missing."""
    write = schema_mod.load().backlink_write
    if write is None:
        raise BacklinkWriteError(
            "This installation has no 'backlink_write' section in schema.json, so CoreYard "
            "will not write storefront links into the yard. See schema.example.json."
        )
    return write


def current(conn: Any, write: Optional["schema_mod.BacklinkWrite"] = None) -> dict[str, str]:
    """Every part whose column holds something, as ``{R#: text}``.

    Read whole rather than part by part. The column is empty on all but a handful of rows
    at a yard that has never used it, and once this command has run it holds one short
    address per listed part — either way it is one round trip against an installation
    reached through a named pipe, where round trips are the cost that matters.
    """
    from coreyard.yms.db import query

    write = write or load()
    found: dict[str, str] = {}
    for row in query(conn, write.current):
        r_number = str(row.get("r_number") or "").strip()
        text = row.get("backlink")
        text = "" if text in (None, "NULL") else str(text).strip()
        if r_number and text:
            found[r_number] = text
    return found


def apply(stamp: list[tuple[str, str]], clear: list[str], prefix: str, *,
          dry_run: bool = False) -> BacklinkResult:
    """Write the planned changes. ``dry_run`` prints the batches and writes nothing."""
    write = load()
    stamp_batches = [build_stamp(chunk, write, prefix) for chunk in chunks(stamp)]
    clear_batches = [build_clear(chunk, write, prefix) for chunk in chunks(clear)]

    if dry_run:
        for batch in stamp_batches + clear_batches:
            print(batch)
        return BacklinkResult()

    from coreyard.yms.db import connect

    stamped = cleared = 0
    with connect() as conn:
        for batch in stamp_batches:
            stamped += execute(conn, batch)
        for batch in clear_batches:
            cleared += execute(conn, batch)
    return BacklinkResult(stamped=stamped, cleared=cleared)
