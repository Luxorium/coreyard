"""Ask the source database what changed, instead of re-reading the whole yard.

A full extract is the honest way to answer "what should be listed", but it is also the
reason the sync can only run hourly: it reads every part over a named pipe, then throws
almost all of it away because a typical hour moves a few dozen rows. That gap is the
window in which a part sold at the counter — or on another sales channel — stays buyable
on the storefront.

This module answers the cheaper question. Given a cursor, it returns only the parts whose
row moved since then, each carrying the ``in_scope`` verdict from the mapping's own
``scope`` predicate, plus the parts whose *photos* moved (which usually leaves the part row
untouched and would otherwise be invisible).

Two properties make the result safe to act on:

* **Retirement is evidence-based.** A full run infers "sold" from absence, which is why it
  needs the fraction guard — a broken query also produces absence. A delta run never
  reasons from absence: it retires a part only when it has the row in hand and that row
  says out-of-scope.
* **The cursor comes from the server's clock**, captured *before* the read and stored with
  a deliberate overlap, so rows committed during the run are re-examined next time rather
  than skipped. Re-examining is free — publishing is idempotent.

A delta run is a catch-up, not a replacement: it cannot see a part that changed while the
cursor was not being kept, so the full sync stays the source of truth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from coreyard.models import Part
from coreyard.yms import schema
from coreyard.yms.db import connect, query, server_now
from coreyard.yms.inventory import FETCH_PAGE_SIZE, photos_required, row_to_part

# Name of the stored cursor. One cursor covers both the row and photo deltas: they are read
# in the same pass against the same server clock, so splitting them would only create a way
# for the two to drift apart.
CURSOR_NAME = "delta_modified_at"

# Re-read this far before the stored cursor. A row is stamped when its transaction *starts*
# but only becomes visible when it commits, so a write in flight during the read can land
# with a timestamp the read already passed. The overlap trades a few redundant rows for not
# losing that write.
DEFAULT_OVERLAP = timedelta(minutes=5)

# A delta that returns more than this is not a delta. Rather than quietly reading the whole
# catalogue one page at a time through a path with no retirement guards, stop and say so.
MAX_DELTA_ROWS = 5000

# The source exposes SQL only through a named pipe, which has a finite number of instances.
# This job connects every few minutes alongside the hourly sync, the order poller and the
# invoiced tagger, so it occasionally arrives while no instance is free and the server answers
# STATUS_PIPE_NOT_AVAILABLE. Observed at roughly 2 runs in 27.
#
# A failed run is already harmless — the cursor only advances on success, so the next run
# re-covers the same window — but it costs a traceback in the log and a health alert for a
# condition that clears by itself in seconds. Retrying the read is safe because it is
# SELECT-only and idempotent.
CONNECT_RETRIES = 3
CONNECT_BACKOFF = 4.0

_TRANSIENT = (
    "STATUS_PIPE_NOT_AVAILABLE",     # no free pipe instance right now
    "STATUS_PIPE_BUSY",
    "STATUS_INSUFF_SERVER_RESOURCES",
    "STATUS_CONNECTION_DISCONNECTED",
)


def _is_transient(exc: BaseException) -> bool:
    """Whether a failure is the pipe being momentarily busy rather than something wrong.

    Matched on the server's status name, not the exception class: the transport raises the
    same impacket ``SessionError`` for a busy pipe and for a rejected login, and retrying the
    second one would just relearn that the credentials are wrong, three times, every time.
    """
    text = str(exc)
    return any(status in text for status in _TRANSIENT)


def _truthy(value: Any) -> bool:
    """Coerce the ``in_scope`` column, whatever shape the transport hands back.

    The TDS layer variously yields ints, bools, or strings for a computed bit, and the
    string ``'False'`` is famously truthy in Python. Decide explicitly.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text in ("1", "true", "y", "yes")


@dataclass
class DeltaResult:
    """What moved since the cursor."""

    listable: list[Part] = field(default_factory=list)   # changed and still publishable
    left_scope: list[str] = field(default_factory=list)  # changed and no longer publishable
    photo_changed: set[str] = field(default_factory=set)
    cursor: Optional[datetime] = None                    # store only after acting on this
    truncated: bool = False                              # too big to be a delta; run a full sync

    def summary(self) -> str:
        return (
            f"changed={len(self.listable)} left_scope={len(self.left_scope)} "
            f"photos={len(self.photo_changed)}"
        )


def _page_rows(conn, mapping, since: datetime,
               images_only: bool = False) -> tuple[list[dict[str, Any]], bool]:
    """Read every changed row, keyset-paged on R# so no reply is unbounded."""
    rows: list[dict[str, Any]] = []
    after: Any = None
    while True:
        page = query(conn, mapping.build_delta_query(since, FETCH_PAGE_SIZE, after,
                                                     images_only))
        rows.extend(page)
        if len(page) < FETCH_PAGE_SIZE:
            return rows, False
        if len(rows) >= MAX_DELTA_ROWS:
            return rows, True
        nxt = page[-1].get("r_number")
        if nxt is None or str(nxt) == str(after):
            raise RuntimeError("delta paging did not advance past the last R#")
        after = nxt


def _photo_changed_r_numbers(conn, mapping, since: datetime) -> set[str]:
    if not mapping.image_changes:
        return set()
    out: set[str] = set()
    for row in query(conn, mapping.build_image_changes_query(since)):
        value = row.get("r_number")
        if value is None and row:
            value = next(iter(row.values()))
        text = str(value).strip() if value is not None else ""
        if text and text not in ("NULL", "None"):
            out.add(text)
    return out


def fetch_changes(since: datetime, overlap: timedelta = DEFAULT_OVERLAP) -> DeltaResult:
    """Everything that moved since ``since``, plus the cursor to store afterwards.

    Retries only a busy named pipe, and only because the whole read is SELECT-only: a partial
    read is discarded and redone from scratch, never resumed, so a retry cannot stitch two
    inconsistent halves together.
    """
    for attempt in range(CONNECT_RETRIES):
        try:
            return _fetch_changes_once(since, overlap)
        except Exception as exc:
            if attempt == CONNECT_RETRIES - 1 or not _is_transient(exc):
                raise
            time.sleep(CONNECT_BACKOFF * (attempt + 1))
    raise RuntimeError("unreachable")


def _fetch_changes_once(since: datetime, overlap: timedelta) -> DeltaResult:
    mapping = schema.load()
    result = DeltaResult()
    with connect() as conn:
        # Captured before the read, so anything written while we are reading is caught by
        # the next run rather than falling into the gap between the two.
        now = server_now(conn)
        result.cursor = now - overlap

        # Same listable definition as the full sync, or the two would fight over every
        # unphotographed part that changed.
        images_only = photos_required(mapping)
        rows, result.truncated = _page_rows(conn, mapping, since, images_only)
        seen: set[str] = set()
        for row in rows:
            part = row_to_part(row)
            key = part.uid()
            if not key or key in seen:
                continue
            seen.add(key)
            if _truthy(row.get(schema.DELTA_SCOPE_FIELD)) and part.is_listable():
                result.listable.append(part)
            else:
                result.left_scope.append(key)

        # Photos move without touching the part row, so these are a separate question. Any
        # R# already covered by the row delta is dropped: it is in hand with a fresh scope
        # verdict, and re-reading it could only produce a staler answer.
        photos = _photo_changed_r_numbers(conn, mapping, since)
        result.photo_changed = set(photos)
        extra = sorted(photos - seen)
        if extra:
            for chunk in (extra[i:i + 200] for i in range(0, len(extra), 200)):
                for row in query(conn, mapping.build_lookup_query(
                        chunk, with_scope=True, images_only=images_only)):
                    part = row_to_part(row)
                    key = part.uid()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    if _truthy(row.get(schema.DELTA_SCOPE_FIELD)) and part.is_listable():
                        result.listable.append(part)
                    # A part whose photos changed but which is not listable is simply not a
                    # publish candidate. It is deliberately NOT added to left_scope: this
                    # run went looking for it by identity rather than observing it change,
                    # so "not listable" here is not evidence that it just stopped being so.

    result.listable.sort(key=lambda p: p.uid())
    result.left_scope.sort()
    return result
