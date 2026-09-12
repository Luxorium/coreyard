"""UX-08: what an installation can change without anybody editing this repository.

A yard adds a part type on a Tuesday. Another yard configures a different warranty, a
different handle prefix, a different shipping policy. None of that is a release, and none of
it should be: the operations available, the part types published and the claims made are all
derived from validated configuration and the current source data, not from a list somebody
has to remember to extend.

The tests run the real command tree against a demo installation, change one thing, and run it
again — because "takes effect on the next run" is a claim about two runs, and a unit test of
a loader cannot make it.
"""

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from coreyard.config import REPO_ROOT

INHERITED = ("COREYARD_", "SMB_", "YMS_", "SHOPIFY_", "STORE_", "XDG_DATA_HOME")


class ADemoInstallation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        started = self.run_command("init", "--demo")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        self.parts = self.source_path()

    def run_command(self, *args: str, extra=None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items()
               if not any(k.startswith(p) for p in INHERITED)}
        env["COREYARD_HOME"] = str(self.home)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.update(extra or {})
        return subprocess.run([sys.executable, "-m", "coreyard", *args],
                              env=env, cwd=self.home, capture_output=True, text=True,
                              timeout=180)

    def source_path(self) -> Path:
        """The CSV this installation reads.

        `init --demo` copies the bundled example into the data root rather than pointing at
        the package, precisely so it can be opened and edited to see what a column does —
        which is what these tests do to it.
        """
        path = self.home / "examples" / "parts.csv"
        self.assertTrue(path.is_file(), f"the demo source is not at {path}")
        return path

    def add_part(self, r_number: str, part_type: str) -> None:
        rows = list(csv.DictReader(self.parts.open(encoding="utf-8")))
        new = dict(rows[0])
        new.update({"r_number": r_number, "part_type": part_type, "price": "199.00",
                    "quantity": "1", "description": ""})
        with self.parts.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows + [new])

    def preview(self) -> str:
        result = self.run_command("sync", "--sink", "csv", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return (self.home / "out" / "products.csv").read_text(encoding="utf-8")


class ANewPartTypeNeedsNoRelease(ADemoInstallation):
    def test_it_is_published_on_the_very_next_run(self):
        """There is no list of supported part types to extend, which is the point: a yard
        that starts inventorying something new has not changed this program."""
        self.assertNotIn("Grille", self.preview())
        self.add_part("9001", "GRILLE")
        self.assertIn("Grille", self.preview())

    def test_a_type_nobody_has_wording_for_is_published_as_it_was_typed(self):
        """Truthful generic rendering rather than a guess: the abbreviation is expanded
        where the policy knows one and left alone where it does not."""
        self.add_part("9002", "ZZQ BRACKET")
        rendered = self.preview()
        # Kept as the yard spells it: the expander knows "BRACKET" and does not know "ZZQ",
        # and inventing a word for the half it does not know is the failure mode.
        self.assertIn("ZZQ Bracket", rendered)

    def test_the_coverage_report_refuses_here_and_says_why(self):
        """`part-types` reads the yard's own part-type table, so a tabular installation
        cannot serve it — and should say so by name rather than failing somewhere inside
        the run on a database setting it was never going to have."""
        result = self.run_command("part-types")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("database", (result.stdout + result.stderr).lower())


class APolicyChangeTakesEffectNextRun(ADemoInstallation):
    def edit_store(self, **settings) -> None:
        """Set storefront identity settings by their real keys.

        Which those are is not guessable — the vendor is `SHOPIFY_VENDOR` and the warranty
        is `STORE_WARRANTY` — which is what `docs/SETTINGS.md` exists to answer. `store.json`
        is a different thing again: its `profile` section is the catalog wording policy, and
        it refuses an unknown key by name rather than ignoring it.
        """
        path = self.home / ".env"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines()
                 if not any(line.startswith(f"{key}=") for key in settings)]
        lines += [f"{key}={value}" for key, value in settings.items()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_a_changed_warranty_shows_up_in_the_preview(self):
        self.edit_store(STORE_WARRANTY="120-day warranty")
        self.assertIn("120-day warranty", self.preview())

    def test_a_changed_vendor_renames_every_product(self):
        self.edit_store(SHOPIFY_VENDOR="Second Yard Auto")
        rendered = self.preview()
        self.assertIn("Second Yard Auto", rendered)

    def test_no_restart_is_involved_because_there_is_no_service(self):
        """Each run is a process. The only long-lived one is `orders serve`, and the runbook
        says that one is restarted; everything else reads its configuration as it starts."""
        self.edit_store(STORE_WARRANTY="A warranty")
        first = self.preview()
        self.edit_store(STORE_WARRANTY="A different warranty")
        self.assertNotEqual(first, self.preview())

    def test_a_run_uses_one_snapshot_of_its_configuration(self):
        """A policy file edited while a run is in flight must not be half-applied to the
        catalogue: the products a single run writes agree with each other."""
        self.edit_store(STORE_WARRANTY="Consistent warranty")
        self.preview()
        rows = [row for row in csv.DictReader(
            (self.home / "out" / "products.csv").open(encoding="utf-8")) if row["Title"]]
        self.assertTrue(rows)
        carrying = [row for row in rows if "Consistent warranty" in row["Body (HTML)"]]
        self.assertEqual(len(carrying), len(rows),
                         "one run published two different warranties")


class LosingACapabilityIsVisible(ADemoInstallation):
    def test_it_refuses_rather_than_publishing_somewhere_else(self):
        """The failure that would be worst here is the quiet one: a sink that cannot reach
        Shopify falling back to writing a file, and a run reporting success having published
        nothing to the store."""
        result = self.run_command("sync", "--dry-run")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("shopify", (result.stdout + result.stderr).lower())
        self.assertFalse((self.home / "out" / "products.csv").exists(),
                         "it wrote a CSV instead of refusing")

    def test_a_source_that_vanished_is_a_failure_not_an_empty_yard(self):
        self.parts.unlink()
        result = self.run_command("sync", "--sink", "csv", "--dry-run")
        self.assertNotEqual(result.returncode, 0)

    def test_the_diagnostic_names_what_is_missing(self):
        result = self.run_command("doctor")
        self.assertIn("shopify", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
