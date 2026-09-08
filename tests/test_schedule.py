"""Scheduled commands take one lock and remain visible to the status command."""

import unittest
from unittest import mock

from coreyard import schedule


class ScheduledCommand(unittest.TestCase):
    def test_generated_command_does_not_deadlock_itself_with_two_lock_owners(self):
        command = schedule.command_for("counts --apply")
        self.assertIn("--lock counts counts --apply", command)
        self.assertNotIn("flock -n", command)

    def test_tasks_do_not_block_unrelated_work(self):
        self.assertIn("--lock sync sync delta", schedule.command_for("sync delta"))
        self.assertIn("--lock orders orders poll", schedule.command_for("orders poll"))

    def test_managed_cron_entry_carries_the_status_marker(self):
        original_read = schedule._crontab_lines
        original_write = schedule._write_crontab
        captured = []
        self.addCleanup(setattr, schedule, "_crontab_lines", original_read)
        self.addCleanup(setattr, schedule, "_write_crontab", original_write)
        schedule._crontab_lines = lambda: []
        schedule._write_crontab = lambda lines: captured.extend(lines)

        schedule.install_cron("counts --apply", 60, dry_run=False)

        entries = [line for line in captured if not line.startswith("#")]
        self.assertEqual(len(entries), 1)
        self.assertIn(f"{schedule.NAME}:counts", entries[0])

    def test_install_removes_the_old_self_deadlocking_entry(self):
        old = (f"*/1 * * * * cd {schedule.REPO_ROOT} && flock -n {schedule.LOCK} "
               f"{schedule.REPO_ROOT}/bin/coreyard --lock sync counts --apply "
               f">> {schedule.LOG} 2>&1")
        original_read = schedule._crontab_lines
        original_write = schedule._write_crontab
        captured = []
        self.addCleanup(setattr, schedule, "_crontab_lines", original_read)
        self.addCleanup(setattr, schedule, "_write_crontab", original_write)
        schedule._crontab_lines = lambda: ["# keep me", old]
        schedule._write_crontab = lambda lines: captured.extend(lines)

        schedule.install_cron("counts --apply", 60, dry_run=False)

        self.assertIn("# keep me", captured)
        self.assertNotIn(old, captured)

    def test_installing_sync_does_not_remove_the_separate_counter(self):
        counter = (f"*/1 * * * * cd {schedule.REPO_ROOT} && "
                   f"{schedule.command_for('counts --apply')} >> {schedule.LOG} 2>&1 "
                   f"# {schedule.NAME}:counts")
        original_read = schedule._crontab_lines
        original_write = schedule._write_crontab
        captured = []
        self.addCleanup(setattr, schedule, "_crontab_lines", original_read)
        self.addCleanup(setattr, schedule, "_write_crontab", original_write)
        schedule._crontab_lines = lambda: [counter]
        schedule._write_crontab = lambda lines: captured.extend(lines)

        schedule.install_cron("sync", 300, dry_run=False)

        self.assertIn(counter, captured)
        self.assertTrue(any(f"{schedule.NAME}:sync" in line for line in captured))


if __name__ == "__main__":
    unittest.main()


class ALockHeldPastItsOwnPeriod(unittest.TestCase):
    """The configuration error that cost four days, and the one place it is visible.

    The delta sync ran every 5 minutes with a 4-minute deadline, sharing `.sync.lock` with
    the hourly full sync and the half-hourly reconcile. A run that used its budget was still
    holding the lock when the next tick fired — every tick — so the other two never ran
    again. Nothing looked wrong: the delta ran on schedule and the starved jobs logged
    "Another run holds .sync.lock; skipping this one" and exited 0.

    The rule is `hold < period`. It lives only in the crontab, where no test, diff or review
    can reach it — so this reads the crontab.
    """

    SYNC = ("0 * * * * cd /srv/x && /usr/bin/timeout 50m /srv/x/bin/coreyard "
            "--lock sync --timeout 45m sync --status ACTIVE")
    DELTA = ("*/5 * * * * cd /srv/x && /usr/bin/timeout 5m /srv/x/bin/coreyard "
             "--lock sync --timeout 4m sync delta")
    RECONCILE = ("30 * * * * cd /srv/x && /usr/bin/timeout 28m /srv/x/bin/coreyard "
                 "--lock sync --timeout 25m reconcile --apply")
    ORDERS = ("*/10 * * * * cd /srv/x && /usr/bin/timeout 10m /srv/x/bin/coreyard "
              "--lock orders --timeout 9m orders poll --write-orders")

    def budget(self, lines):
        with mock.patch.object(schedule.shutil, "which", return_value="/usr/bin/crontab"), \
                mock.patch.object(schedule, "_crontab_lines", return_value=lines):
            return schedule.lock_budget()

    def test_a_job_that_finishes_inside_its_period_is_not_reported(self):
        self.assertEqual(self.budget([self.ORDERS]), [])

    def test_the_delta_configuration_that_caused_the_outage_is_caught(self):
        """4 minutes of every 5, against the sync and reconcile it shares .sync.lock with.

        Keyed by its budget, not by its task: every job on `.sync.lock` reports the lock as
        its task, which is exactly the ambiguity that made this configuration hard to see in
        the crontab in the first place.
        """
        found = self.budget([self.DELTA, self.SYNC, self.RECONCILE])
        delta = [job for job in found if job["hold"] == 240]
        self.assertEqual(len(delta), 1, found)
        self.assertEqual(delta[0]["period"], 300)
        self.assertTrue(delta[0]["shared"])

    def test_it_is_not_enough_to_stay_inside_your_own_period(self):
        """The whole reason the threshold is half. The delta never overran its period — it
        held 80% of every window, and the jobs that fire once and twice an hour never won
        the lock. A rule of `hold >= period` would have called that configuration healthy
        for the entire four days it was starving the storefront."""
        self.assertLess(240, 300)                       # the delta never overran
        contended = self.budget([self.DELTA, self.SYNC, self.RECONCILE])
        self.assertTrue(any(job["hold"] == 240 for job in contended))

    def test_a_job_alone_on_its_lock_is_judged_only_on_its_own_ticks(self):
        """Nothing else wants it, so holding 80% of the period costs nobody anything."""
        self.assertEqual(self.budget([self.DELTA]), [])

    def test_the_hourly_sync_is_named_too_not_just_the_catch_up(self):
        """45 minutes of every 60, on the same lock reconcile needs at :30. The delta was
        the trigger; this is why reconcile stayed starved."""
        found = {job["task"]: job for job in self.budget([self.SYNC, self.RECONCILE])}
        self.assertIn("sync", found)
        self.assertEqual(found["sync"]["hold"], 2700)

    def test_two_jobs_on_one_lock_starve_each_other(self):
        """Both eBay workers run every 5 minutes with a 14-minute deadline on `.ebay.lock`
        — found live in this installation's crontab by this check."""
        ebay = ["*/5 * * * * /srv/x/bin/coreyard --lock ebay --timeout 14m ebay prices",
                "2-57/5 * * * * /srv/x/bin/coreyard --lock ebay --timeout 14m ebay titles"]
        found = self.budget(ebay)
        self.assertEqual(len(found), 2)
        self.assertTrue(all(job["shared"] and job["hold"] == 840 for job in found))

    def test_a_commented_out_job_holds_nothing(self):
        self.assertEqual(self.budget(["#" + self.DELTA]), [])

    def test_the_outer_timeout_bounds_a_job_with_no_deadline_of_its_own(self):
        """`timeout(1)` is what releases the lock when the tool has no `--timeout`."""
        line = ("*/5 * * * * /usr/bin/timeout --signal=TERM --kill-after=30s 9m "
                "/srv/x/bin/coreyard --lock sync sync delta")
        found, = self.budget([line])
        self.assertEqual(found["hold"], 540)

    def test_a_line_running_no_coreyard_job_is_ignored(self):
        self.assertEqual(self.budget(["*/5 * * * * /usr/bin/backup.sh"]), [])

    def test_an_unreadable_duration_is_skipped_rather_than_guessed(self):
        line = "*/5 * * * * /srv/x/bin/coreyard --lock sync --timeout later sync delta"
        self.assertEqual(self.budget([line]), [])
