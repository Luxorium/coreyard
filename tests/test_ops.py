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
import unittest.mock
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


class CryWolf(unittest.TestCase):
    """Three ways the diagnostics reported a non-problem, each fixed.

    They matter together: a check that stays lit for something already fixed, or for
    something working as designed, is how people learn to stop reading the output.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_run_in_flight_is_visible_and_does_not_count_as_an_outcome(self):
        with ops.record("sync", "", db=self.db):
            self.assertTrue(ops.running("sync", "", db=self.db))
            self.assertIsNone(ops.last("sync", "", db=self.db),
                              "a run still going must not mask the one before it")
        self.assertFalse(ops.running("sync", "", db=self.db))
        self.assertTrue(ops.last("sync", "", db=self.db)["ok"])

    def test_a_killed_run_stops_counting_as_running(self):
        """SIGKILL never lets a run close its row. Believing it forever would go quiet
        exactly when something had died hard."""
        import sqlite3
        from datetime import datetime, timedelta, timezone

        with ops.record("sync", "", db=self.db):
            pass
        long_ago = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("INSERT INTO runs(command, scope, started, finished, ok, counts)"
                         " VALUES ('sync','',?,NULL,NULL,'{}')", (long_ago,))
        conn.close()
        self.assertFalse(ops.running("sync", "", db=self.db))

    def test_a_deadline_stop_is_not_reported_as_a_failure(self):
        with self.assertRaises(SystemExit):
            with ops.record("sync", "", db=self.db):
                raise SystemExit(124)
        run = ops.last("sync", "", db=self.db)
        self.assertTrue(run["counts"].get("timed_out"))

        report = {"reachability": [], "counts": {}, "pending": {},
                  "runs": {"Last full sync": {"when": "0m ago", "ok": False,
                                              "counts": {"timed_out": True,
                                                         "created": 40}}}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status._render(report)
        self.assertIn("timed out", out.getvalue())
        self.assertNotIn("FAILED", out.getvalue())

    def test_a_command_that_returns_nonzero_is_not_recorded_as_a_success(self):
        """OPS-01, the observed `exit=2` / `ok=1` defect.

        A command reports failure by *returning* a code, not by raising. The recorder saw
        only that the `with` block ended, so a sync that exited 2 was written to the run
        history as ok — and `coreyard status` then reported the last full sync as
        successful while the shell had already reported it as a failure. A status page that
        contradicts the exit code is worse than no status page: it is the one an operator
        believes.
        """
        with ops.record("sync", "", db=self.db) as run:
            run.code = 2
        recorded = ops.last("sync", "", db=self.db)
        self.assertFalse(recorded["ok"])
        self.assertEqual(recorded["exit_code"], 2)

    def test_a_command_that_returns_zero_is_recorded_as_a_success(self):
        with ops.record("sync", "", db=self.db) as run:
            run.code = 0
        recorded = ops.last("sync", "", db=self.db)
        self.assertTrue(recorded["ok"])
        self.assertEqual(recorded["exit_code"], 0)

    def test_the_deadline_code_is_recorded_but_is_not_a_failure(self):
        """124 is the deadline the run was given, not a fault."""
        with ops.record("sync", "", db=self.db) as run:
            run.code = ops.TIMED_OUT
        recorded = ops.last("sync", "", db=self.db)
        self.assertTrue(recorded["ok"])
        self.assertTrue(recorded["counts"].get("timed_out"))

    def test_an_exception_beats_whatever_code_was_assigned(self):
        with self.assertRaises(RuntimeError):
            with ops.record("sync", "", db=self.db) as run:
                run.code = 0
                raise RuntimeError("fell over after saying it was fine")
        self.assertFalse(ops.last("sync", "", db=self.db)["ok"])

    def test_a_row_written_before_the_column_existed_still_reads(self):
        """`exit_code` was added after the table shipped. An old row reports None rather
        than 0, because "not recorded" and "exited cleanly" are different answers."""
        import sqlite3

        with ops.record("sync", "", db=self.db) as run:
            run.code = 0
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET exit_code = NULL")
        conn.close()
        self.assertIsNone(ops.last("sync", "", db=self.db)["exit_code"])

    def test_the_cli_hands_the_command_result_to_the_recorder(self):
        """The fix only works if `cli.main` actually assigns it, so assert the wiring."""
        import argparse

        from coreyard import cli

        calls = {}

        @contextlib.contextmanager
        def fake_record(command, scope="", db=None):
            outcome = ops.Outcome()
            yield outcome
            calls["code"] = outcome.code

        parser = argparse.ArgumentParser()
        parser.set_defaults(command="sync", func=lambda args: 2, lock=None, timeout=None,
                            scope="")
        with unittest.mock.patch.object(cli, "build_parser", lambda: parser), \
                unittest.mock.patch.object(cli.ops, "record", fake_record), \
                unittest.mock.patch.object(cli.ops, "run_header", lambda *a, **k: ""):
            self.assertEqual(cli.main([]), 2)
        self.assertEqual(calls["code"], 2, "the exit code never reached the run history")

    def test_a_real_failure_is_still_reported_as_one(self):
        with self.assertRaises(RuntimeError):
            with ops.record("sync", "", db=self.db):
                raise RuntimeError("the database went away")
        run = ops.last("sync", "", db=self.db)
        self.assertFalse(run["ok"])
        self.assertFalse(run["counts"].get("timed_out"))

    def test_doctor_reads_only_the_most_recent_run_from_a_log(self):
        """An error fixed days ago sat in the tail window and was re-reported until enough
        traffic pushed it out — a week, for a job that prints four lines a tick."""
        import shutil
        from datetime import timedelta

        log_dir = Path(self.tmp.name) / "out"
        log_dir.mkdir()
        (log_dir / "orders.log").write_text(
            f"{ops.run_header('orders')}\n"
            "ImportError: cannot import name 'GONE' from 'coreyard.orders.pipeline'\n"
            f"{ops.run_header('orders')}\n"
            "1 order(s) in the window, 0 newly queued and handled.\n",
            encoding="utf-8")
        original = doctor.LOG_DIR
        try:
            doctor.LOG_DIR = log_dir
            self.assertEqual(doctor.check_logs(timedelta(hours=2)), [])
        finally:
            doctor.LOG_DIR = original
            shutil.rmtree(log_dir, ignore_errors=True)

    def test_doctor_still_reports_an_error_in_the_current_run(self):
        import shutil
        from datetime import timedelta

        log_dir = Path(self.tmp.name) / "out2"
        log_dir.mkdir()
        (log_dir / "orders.log").write_text(
            f"{ops.run_header('orders')}\n"
            "1 order(s) in the window.\n"
            f"{ops.run_header('orders')}\n"
            "Traceback (most recent call last):\n",
            encoding="utf-8")
        original = doctor.LOG_DIR
        try:
            doctor.LOG_DIR = log_dir
            findings = doctor.check_logs(timedelta(hours=2))
            self.assertEqual(len(findings), 1)
            self.assertIn("Traceback", findings[0][2])
        finally:
            doctor.LOG_DIR = original
            shutil.rmtree(log_dir, ignore_errors=True)
