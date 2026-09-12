"""UX-07: what a long run says about itself while it is still running.

A full sync spends minutes in two phases that printed nothing at all — one listing of a photo
share with 27,000 folders, one paged read of the yard. For that whole time a run that is
working and a run that is wedged produce the same output: none. The criterion asks for an
update at least every thirty seconds, with elapsed time, with processed-of-total where a
total is known, and with no invented completion estimate — a percentage derived from a total
nobody measured is a promise the run cannot keep.
"""

import io
import time
import unittest
import unittest.mock

from coreyard.progress import DEFAULT_INTERVAL, Phase, elapsed, waiting


class ElapsedReadsAtAGlance(unittest.TestCase):
    def test_seconds_minutes_and_hours(self):
        self.assertEqual(elapsed(0), "0s")
        self.assertEqual(elapsed(45), "45s")
        self.assertEqual(elapsed(130), "2m 10s")
        self.assertEqual(elapsed(3845), "1h 04m")

    def test_a_negative_clock_is_not_a_negative_duration(self):
        self.assertEqual(elapsed(-5), "0s")


class TheHeartbeatSaysWhatIsKnown(unittest.TestCase):
    def line(self, **kw) -> str:
        phase = Phase("reading the yard", out=io.StringIO(), **kw)
        phase.started -= 42                       # as if it had been running that long
        return phase.line()

    def test_it_names_the_phase_and_how_long_it_has_been_going(self):
        line = self.line()
        self.assertIn("still reading the yard", line)
        self.assertIn("42s", line)

    def test_a_known_total_is_reported_as_a_fraction(self):
        phase = Phase("publishing", out=io.StringIO(), total=480)
        phase.advance(12)
        self.assertIn("12/480", phase.line())

    def test_without_a_total_it_counts_what_it_has_and_stops_there(self):
        """No fraction, no percentage, no estimate: the number read so far is a fact and
        everything else about the remainder would be invented."""
        phase = Phase("reading the yard", out=io.StringIO())
        phase.advance(1500)
        line = phase.line()
        self.assertIn("1,500 so far", line)
        self.assertNotIn("%", line)
        self.assertNotIn("remaining", line)

    def test_it_says_nothing_about_a_phase_that_has_done_nothing_yet(self):
        self.assertNotIn("so far", self.line())


class ItSpeaksWhileTheBlockRuns(unittest.TestCase):
    def test_a_slow_block_is_reported_more_than_once(self):
        out = io.StringIO()
        with waiting("reading the yard", every=0.02, out=out):
            time.sleep(0.12)
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(all("still reading the yard" in line for line in lines))

    def test_a_quick_block_says_nothing(self):
        """Most phases are quick, and a line per phase would be noise in every log."""
        out = io.StringIO()
        with waiting("reading the yard", every=5.0, out=out):
            pass
        self.assertEqual(out.getvalue(), "")

    def test_progress_recorded_during_the_block_reaches_the_line(self):
        out = io.StringIO()
        with waiting("publishing", every=0.02, out=out, total=3) as phase:
            phase.advance(2)
            time.sleep(0.06)
        self.assertIn("2/3", out.getvalue())

    def test_the_heartbeat_stops_when_the_block_does(self):
        out = io.StringIO()
        with waiting("reading the yard", every=0.02, out=out):
            time.sleep(0.05)
        after = out.getvalue()
        time.sleep(0.08)
        self.assertEqual(out.getvalue(), after, "the thread outlived its phase")

    def test_an_exception_still_stops_it(self):
        out = io.StringIO()
        with self.assertRaises(RuntimeError):
            with waiting("reading the yard", every=0.02, out=out):
                raise RuntimeError("the yard refused")
        after = out.getvalue()
        time.sleep(0.06)
        self.assertEqual(out.getvalue(), after)

    def test_the_default_interval_keeps_the_promise(self):
        """The criterion asks for an update at least every thirty seconds."""
        self.assertLessEqual(DEFAULT_INTERVAL, 30)


class TheRunItselfIsInstrumented(unittest.TestCase):
    def test_every_phase_that_can_go_quiet_for_minutes_is_wrapped(self):
        """The three that do: the share listing, the yard read, and the publish loop."""
        import inspect

        from coreyard import run_sync

        source = inspect.getsource(run_sync)
        for label in ("listing the photo share", "reading the yard", "publishing"):
            with self.subTest(phase=label):
                self.assertIn(f'waiting("{label}"', source)

    def test_a_completed_run_reports_how_long_it_took(self):
        import argparse
        import contextlib

        from coreyard import run_sync
        from coreyard.state import DiffResult

        args = argparse.Namespace(dry_run=False, scope=None)
        diff = DiffResult(added=["1"], changed=[], unchanged=[], removed=[])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                unittest.mock.patch.object(run_sync.ops, "count"):
            run_sync._summarise(diff, {"1"}, set(), set(), [object()], args, seconds=252)
        self.assertIn("Sync complete in 4m 12s", out.getvalue())


if __name__ == "__main__":
    unittest.main()
