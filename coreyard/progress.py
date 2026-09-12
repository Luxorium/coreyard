"""Say something while a long phase is still running.

A full sync spends minutes in two places that print nothing: reading the yard, and listing
the photo share. From outside, a run that is working and a run that is wedged look identical
for that whole time — the same blank log, the same absent prompt — and the only way to tell
them apart is to wait and see which one ends. That is the question this answers, and it is
worth answering in a cron log as much as at a terminal: "still reading the yard (2m 10s)" on
the line before a traceback is what says the extract was the slow part.

Two rules, both from the release criterion this exists for. Report what is *known* —
elapsed, and processed-of-total only where a total is a fact rather than a guess — and never
invent a completion estimate: a percentage derived from a total nobody measured is a promise
the run has no way to keep.
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from typing import Optional

#: How long a phase may say nothing. The criterion asks for an update at least every 30
#: seconds; halving that leaves room for a line to be late without breaking the promise.
DEFAULT_INTERVAL = 15.0


def elapsed(seconds: float) -> str:
    """"45s", "2m 10s", "1h 4m" — the coarse-to-fine spelling people read at a glance."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class Phase:
    """One long operation, with a heartbeat while it runs and a line when it ends.

    The heartbeat is a background thread rather than a call inside the loop, because the
    phases that go quiet longest are the ones with no loop to instrument: a single query that
    takes four minutes, one `smbclient` listing of a share with 27,000 folders. A thread can
    speak for them without either of them knowing it exists.
    """

    def __init__(self, label: str, every: float = DEFAULT_INTERVAL, out=None,
                 total: Optional[int] = None) -> None:
        self.label = label
        self.every = every
        self.out = out or sys.stdout
        self.total = total
        self.done = 0
        self.started = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- what the phase reports about itself -------------------------------------------
    def advance(self, done: int) -> None:
        """Record progress. Never printed on its own — the heartbeat decides when to speak."""
        self.done = done

    @property
    def seconds(self) -> float:
        return time.monotonic() - self.started

    def line(self) -> str:
        counted = ""
        if self.done and self.total:
            counted = f" {self.done:,}/{self.total:,}"
        elif self.done:
            # No total, so no fraction and no percentage: the number read so far is a fact,
            # and everything else about the remainder would be invented.
            counted = f" {self.done:,} so far"
        return f"  still {self.label}{counted} ({elapsed(self.seconds)})"

    # -- the heartbeat ------------------------------------------------------------------
    def _beat(self) -> None:
        while not self._stop.wait(self.every):
            print(self.line(), file=self.out, flush=True)

    def start(self) -> "Phase":
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> float:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        return self.seconds


@contextmanager
def waiting(label: str, every: float = DEFAULT_INTERVAL, out=None,
            total: Optional[int] = None):
    """Run a block with a heartbeat. Yields the :class:`Phase` so it can report progress.

    Silent when the block is quick, which is most of them: the first line comes only after
    ``every`` seconds have passed with nothing to show for them.
    """
    phase = Phase(label, every=every, out=out, total=total).start()
    try:
        yield phase
    finally:
        phase.stop()
