"""Run history, and the two commands an operator reaches for first.

`status` and `doctor` are the commands people run *when something is already wrong*, so the
property that matters most is that they still produce a report when a dependency is down.
A status page that raises because Shopify is unreachable is useless exactly when it is
needed.
"""

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from coreyard import doctor, ops, status
from coreyard.state import SyncState, fingerprints_all, subset

from tests.test_sync_plan import NO_IMG, STORE, part


class RunHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_successful_run_is_recorded_with_its_counts(self):
        with ops.record("sync", "inventory", db=self.db):
            ops.count(created=3, retired=1)
        run = ops.last("sync", "inventory", db=self.db)
        self.assertTrue(run["ok"])
        self.assertEqual(run["counts"], {"created": 3, "retired": 1})

    def test_a_crashing_run_is_still_recorded(self):
        """An unrecorded failure is indistinguishable from a job nobody scheduled."""
        with self.assertRaises(RuntimeError):
            with ops.record("sync", db=self.db):
                raise RuntimeError("the database went away")
        self.assertFalse(ops.last("sync", db=self.db)["ok"])

    def test_counts_do_not_leak_between_runs(self):
        with ops.record("sync", db=self.db):
            ops.count(created=99)
        with ops.record("reconcile", db=self.db):
            pass
        self.assertEqual(ops.last("reconcile", db=self.db)["counts"], {})

    def test_scopes_are_remembered_separately(self):
        with ops.record("sync", "delta", db=self.db):
            ops.count(created=1)
        with ops.record("sync", "", db=self.db):
            ops.count(created=2)
        self.assertEqual(ops.last("sync", "delta", db=self.db)["counts"]["created"], 1)
        self.assertEqual(ops.last("sync", "", db=self.db)["counts"]["created"], 2)

    def test_history_survives_an_unusable_database(self):
        self.assertEqual(ops.history(db=Path("/nonexistent/dir/state.sqlite3")), [])

    def test_recording_never_breaks_the_run_it_is_recording(self):
        with ops.record("sync", db=Path("/nonexistent/dir/state.sqlite3")):
            ops.count(created=1)


class InterruptedRun(unittest.TestCase):
    """A publish run that is stopped must keep what it published.

    Without this the scheduled job cannot work off a backlog larger than one timeout
    window: each run publishes what it can, records nothing, and the next run starts from
    the beginning — which is what an installation here did for twelve consecutive hours.
    """

    def test_checkpointed_progress_survives_a_run_that_never_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            parts = [part(str(i)) for i in range(10)]
            with SyncState(db) as state:
                fingerprints = fingerprints_all(parts, NO_IMG, STORE)
                self.assertEqual(len(state.diff(fingerprints).added), 10)
                # The run publishes four parts, then is killed: no commit ever happens.
                state.update(subset(fingerprints, {"0", "1", "2", "3"}))

            with SyncState(db) as state:
                diff = state.diff(fingerprints_all(parts, NO_IMG, STORE))
                self.assertEqual(len(diff.unchanged), 4, "the four already done")
                self.assertEqual(len(diff.added), 6, "only the remainder is retried")

    def test_a_part_that_failed_to_publish_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            parts = [part("1"), part("2")]
            with SyncState(db) as state:
                fingerprints = fingerprints_all(parts, NO_IMG, STORE)
                state.update(subset(fingerprints, {"1"}))     # R#2 raised
                self.assertEqual(state.diff(fingerprints).added, ["2"])


class Status(unittest.TestCase):
    def test_a_dead_dependency_becomes_a_line_not_a_traceback(self):
        def boom():
            raise RuntimeError("connection refused")

        label, detail = status._probe("Shopify", boom)
        self.assertEqual(label, "Shopify")
        self.assertIn("FAILED", detail)
        self.assertIn("connection refused", detail)

    def test_the_report_renders_with_everything_missing(self):
        report = {"reachability": [("Shopify", "FAILED — down")], "counts": {},
                  "pending": {}, "runs": {"Last full sync": None}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status._render(report)
        self.assertIn("FAILED", out.getvalue())
        self.assertIn("never", out.getvalue())

    def test_a_dry_run_never_reads_as_a_completed_sync(self):
        """The one line here somebody could act on wrongly: "last full sync, ok" is what
        you check before deciding the pipeline is healthy."""
        report = {"reachability": [], "counts": {}, "pending": {},
                  "runs": {"Last full sync": {"when": "13m ago", "ok": True,
                                              "counts": {"dry_run": True, "created": 0}}}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status._render(report)
        self.assertIn("dry run", out.getvalue())

    def test_a_real_run_shows_its_counts(self):
        report = {"reachability": [], "counts": {}, "pending": {},
                  "runs": {"Last full sync": {"when": "5m ago", "ok": True,
                                              "counts": {"created": 12, "retired": 3}}}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status._render(report)
        self.assertIn("created=12", out.getvalue())
        self.assertIn("ok", out.getvalue())

    def test_ages_are_readable_and_never_raise(self):
        self.assertEqual(status._ago(None), "never")
        self.assertEqual(status._ago("not a timestamp"), status.UNKNOWN)

    def test_status_is_read_only_and_advisory(self):
        """It must not exit non-zero: having an opinion is `doctor`'s job."""
        parser = argparse.ArgumentParser()
        status.add_arguments(parser)
        self.assertFalse(parser.parse_args([]).deep)


class Doctor(unittest.TestCase):
    def test_a_check_that_itself_fails_does_not_hide_the_others(self):
        def boom():
            raise RuntimeError("nope")

        def fine():
            return [(doctor.OK, "fine", "yes")]

        results = doctor.collect([boom, fine])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1], (doctor.OK, "fine", "yes"))
        self.assertEqual(results[0][0], doctor.FAIL)

    def test_the_verdict_is_the_worst_result(self):
        self.assertEqual(doctor.verdict([(doctor.OK, "a", "")]), doctor.OK)
        self.assertEqual(doctor.verdict(
            [(doctor.OK, "a", ""), (doctor.WARN, "b", "")]), doctor.WARN)
        self.assertEqual(doctor.verdict(
            [(doctor.WARN, "a", ""), (doctor.FAIL, "b", "")]), doctor.FAIL)

    def test_the_exit_code_ranks_healthy_degraded_failed(self):
        self.assertEqual(doctor.RANK[doctor.OK], 0)
        self.assertEqual(doctor.RANK[doctor.WARN], 1)
        self.assertEqual(doctor.RANK[doctor.FAIL], 2)

    def test_an_empty_report_is_healthy_rather_than_an_error(self):
        self.assertEqual(doctor.verdict([]), doctor.OK)

    def test_the_environment_check_needs_no_network_or_env(self):
        for level, _name, _detail in doctor.check_environment():
            self.assertIn(level, (doctor.OK, doctor.WARN, doctor.FAIL))


if __name__ == "__main__":
    unittest.main()
