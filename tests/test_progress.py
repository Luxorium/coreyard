"""UX-07: what a long run says about itself while it is still running.

A full sync spends minutes in two phases that printed nothing at all — one listing of a photo
share with 27,000 folders, one paged read of the yard. For that whole time a run that is
working and a run that is wedged produce the same output: none. The criterion asks for an
update at least every thirty seconds, with elapsed time, with processed-of-total where a
total is known, and with no invented completion estimate — a percentage derived from a total
nobody measured is a promise the run cannot keep.
"""

import contextlib
import io
import time
import unittest
import unittest.mock

from coreyard import progress
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


class HowMuchToSay(unittest.TestCase):
    """Quiet suppresses routine progress and nothing else; verbose adds per-item decisions.

    The line that must survive every level is the one saying a run failed. A mode that can
    hide why a run failed is not worth the flag it takes to turn on.
    """

    def setUp(self):
        self.addCleanup(progress.set_level, progress.NORMAL)

    def said(self, level, text, at=progress.NORMAL) -> str:
        progress.set_level(level)
        out = io.StringIO()
        progress.say(text, at=at, out=out)
        return out.getvalue()

    def test_routine_progress_is_kept_at_the_normal_level(self):
        self.assertIn("12/480", self.said(progress.NORMAL, "12/480"))

    def test_and_dropped_when_asked_for_quiet(self):
        self.assertEqual(self.said(progress.QUIET, "12/480"), "")

    def test_per_item_detail_needs_to_be_asked_for(self):
        self.assertEqual(self.said(progress.NORMAL, "R#51: published", at=progress.VERBOSE), "")
        self.assertIn("R#51", self.said(progress.VERBOSE, "R#51: published",
                                        at=progress.VERBOSE))

    def test_verbose_still_shows_the_routine_lines(self):
        self.assertIn("12/480", self.said(progress.VERBOSE, "12/480"))

    def test_the_heartbeat_is_routine_and_obeys_quiet(self):
        progress.set_level(progress.QUIET)
        out = io.StringIO()
        with waiting("reading the yard", every=0.02, out=out):
            time.sleep(0.06)
        self.assertEqual(out.getvalue(), "")

    def test_a_failure_is_not_routine_and_survives_quiet(self):
        import argparse
        import contextlib

        from coreyard import run_sync
        from coreyard.state import DiffResult

        progress.set_level(progress.QUIET)
        args = argparse.Namespace(dry_run=False, scope=None)
        diff = DiffResult(added=["1", "2"], changed=[], unchanged=[], removed=[])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                unittest.mock.patch.object(run_sync.ops, "count"):
            run_sync._summarise(diff, {"1"}, set(), set(), [object(), object()], args)
        printed = out.getvalue()
        self.assertIn("failed to publish", printed)
        self.assertIn("Sync complete", printed,
                      "the outcome is not narration: --quiet means stop telling me what "
                      "you are doing, not what you did")

    def test_machine_readable_output_implies_quiet(self):
        """A heartbeat interleaved with a JSON document makes it one neither a person nor
        `jq` can read, and `status --json | jq` is the reason the flag exists."""
        import argparse

        from coreyard import cli

        self.assertEqual(cli._verbosity(argparse.Namespace(json=True)), progress.QUIET)
        self.assertEqual(cli._verbosity(argparse.Namespace()), progress.NORMAL)

    def test_asking_for_both_is_answered_with_the_louder_one(self):
        """Somebody who passes `--verbose --json` has said which they want."""
        import argparse

        from coreyard import cli

        self.assertEqual(cli._verbosity(argparse.Namespace(json=True, verbose=True)),
                         progress.VERBOSE)

    def test_the_json_report_parses_with_progress_turned_on(self):
        """The end-to-end version: run it for real and hand the output to a parser."""
        import json
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        from coreyard.config import REPO_ROOT

        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("COREYARD_", "SMB_", "YMS_", "SHOPIFY_", "STORE_"))}
        with tempfile.TemporaryDirectory() as home:
            env["COREYARD_HOME"] = home
            env["PYTHONPATH"] = str(REPO_ROOT)
            subprocess.run([sys.executable, "-m", "coreyard", "init", "--demo"],
                           env=env, cwd=home, capture_output=True, timeout=180)
            result = subprocess.run(
                [sys.executable, "-m", "coreyard", "status", "--json"],
                env=env, cwd=home, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("counts", json.loads(result.stdout))

    def test_the_flags_are_mutually_exclusive(self):
        from coreyard import cli

        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.build_parser().parse_args(["--quiet", "--verbose", "status"])


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
