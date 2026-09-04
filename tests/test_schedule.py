"""Scheduled commands take one lock and remain visible to the status command."""

import unittest

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
