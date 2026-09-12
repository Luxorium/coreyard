"""SEC-03: where a customer's order body can end up, and for how long.

An order payload is a name, a phone number and a street address. It is written in exactly one
place, cleared as soon as it is handled, and kept for a bounded window when a stage fails so
that stage can be retried. The clauses below are the ones that are easy to *believe* and
cheap to be wrong about: a database that is `0600` while its journal is not, a backup that is
world-readable for the second before `chmod` runs, a diagnostic that prints the token it was
checking.

What this deliberately does not claim is forensic erasure. `PRAGMA secure_delete` overwrites
the bytes inside the database file, which is what an application can do; a filesystem that
relocates pages and a backup taken before the clear may still hold a copy, and the
documentation says so rather than implying otherwise.
"""

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from coreyard.orders.pipeline import EventQueue

REPO = Path(__file__).resolve().parent.parent

ORDER = {"id": 9001, "name": "#1042", "financial_status": "paid",
         "customer": {"first_name": "Alex", "last_name": "Rivera"},
         "shipping_address": {"address1": "17 Colvin Ave", "phone": "555-0143"},
         "line_items": [{"sku": "51", "quantity": 1}]}
SECRET_IN_BODY = "17 Colvin Ave"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TheQueueIsOwnerOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "queue.sqlite3"
        self.queue = EventQueue(self.path)
        self.addCleanup(self.queue.close)

    def test_the_database_is_0600(self):
        self.assertEqual(mode(self.path), 0o600)

    def test_so_is_the_journal_it_writes_beside_it(self):
        """The rollback journal holds the same bytes the row does. SQLite gives it the
        database's mode, which is a property of SQLite rather than of this code — so it is
        checked here, where changing the journal mode would have to come past it."""
        self.queue.conn.execute("BEGIN")
        self.queue.conn.execute(
            "INSERT INTO events(webhook_id, topic, received_at, payload, state)"
            " VALUES ('x','orders/paid','now',?,'pending')", (json.dumps(ORDER),))
        sidecars = [p for p in Path(self.tmp.name).iterdir() if p.name != self.path.name]
        self.assertTrue(sidecars, "expected a journal while a transaction is open")
        for sidecar in sidecars:
            with self.subTest(file=sidecar.name):
                self.assertEqual(mode(sidecar), 0o600)
        self.queue.conn.rollback()

    def test_a_handled_payload_leaves_no_copy_in_the_file(self):
        self.queue.add("wh-1", "orders/paid", json.dumps(ORDER).encode(), "order-1042")
        self.queue.finish("wh-1", "#1042", ["51"], work_order="2075")
        self.assertNotIn(SECRET_IN_BODY.encode(), self.path.read_bytes())

    def test_what_is_kept_is_the_evidence_rather_than_the_person(self):
        self.queue.add("wh-1", "orders/paid", json.dumps(ORDER).encode(), "order-1042")
        self.queue.finish("wh-1", "#1042", ["51"], work_order="2075")
        row = self.queue.conn.execute(
            "SELECT payload, order_name, work_order FROM events").fetchone()
        self.assertIsNone(row[0])
        self.assertEqual((row[1], row[2]), ("#1042", "2075"))


class TheBackupIsOwnerOnly(unittest.TestCase):
    """The one copy of all of it in one directory: state, queue and `.env`.

    Run for real rather than read, because what is checked is the script's behaviour under a
    umask it does not control, and a shell script's file modes are not obvious from reading
    it. These pin the *finished* backup. The `umask 077` at the top of the script is about
    the moment before that — a file exists between its creation and the closing `chmod`, and
    no assertion made afterwards can see it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "out").mkdir()
        shutil.copy(REPO / "scripts" / "backup_state.sh", self.repo / "scripts")
        (self.repo / ".env").write_text("SHOPIFY_ADMIN_TOKEN=shpat_notreal\n")
        (self.repo / ".env").chmod(0o600)
        for name in ("coreyard_sync_state.sqlite3", "coreyard_webhook_queue.sqlite3"):
            conn = sqlite3.connect(self.repo / name)
            with conn:
                conn.execute("CREATE TABLE parts (r_number TEXT)")
                conn.execute("CREATE TABLE retired (r_number TEXT)")
            conn.close()
        self.dest = Path(self.tmp.name) / "backups"

    def run_backup(self, umask: str = "0022"):
        return subprocess.run(
            ["bash", "-c", f"umask {umask}; exec scripts/backup_state.sh"],
            cwd=self.repo, capture_output=True, text=True,
            env={**os.environ, "BACKUP_DIR": str(self.dest),
                 "COREYARD_PYTHON": "python3"})

    def test_every_file_it_writes_is_owner_only(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        written = [p for p in self.dest.rglob("*") if p.is_file()]
        self.assertTrue(written, result.stdout + result.stderr)
        for path in written:
            with self.subTest(file=path.name):
                self.assertEqual(mode(path) & 0o077, 0, f"{path.name} is group/world readable")

    def test_the_finished_backup_is_owner_only_whatever_the_caller_permits(self):
        """A cron job inherits whatever umask the shell it runs under has, which on a fresh
        host is not necessarily a strict one."""
        self.run_backup(umask="0000")
        for path in (p for p in self.dest.rglob("*") if p.is_file()):
            with self.subTest(file=path.name):
                self.assertEqual(mode(path) & 0o077, 0)

    def test_the_secrets_file_is_carried_across_as_it_was(self):
        self.run_backup()
        copies = list(self.dest.rglob(".env"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(mode(copies[0]), 0o600)


class DiagnosticsCarryNeitherSecretsNorPayloads(unittest.TestCase):
    """Whatever an operator sends when they ask for help is the support bundle here."""

    def test_the_capability_report_never_echoes_a_secret(self):
        from coreyard import capabilities

        env = {"SHOPIFY_ADMIN_TOKEN": "shpat_notreal_secret",
               "SHOPIFY_WEBHOOK_SECRET": "shpss_notreal_secret",
               "SMB_PASSWORD": "hunter2"}
        with mock.patch.dict(os.environ, env, clear=False):
            report = "\n".join(f"{name} {state} {detail}"
                               for name, state, detail in capabilities.detect().summary())
        for secret in env.values():
            with self.subTest(secret=secret[:6]):
                self.assertNotIn(secret, report)

    def test_the_status_report_has_nowhere_to_put_an_order_payload(self):
        """`status` reads the sync state and the run history. Neither holds an order body,
        and the queue — which does — is not one of the things it opens."""
        import inspect

        from coreyard import status

        self.assertNotIn("EventQueue", inspect.getsource(status))
        self.assertNotIn("queue", inspect.getsource(status.gather).lower())


if __name__ == "__main__":
    unittest.main()
