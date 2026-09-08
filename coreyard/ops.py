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
from datetime import datetime, timedelta, timezone
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

# Added after the table shipped. A row written before the upgrade has no code to report,
# which is why this reads as NULL rather than as 0: "we did not record it" and "it exited
# cleanly" are different answers, and only one of them should make a failed run look fine.
_MIGRATIONS = (("exit_code", "ALTER TABLE runs ADD COLUMN exit_code INTEGER"),)

# Written at the top of every scheduled run's output. Scheduled jobs append to one log
# forever, so without a boundary there is no way to tell a failure that is happening from
# one that was fixed days ago: an error sits in the tail window and is re-reported until
# enough traffic pushes it out — which for a job that prints four lines a tick can take a
# week. `doctor` reads only the last run's segment because of this line.
RUN_HEADER = "=== coreyard"


def run_header(command: str, scope: str = "") -> str:
    stamp = datetime.now().isoformat(timespec="seconds")
    return f"{RUN_HEADER} {command}{' ' + scope if scope else ''} @ {stamp} ==="


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
    present = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    for column, statement in _MIGRATIONS:
        if column not in present:
            conn.execute(statement)
    conn.commit()
    return conn


# A row with no `finished` is a run in flight — unless it is old, in which case the process
# was killed outright and will never come back to close it.
STALE_RUN = timedelta(hours=2)


# 124 is the status GNU ``timeout`` uses and the one ``cli._deadline`` raises. A run
# stopped by its own deadline kept everything it banked, so it is recorded as an outcome in
# its own right rather than as a failure — reporting it FAILED buries the runs that broke.
TIMED_OUT = 124


class Outcome:
    """The exit status of the run being recorded.

    A command reports failure by *returning* a nonzero code, not by raising: that is what
    ``argparse``-style CLIs do and what ``cli.main`` propagates to the shell. The recorder
    therefore has to be told, because from inside a ``with`` block a function that returns 2
    and one that returns 0 look identical. Not being told is exactly how a sync that exited
    2 was written to the run history as ``ok=1``, so ``coreyard status`` reported the last
    full sync as successful while the shell had already reported it as failed.
    """

    __slots__ = ("code",)

    def __init__(self) -> None:
        self.code: int | None = 0

    @property
    def ok(self) -> bool:
        return self.code in (0, None, TIMED_OUT)


@contextmanager
def record(command: str, scope: str = "", db: Path = DEFAULT_STATE_DB):
    """Record one run, whatever happens to it.

    The row is written when the run *starts* and closed when it ends, so a run in progress
    is visible — which is what lets a diagnostic tell "the catch-up job is being skipped
    because a full sync holds the lock" from "the catch-up job is dead". A crash is the
    other case worth recording, so the close happens in a ``finally``. Recording must never
    be the thing that breaks a run, so every failure here is swallowed.

    Yields an :class:`Outcome`. A caller whose command returns an exit code must assign it
    (``with ops.record(...) as run: run.code = func(args)``); one whose body raises on
    failure can ignore the value and keep the plain ``with`` form.
    """
    _counts.clear()
    started = datetime.now(timezone.utc)
    row_id = None
    try:
        conn = _connect(db)
        with conn:
            cursor = conn.execute(
                "INSERT INTO runs(command, scope, started, finished, ok, counts)"
                " VALUES (?,?,?,NULL,NULL,'{}')",
                (command, scope, started.isoformat()))
            row_id = cursor.lastrowid
            conn.execute(
                "DELETE FROM runs WHERE id NOT IN"
                " (SELECT id FROM runs ORDER BY id DESC LIMIT ?)", (MAX_ROWS,))
        conn.close()
    except Exception:
        pass

    outcome = Outcome()
    raised = False
    try:
        yield outcome
    except SystemExit as exc:
        outcome.code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None
                                                                   else 1)
        raise
    except BaseException:
        raised = True
        outcome.code = 1
        raise
    finally:
        if outcome.code == TIMED_OUT:
            _counts["timed_out"] = True
        # An exception is a failure whatever the code says; otherwise the returned status
        # is the answer, because it is the one the shell and any monitor already saw.
        ok = (not raised) and outcome.ok
        try:
            conn = _connect(db)
            with conn:
                if row_id is None:                 # the opening insert did not land
                    conn.execute(
                        "INSERT INTO runs(command, scope, started, finished, ok, counts,"
                        " exit_code) VALUES (?,?,?,?,?,?,?)",
                        (command, scope, started.isoformat(),
                         datetime.now(timezone.utc).isoformat(), int(ok),
                         json.dumps(_counts, default=str), outcome.code))
                else:
                    conn.execute(
                        "UPDATE runs SET finished = ?, ok = ?, counts = ?, exit_code = ?"
                        " WHERE id = ?",
                        (datetime.now(timezone.utc).isoformat(), int(ok),
                         json.dumps(_counts, default=str), outcome.code, row_id))
            conn.close()
        except Exception:
            pass


def running(command: str, scope: str | None = None,
            db: Path = DEFAULT_STATE_DB) -> bool:
    """Is a run of this command in flight right now?

    An unfinished row older than :data:`STALE_RUN` is treated as gone rather than running:
    a process killed with SIGKILL never gets to close its row, and a diagnostic that
    believed it forever would go quiet exactly when something had died hard.
    """
    cutoff = datetime.now(timezone.utc) - STALE_RUN
    for run in history(limit=MAX_ROWS, db=db, include_running=True):
        if run["finished"] or run["command"] != command:
            continue
        if scope is not None and run["scope"] != scope:
            continue
        try:
            if datetime.fromisoformat(run["started"]) > cutoff:
                return True
        except ValueError:
            continue
    return False


def history(limit: int = 20, db: Path = DEFAULT_STATE_DB,
            include_running: bool = False) -> list[dict]:
    """Recent runs, newest first.

    Returns [] for anything that goes wrong — a missing file, an unwritable directory, a
    table that is not there yet. ``coreyard status`` reads this, and status is the command
    people run when something is *already* broken; it must not be the second thing to fail.
    """
    conn = None
    try:
        conn = _connect(db)
        rows = conn.execute(
            "SELECT command, scope, started, finished, ok, counts, exit_code FROM runs"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()
    out = []
    for command, scope, started, finished, ok, counts, exit_code in rows:
        # An unfinished row is a run in flight. It is not an outcome, so by default it is
        # left out: "last full sync" must mean the last one that *ended*, or a run that is
        # still going would mask the result of the one before it.
        if finished is None and not include_running:
            continue
        try:
            parsed = json.loads(counts)
        except ValueError:
            parsed = {}
        out.append({"command": command, "scope": scope or "", "started": started,
                    "finished": finished, "ok": bool(ok), "counts": parsed,
                    "exit_code": exit_code, "running": finished is None})
    return out


def last_ok(command: str, scope: str | None = None,
            db: Path = DEFAULT_STATE_DB) -> str | None:
    """When the most recent *successful* run of a command finished, or None.

    Distinct from :func:`last`, which answers "what happened last time" and so reports a
    failure as the latest outcome. This answers "when did this last actually work", which is
    the question a staleness check has to ask: a job skipped by a held lock records a run
    like any other, and a check reading only the newest row would see a fresh timestamp for
    a job that has done nothing for days. That is precisely how the hourly reconcile went
    unnoticed from 2026-09-02 to 2026-09-06.

    Asked in SQL rather than by scanning :func:`history`, because a job that runs every
    minute floods the table and the row wanted here may be thousands back.
    """
    conn = None
    try:
        conn = _connect(db)
        sql = ("SELECT finished FROM runs WHERE command = ? AND ok = 1"
               " AND finished IS NOT NULL")
        values: tuple = (command,)
        if scope is not None:
            sql += " AND scope = ?"
            values += (scope,)
        row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", values).fetchone()
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()
    return row[0] if row else None


def last(command: str, scope: str | None = None,
         db: Path = DEFAULT_STATE_DB) -> dict | None:
    """The most recent *finished* run of one command (optionally one scope), or None."""
    for run in history(limit=MAX_ROWS, db=db):
        if run["command"] == command and (scope is None or run["scope"] == scope):
            return run
    return None
