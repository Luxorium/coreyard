"""What ran, when, and whether it worked.

Every scheduled job writes to a log file and exits; nothing reads those logs. That is
survivable for a failure, which at least leaves a traceback, and not for a *success that
stopped happening* — a job removed from the crontab, or one whose entry point was renamed,
looks exactly like a quiet day. This records each run in the state database so
``coreyard status`` can answer "when did a full sync last finish, and did it work" without
anyone parsing a log.

Deliberately small: one row per run, no metrics system, no retention policy beyond a cap.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from coreyard.state import DEFAULT_STATE_DB

# Enough history to see a pattern (a job failing every third run), not enough to grow
# without bound. At a run every five minutes this is about a fortnight.
MAX_ROWS = 4000

_DDL = """CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  command TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '',
  started TEXT NOT NULL,
  finished TEXT,
  ok INTEGER,
  counts TEXT NOT NULL DEFAULT '{}')"""

# Filled in by whichever command is running, read when the run is recorded. A module global
# rather than a parameter threaded through eight call sites, because the counts are produced
# deep inside the publish loop and the recording happens at the CLI boundary.
_counts: dict = {}


def count(**values) -> None:
    """Attach counts to the run in progress (created, retired, failed, ...)."""
    _counts.update(values)


def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=30)
    conn.execute(_DDL)
    conn.commit()
    return conn


@contextmanager
def record(command: str, scope: str = "", db: Path = DEFAULT_STATE_DB):
    """Record one run, whatever happens to it.

    A crash is the case worth recording — an unrecorded failure is indistinguishable from a
    job that was never scheduled — so the row is written from a ``finally``. Recording must
    never be the thing that breaks a run, so every failure here is swallowed.
    """
    _counts.clear()
    started = datetime.now(timezone.utc)
    ok = False
    try:
        yield
        ok = True
    except SystemExit as exc:
        ok = exc.code in (0, None)
        raise
    except BaseException:
        raise
    finally:
        try:
            conn = _connect(db)
            with conn:
                conn.execute(
                    "INSERT INTO runs(command, scope, started, finished, ok, counts)"
                    " VALUES (?,?,?,?,?,?)",
                    (command, scope, started.isoformat(),
                     datetime.now(timezone.utc).isoformat(), int(ok),
                     json.dumps(_counts, default=str)),
                )
                conn.execute(
                    "DELETE FROM runs WHERE id NOT IN"
                    " (SELECT id FROM runs ORDER BY id DESC LIMIT ?)", (MAX_ROWS,))
            conn.close()
        except Exception:
            pass


def history(limit: int = 20, db: Path = DEFAULT_STATE_DB) -> list[dict]:
    """Recent runs, newest first.

    Returns [] for anything that goes wrong — a missing file, an unwritable directory, a
    table that is not there yet. ``coreyard status`` reads this, and status is the command
    people run when something is *already* broken; it must not be the second thing to fail.
    """
    conn = None
    try:
        conn = _connect(db)
        rows = conn.execute(
            "SELECT command, scope, started, finished, ok, counts FROM runs"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()
    out = []
    for command, scope, started, finished, ok, counts in rows:
        try:
            parsed = json.loads(counts)
        except ValueError:
            parsed = {}
        out.append({"command": command, "scope": scope or "", "started": started,
                    "finished": finished, "ok": bool(ok), "counts": parsed})
    return out


def last(command: str, scope: str | None = None,
         db: Path = DEFAULT_STATE_DB) -> dict | None:
    """The most recent run of one command (optionally one scope), or None."""
    for run in history(limit=MAX_ROWS, db=db):
        if run["command"] == command and (scope is None or run["scope"] == scope):
            return run
    return None
