"""The quickstart, on an installation that has nothing else.

DIST-03 asks for an install into a clean environment "without a sibling repository or
preexisting private files". That phrase is doing a lot of work: every failure this file
pins down was invisible in a developer's checkout, because a checkout that runs the
documented quickstart already has a real `schema.json`, a populated `.env` and a photo
share beside the code, and each of those satisfied a check that had no business running.

Run against a data root that has none of them, `coreyard sync --sink csv --dry-run` — the
third line of the README — failed four times in a row, each time on a database requirement
the documented CSV path does not have: a schema mapping, `YMS_DB_HOST`, `SMB_HOST`, and
finally an actual `smbclient` subprocess looking for a share that does not exist.

The test is a subprocess because the data root is resolved once, when `coreyard.config` is
imported. That is the right moment for a running sync and the wrong one for a test that
wants to ask a second time.
"""

import csv
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coreyard.config import REPO_ROOT

# Anything a real installation would have. Stripped so the test cannot pass by borrowing
# this machine's configuration — which is exactly how the failures below stayed hidden.
INHERITED = ("COREYARD_", "SMB_", "YMS_", "SHOPIFY_", "STORE_", "XDG_DATA_HOME")


def run(home: Path, *args: str, extra: "dict[str, str] | None" = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if not any(k.startswith(p) for p in INHERITED)}
    env["COREYARD_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(extra or {})
    # Deliberately not the checkout: a relative path that only resolves from the source tree
    # is the bug, not the setup.
    return subprocess.run([sys.executable, "-m", "coreyard", *args],
                          env=env, cwd=home, capture_output=True, text=True, timeout=180)


class TheDocumentedQuickstart(unittest.TestCase):
    """`init --demo` then `sync --sink csv --dry-run`, and nothing else."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        init = run(self.home, "init", "--demo")
        self.assertEqual(init.returncode, 0, init.stderr)

    def test_init_writes_a_working_installation(self):
        for name in (".env", "store.json"):
            self.assertTrue((self.home / name).is_file(), f"{name} was not written")

    def test_the_demo_source_exists_where_the_configuration_names_it(self):
        """`.env` says `tabular:examples/parts.csv`; something has to put a file there."""
        env = (self.home / ".env").read_text(encoding="utf-8")
        line = next(l for l in env.splitlines() if l.startswith("COREYARD_SOURCE="))
        relative = line.split("=", 1)[1].split(":", 1)[1]
        self.assertTrue((self.home / relative).is_file(), f"{relative} is not there")

    def test_the_sync_renders_the_example_yard(self):
        result = run(self.home, "sync", "--sink", "csv", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        products = self.home / "out" / "products.csv"
        self.assertTrue(products.is_file(), result.stdout + result.stderr)
        with products.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 7, "the bundled example yard has seven parts")

    def test_it_asks_for_nothing_a_csv_source_does_not_have(self):
        """The four regressions, named. Each one used to end the run."""
        result = run(self.home, "sync", "--sink", "csv", "--dry-run")
        output = result.stdout + result.stderr
        for demanded in ("schema.json", "YMS_DB_HOST", "SMB_HOST", "smbclient"):
            self.assertNotIn(demanded, output,
                             f"a CSV source was asked for {demanded}:\n{output}")

    def test_nothing_was_written_into_the_installation(self):
        """DIST-04: the data root took the writes, and the code directory took none.

        Compared by modification time rather than existence, because the checkout running
        these tests is itself a live installation and has an `out/` full of real output.
        """
        watched = REPO_ROOT / "out" / "products.csv"
        before = watched.stat().st_mtime if watched.exists() else None
        run(self.home, "sync", "--sink", "csv", "--dry-run")
        after = watched.stat().st_mtime if watched.exists() else None
        self.assertEqual(before, after,
                         "the run wrote into the checkout rather than the data root")
        self.assertTrue((self.home / "out" / "products.csv").is_file())


class TheGuidanceItPrints(unittest.TestCase):
    """An instruction that names a file the reader does not have is not an instruction.

    Both messages below used to read `cp schema.example.json schema.json`, a bare filename
    that resolves only from the top of a checkout. Printed to someone who installed the
    package, it named a file in neither their working directory nor anywhere else they could
    reasonably look.
    """

    def _copy_target(self, text: str) -> Path:
        for line in text.splitlines():
            if "cp " in line and "schema.example.json" in line:
                return Path(line.split("cp ", 1)[1].strip().rstrip("\\").strip())
        self.fail(f"no copy instruction in:\n{text}")

    def test_the_schema_error_points_at_a_file_that_exists(self):
        from coreyard.yms import schema

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(schema.SchemaError) as caught:
                schema.load(Path(tmp) / "schema.json")
        self.assertTrue(self._copy_target(str(caught.exception)).is_file())

    def test_the_sync_refusal_points_at_a_file_that_exists(self):
        """The database path, refused for the right reason and pointing somewhere real.

        Credentials are supplied so the capability preflight passes and the run reaches the
        missing-mapping refusal. They are deliberately unreachable placeholders: this must
        stop at "no schema mapping" without opening a socket, which is also what keeps the
        test offline.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            result = run(home, "sync", "--dry-run", extra={
                "COREYARD_SOURCE": "database",
                "SMB_HOST": "192.0.2.1", "SMB_SERVER_NAME": "NOWHERE",
                "SMB_USER": "u", "SMB_PASSWORD": "p", "SMB_IMAGES_SHARE": "share",
                "YMS_DB_HOST": "192.0.2.1", "YMS_DB_NAME": "db",
                "SHOPIFY_VENDOR": "Test Yard",
            })
        output = result.stdout + result.stderr
        self.assertIn("No schema mapping yet", output, output)
        self.assertTrue(self._copy_target(output).is_file())
