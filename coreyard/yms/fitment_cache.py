"""A local copy of the interchange catalogue's application rows.

Resolving fitment is one query per *interchange group*, not per part, and this installation
has 16,239 distinct groups behind 26,858 parts — a reuse factor of 1.65. So the resolver's
in-memory cache saves about a third, and the remaining 16,239 round trips over the named
pipe cost the better part of an hour. Every command that renders a title pays it again:
a sync, a repair, an audit, the listing-portal daily pass.

That cost buys nothing, because the applications table is the *catalogue vendor's reference
data*. It describes which vehicles a part number fits, and it changes when the catalogue is
updated — not when a car arrives in the yard, not when a part is sold, and not between two
runs half an hour apart.

**What is cached is the raw application strings, never the parsed fitment.** Parsing them is
pure, local and cheap, and it is also the part of this pipeline that gets *fixed*: reading a
model number as a year, widening a title's years from a row with no make, cutting a
qualifier where the restriction lives. Caching the parsed result would mask the next such
fix behind a stale cache until somebody remembered to clear it. Caching the strings means a
parser improvement takes effect on the very next run, for free.

Three things invalidate an entry, because a cache that cannot go stale is a cache nobody has
thought about:

* **The query changed.** The cache stamps a fingerprint of the site's own
  ``interchange_applications`` and ``interchange_makes`` SQL. Edit ``schema.json`` and every
  entry is void, because the rows it holds answered a different question.
* **Age.** Entries older than ``COREYARD_FITMENT_CACHE_DAYS`` (default 30) are ignored, so a
  catalogue update is picked up without anyone doing anything.
* **A refusal to guess.** An empty result is cached like any other — a part number that fits
  nothing is a real answer — but a *failed* query is not cached at all, so a transport blip
  cannot be frozen into "this part fits nothing".

Set ``COREYARD_FITMENT_CACHE=0`` to switch it off entirely.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional, Sequence

from coreyard import config

# Bumped when the stored shape changes. A row written by an older layout is dropped rather
# than interpreted, for the same reason the image manifest carries a version.
CACHE_VERSION = 1

DEFAULT_PATH = "coreyard_fitment_cache.sqlite3"
DEFAULT_MAX_AGE_DAYS = 30


def query_fingerprint(*statements: str) -> str:
    """A stable digest of the SQL whose answers are being stored.

    The cache holds rows that answered *these* statements. If a site edits the query — a
    different column, another join, a widened WHERE — the stored rows answer a question
    nobody is asking any more, so they must not be served.
    """
    digest = hashlib.sha256()
    digest.update(str(CACHE_VERSION).encode("utf-8"))
    for statement in statements:
        digest.update(b"\x00")
        digest.update(" ".join((statement or "").split()).encode("utf-8"))
    return digest.hexdigest()


def enabled() -> bool:
    """Whether this installation wants the cache. On unless explicitly switched off."""
    raw = (config._get("COREYARD_FITMENT_CACHE", "1") or "").strip().lower()
    return raw not in {"0", "no", "off", "false"}


def cache_path() -> Path:
    return Path(config._get("COREYARD_FITMENT_CACHE_FILE", "") or DEFAULT_PATH)


def max_age_seconds() -> float:
    raw = (config._get("COREYARD_FITMENT_CACHE_DAYS", "") or "").strip()
    try:
        days = float(raw) if raw else DEFAULT_MAX_AGE_DAYS
    except ValueError:
        days = DEFAULT_MAX_AGE_DAYS
    return max(0.0, days) * 86400.0


class FitmentCache:
    """Application rows for ``(part type code, interchange code)``, kept between runs.

    The connection is bound to the thread that opened it, as every sqlite connection is.
    Fitment resolution is single-threaded in every path that uses it, and a cache is an
    optimisation rather than a record, so this deliberately does not try to be shared.
    """

    def __init__(self, path: "str | Path", fingerprint: str,
                 max_age: Optional[float] = None) -> None:
        self.path = Path(path)
        self.fingerprint = fingerprint
        self.max_age = max_age_seconds() if max_age is None else max_age
        self.hits = 0
        self.misses = 0
        self.writes_lost = 0
        self._closed = False
        self.degraded = False
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # Every scheduled job on this host resolves fitment, so several processes share this
        # file. WAL allows exactly one writer, and each write here is a single small row, so
        # waiting briefly is right and waiting long is not: the caller has a database to
        # query and must not be held up by an optimisation.
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS applications ("
            " part_type_code INTEGER NOT NULL,"
            " interchange_code TEXT NOT NULL,"
            " apps TEXT NOT NULL,"
            " fetched_at REAL NOT NULL,"
            " PRIMARY KEY (part_type_code, interchange_code))"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.conn.commit()
        self._enforce_fingerprint()

    def _enforce_fingerprint(self) -> None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='fingerprint'").fetchone()
        stored = row[0] if row else None
        if stored == self.fingerprint:
            return
        # The rows answered a different query. Dropping them is the only safe reading:
        # serving them would publish fitment the site's own SQL no longer asks for.
        self.conn.execute("DELETE FROM applications")
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('fingerprint', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.fingerprint,))
        self.conn.commit()

    def get(self, part_type_code: int, interchange_code: str) -> Optional[list[str]]:
        """Stored applications, or ``None`` when this group must be fetched.

        Never raises. A cache that cannot answer is indistinguishable from one that has
        nothing to say, and the caller has a database to fall back on.
        """
        if self.degraded:
            return None
        try:
            row = self.conn.execute(
                "SELECT apps, fetched_at FROM applications"
                " WHERE part_type_code=? AND interchange_code=?",
                (int(part_type_code), str(interchange_code)),
            ).fetchone()
        except sqlite3.Error:
            self._degrade()
            return None
        if row is None:
            self.misses += 1
            return None
        if self.max_age and (time.time() - float(row[1])) > self.max_age:
            self.misses += 1
            return None
        try:
            apps = json.loads(row[0])
        except (TypeError, ValueError):
            self.misses += 1
            return None
        if not isinstance(apps, list):
            self.misses += 1
            return None
        self.hits += 1
        return [str(a) for a in apps]

    def put(self, part_type_code: int, interchange_code: str,
            apps: Sequence[str]) -> None:
        """Store one group's applications. An empty list is a real answer and is kept.

        Never raises, and commits on the spot. Batching these into one transaction held the
        single WAL write lock across hundreds of round trips and starved every other job on
        the host — a five-minute ``sync delta`` against a two-hour repair. The write itself
        is one small row and the caller has just waited on a network query, so committing
        immediately costs nothing measurable and holds the lock for milliseconds.
        """
        if self.degraded:
            return
        try:
            self.conn.execute(
                "INSERT INTO applications(part_type_code, interchange_code, apps, fetched_at)"
                " VALUES(?,?,?,?)"
                " ON CONFLICT(part_type_code, interchange_code) DO UPDATE SET"
                "  apps=excluded.apps, fetched_at=excluded.fetched_at",
                (int(part_type_code), str(interchange_code),
                 json.dumps([str(a) for a in apps]), time.time()),
            )
        except sqlite3.Error:
            # Losing one row costs one query on some later run. Losing the run costs hours.
            self.writes_lost += 1

    def _degrade(self) -> None:
        """Stop consulting the cache for the rest of this run, quietly."""
        self.degraded = True

    def commit(self) -> None:
        """Kept for callers that batch; writes are already committed as they are made."""
        return

    def summary(self) -> str:
        """One line on what the cache did, including when it stopped being able to.

        Degradation is reported even when nothing was read, because a silent cache and a
        broken one look identical from the outside and only one of them is fine.
        """
        total = self.hits + self.misses
        if total:
            share = 100.0 * self.hits / total
            text = (f"fitment cache: {self.hits:,} hit / {self.misses:,} fetched "
                    f"({share:.0f}% served locally)")
        else:
            text = "fitment cache: unused"
        if self.writes_lost:
            text += f", {self.writes_lost:,} not stored (busy)"
        if self.degraded:
            text += " — cache gave up, the run continued"
        return text

    def close(self) -> None:
        """Bank and release. Safe to call twice — a cache is closed on many paths."""
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self.conn.commit()
        except sqlite3.Error:
            pass
        finally:
            self.conn.close()

    def __enter__(self) -> "FitmentCache":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def open_for(schema, path: "str | Path | None" = None) -> Optional[FitmentCache]:
    """A cache for this site's interchange SQL, or ``None`` when it is switched off."""
    if not enabled() or not getattr(schema, "interchange_applications", ""):
        return None
    fingerprint = query_fingerprint(schema.interchange_applications,
                                   getattr(schema, "interchange_makes", ""))
    try:
        return FitmentCache(path or cache_path(), fingerprint)
    except sqlite3.Error:
        # A cache that cannot be opened is not a reason to fail a run that was going to
        # query the database anyway.
        return None
