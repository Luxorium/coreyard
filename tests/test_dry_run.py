"""UX-03: `--dry-run` reports, and that is the whole of what it does.

Sixteen commands take the flag, and it is the one an operator uses when they are unsure —
before a first publish, after changing a policy file, when a diff looks wrong. It is
therefore the flag with the least margin for being approximately true: a dry run that
advances the snapshot leaves the next real run believing work was done, and the change it
skipped is never published by anything.

What a dry run *may* write is narrow and documented: its preview (the CSV sink's file) and a
row in the run history, because a run that happened is a fact whether or not it changed
anything. What it may not touch is publication state — the fingerprints, the photo
manifests, the retirement memory and the delta cursor — and that is what these check, table
by table rather than by file, because the run history lives in the same database.

`schedule install --dry-run` is exercised in `tests/test_schedule.py` instead: it writes to
the host's crontab rather than to the data root, and a test that runs it for real would be
editing the machine's own scheduled jobs to prove it does not.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coreyard import cli
from coreyard.config import REPO_ROOT

INHERITED = ("COREYARD_", "SMB_", "YMS_", "SHOPIFY_", "STORE_", "XDG_DATA_HOME")

#: Writes to the host's crontab, not the data root. See the module docstring.
EXCLUDED = {"schedule install"}


def dry_run_commands() -> list[str]:
    return [path for path, parser in cli.tree(cli.build_parser())
            if any(a.option_strings == ["--dry-run"] for a in parser._actions)
            and path not in EXCLUDED]


class NoDryRunAdvancesPublicationState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls.tmp.name)
        started = cls.run_command("init", "--demo")
        assert started.returncode == 0, started.stdout + started.stderr
        # A real snapshot to be wrong about: an empty state cannot be advanced, so every
        # assertion below would hold for the wrong reason. Written directly because the CSV
        # sink deliberately commits nothing — it is a preview path — and the API sink needs
        # a store this test does not have.
        cls.seed_state()

    @classmethod
    def seed_state(cls) -> None:
        from decimal import Decimal

        from coreyard.config import StoreProfile
        from coreyard.models import Part
        from coreyard.state import SyncState, fingerprints_all

        parts = [Part(r_number=str(n), part_type="Door", price=Decimal("100.00"),
                      quantity=1, make="Honda", model="Civic", year=2015)
                 for n in range(1, 6)]
        with SyncState(cls.home / "coreyard_sync_state.sqlite3") as state:
            state.update(fingerprints_all(parts, lambda part: [], StoreProfile()))
            state.set_cursor("delta_modified_at", "2026-09-01T00:00:00")
            state.record_retired("99", "ACTIVE")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def run_command(cls, *args: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items()
               if not any(k.startswith(p) for p in INHERITED)}
        env["COREYARD_HOME"] = str(cls.home)
        env["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run([sys.executable, "-m", "coreyard", *args],
                              env=env, cwd=cls.home, capture_output=True, text=True,
                              timeout=180)

    def publication_state(self) -> dict:
        """Everything a later run reads to decide what is already published."""
        database = self.home / "coreyard_sync_state.sqlite3"
        if not database.exists():
            return {}
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            state = {}
            for table in ("parts", "retired", "channel_state", "cursors"):
                try:
                    state[table] = conn.execute(
                        f"SELECT * FROM {table} ORDER BY 1").fetchall()
                except sqlite3.Error:
                    state[table] = None      # a table this installation has not created
            return state
        finally:
            conn.close()

    def test_the_fixture_has_something_to_lose(self):
        """Otherwise every assertion below is about an empty database."""
        self.assertTrue(self.publication_state()["parts"])

    def test_every_dry_run_command_leaves_it_exactly_as_it_was(self):
        commands = dry_run_commands()
        self.assertGreaterEqual(len(commands), 14, "the sweep stopped finding commands")
        before = self.publication_state()
        for path in commands:
            with self.subTest(command=path):
                result = self.run_command(*path.split(), "--dry-run")
                # 0 ran and reported; 2 refused because this demo installation has no
                # Shopify credentials. Both are "nothing was changed", which is the claim.
                self.assertIn(result.returncode, (0, 2),
                              result.stdout + result.stderr)
                self.assertEqual(self.publication_state(), before,
                                 f"`{path} --dry-run` moved publication state")

    def test_a_dry_run_is_recorded_as_one(self):
        """The run history does gain a row — a run that happened is a fact — and it says
        dry so that "last full sync, ok" cannot be read off an hour that published nothing."""
        self.run_command("sync", "--sink", "csv", "--dry-run")
        database = self.home / "coreyard_sync_state.sqlite3"
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            counts = conn.execute(
                "SELECT counts FROM runs WHERE command = 'sync'"
                " ORDER BY id DESC LIMIT 1").fetchone()[0]
        finally:
            conn.close()
        self.assertIn('"dry_run": true', counts)

    def test_the_preview_it_writes_is_the_documented_one(self):
        """The CSV sink's dry run writes its file: that is the preview, and the runbook says
        so. Nothing else appears outside `out/`."""
        before = {p.relative_to(self.home) for p in self.home.rglob("*")
                  if p.is_file() and "out" not in p.parts}
        self.run_command("sync", "--sink", "csv", "--dry-run")
        after = {p.relative_to(self.home) for p in self.home.rglob("*")
                 if p.is_file() and "out" not in p.parts}
        self.assertEqual(after, before)
        self.assertTrue((self.home / "out" / "products.csv").exists())


class TheFlagIsOfferedWhereItMatters(unittest.TestCase):
    def test_every_command_that_writes_to_the_store_can_be_previewed(self):
        """A command that changes a catalogue and cannot be asked what it would change is a
        command people run at the wrong moment."""
        previewable = set(dry_run_commands()) | EXCLUDED
        for path, (effect, _, gate) in cli.SUPPORT.items():
            if effect != cli.STORE:
                continue
            with self.subTest(command=path):
                # Either it takes `--dry-run`, or it plans unless told to `--apply`, which is
                # the same promise spelled the other way round.
                self.assertTrue(path in previewable or gate == "--apply",
                                f"`coreyard {path}` writes and offers no preview")
