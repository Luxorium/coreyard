"""UX-04: the number a scheduled job actually reads.

A cron line's `||` branch, a systemd `OnFailure=`, a monitor's alert rule — none of them read
the message. They read the status, so the status has to mean the same thing on every command
and there have to be few enough of them to write down: `0` did it or deliberately did
nothing, `1` tried and failed, `2` refused before doing anything, `124` ran out of its
deadline and stopped cleanly.

The last one matters most for this installation, because a refusal and a failure want
different responses. `2` means nothing changed and re-running changes nothing again — go and
fix the configuration. `1` means something was attempted, and the next scheduled run will
retry whatever is retryable.
"""

import argparse
import ast
import contextlib
import io
import os
import pathlib
import tempfile
import unittest
import unittest.mock as mock

from coreyard import cli

REPO = pathlib.Path(__file__).resolve().parent.parent


class TheSetIsClosed(unittest.TestCase):
    """Four codes, documented. A fifth would be a contract change nobody announced."""

    DOCUMENTED = {0, 1, 2, 124}

    def statuses(self) -> set[int]:
        found = set()
        for path in sorted((REPO / "coreyard").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, int)):
                    found.add(node.value.value)
                if (isinstance(node, ast.Call)
                        and getattr(node.func, "id", "") == "SystemExit"
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, int)):
                    found.add(node.args[0].value)
        return found

    def test_the_code_produces_only_documented_statuses(self):
        undocumented = self.statuses() - self.DOCUMENTED
        self.assertEqual(undocumented, set(),
                         f"these statuses are returned and not documented: {undocumented}")

    def test_each_documented_status_is_in_the_runbook(self):
        runbook = (REPO / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        section = runbook.split("### Exit codes", 1)[1].split("###", 1)[0]
        for code in sorted(self.DOCUMENTED):
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", section)


class TwoIsARefusal(unittest.TestCase):
    """Nothing was changed, and re-running without fixing anything changes nothing again."""

    def test_a_capability_this_installation_lacks(self):
        out = io.StringIO()
        caps = mock.Mock(source_spec="tabular:/tmp/parts.csv", source_kind="tabular")
        with contextlib.redirect_stderr(out):
            code = cli._refuse("sync delta", [("delta", "no modified_at mapping")], caps)
        self.assertEqual(code, 2)
        self.assertIn("Nothing was changed", out.getvalue())

    def test_a_snapshot_belonging_to_another_store(self):
        from coreyard.state import SyncState

        with tempfile.TemporaryDirectory() as directory:
            db = pathlib.Path(directory) / "state.sqlite3"
            with SyncState(db) as state:
                state.claim("elsewhere.myshopify.com", "coreyard")
            with mock.patch.object(cli, "_installation",
                                   return_value=("mine.myshopify.com", "coreyard")), \
                    mock.patch("coreyard.state.DEFAULT_STATE_DB", db), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = cli._snapshot_moved("sync", argparse.Namespace(dry_run=False))
        self.assertEqual(code, 2)

    def test_a_source_older_than_this_installation_allows(self):
        from datetime import timedelta

        from coreyard import source as source_mod

        stale = source_mod.Freshness(timedelta(hours=40), timedelta(hours=24), True,
                                     "tabular: 40.0h old, limit 24h")
        with mock.patch.object(source_mod, "freshness", return_value=stale), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli._source_too_old("sync", argparse.Namespace(dry_run=False))
        self.assertEqual(code, 2)

    def test_a_flag_that_cannot_apply_to_this_command(self):
        from coreyard import run_sync

        from tests.test_sync_plan import sync_args

        with contextlib.redirect_stderr(io.StringIO()):
            code = run_sync.cmd_delta(sync_args(["delta", "--r-number", "51"]))
        self.assertEqual(code, 2)


class ZeroIsAlsoDeliberatelyDoingNothing(unittest.TestCase):
    def test_a_busy_lock_skips_the_tick(self):
        """The catch-up shares the full sync's lock on purpose, so a monitor that treated a
        skipped tick as a failure would page every hour for the system working."""
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            out_dir = pathlib.Path(directory) / "out"
            out_dir.mkdir()
            held = out_dir / ".sync.lock"
            handle = held.open("w")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with mock.patch("coreyard.cli.out_dir", return_value=out_dir), \
                        mock.patch("coreyard.ops.skipped"), \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = cli.main(["--lock", "sync", "status"])
            finally:
                handle.close()
        self.assertEqual(code, 0)

    def test_a_dry_run_that_would_have_changed_things(self):
        """`--dry-run` reports; reporting is a success even when the report is alarming."""
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("COREYARD_", "SMB_", "YMS_", "SHOPIFY_", "STORE_"))}
        with tempfile.TemporaryDirectory() as home:
            env["COREYARD_HOME"] = home
            env["PYTHONPATH"] = str(REPO)
            subprocess.run([sys.executable, "-m", "coreyard", "init", "--demo"],
                           env=env, cwd=home, capture_output=True, timeout=180)
            done = subprocess.run(
                [sys.executable, "-m", "coreyard", "sync", "--sink", "csv", "--dry-run"],
                env=env, cwd=home, capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class OneTwentyFourIsTheDeadline(unittest.TestCase):
    def test_the_timeout_exits_the_status_gnu_timeout_uses(self):
        """So a monitor that already understands the crontab's `timeout` reads CoreYard's
        own deadline the same way."""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                with cli._deadline(1):
                    os.kill(os.getpid(), __import__("signal").SIGALRM)
        self.assertEqual(caught.exception.code, 124)

    def test_a_run_stopped_that_way_is_not_recorded_as_a_failure(self):
        """It kept everything it banked, so reporting it FAILED buries the runs that broke."""
        from coreyard import ops

        with tempfile.TemporaryDirectory() as directory:
            db = pathlib.Path(directory) / "state.sqlite3"
            with self.assertRaises(SystemExit):
                with ops.record("sync", "", db=db):
                    raise SystemExit(124)
            run = ops.last("sync", "", db=db)
        self.assertTrue(run["ok"])
        self.assertTrue(run["counts"].get("timed_out"))
        self.assertEqual(run["exit_code"], 124)


if __name__ == "__main__":
    unittest.main()
