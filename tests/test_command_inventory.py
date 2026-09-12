"""Every public command has a declared support status, and the inventory says the truth.

(Named for the *command* inventory. ``tests/test_inventory.py`` is the yard extract's
mapping suite — "inventory" means the parts in this codebase far more often than it means
the list of commands.)

REL-01 forbids a public command with an undocumented support status, and REL-05 asks for a
checked-in inventory built from the *actual* CLI tree. Both fail the same way if left to
discipline: someone adds a subcommand, nobody adds a row, and the document quietly
describes a tool that no longer exists — which is worse than no document, because people
trust it.

These tests are the thing that makes it not a discipline problem. `cli.SUPPORT` must name
every node of the tree and no node that is gone, and `docs/INVENTORY.md` must be what the
generator produces right now.
"""

import argparse
import os
import unittest

from coreyard import cli

import scripts.inventory as inventory

# Building the command tree loads `.env`, deliberately: argparse evaluates its defaults at
# construction time, so a setting that lives in `.env` would otherwise never be seen as a
# flag default. That makes it a side effect this module has to contain rather than spread —
# a later test that asserts on the environment would otherwise fail because of this one.
_ENVIRONMENT: dict[str, str] = {}


def setUpModule():
    _ENVIRONMENT.update(os.environ)


def tearDownModule():
    os.environ.clear()
    os.environ.update(_ENVIRONMENT)


def paths() -> set[str]:
    return {path for path, _ in cli.tree(cli.build_parser())}


class SupportStatus(unittest.TestCase):
    def test_every_command_in_the_tree_has_a_declared_support_status(self):
        undeclared = sorted(paths() - set(cli.SUPPORT))
        self.assertEqual(undeclared, [],
                         "add these to cli.SUPPORT with what they write and what they need")

    def test_no_support_entry_describes_a_command_that_is_gone(self):
        stale = sorted(set(cli.SUPPORT) - paths())
        self.assertEqual(stale, [], "these were removed from the CLI; drop their rows")

    def test_every_effect_is_one_of_the_declared_kinds(self):
        known = {cli.READS, cli.LOCAL, cli.STORE, cli.SOURCE}
        for path, (effect, _, _) in cli.SUPPORT.items():
            self.assertIn(effect, known, path)

    def test_every_named_capability_is_one_capabilities_actually_reports(self):
        """A requirement nobody can check is a requirement nobody meets."""
        import os
        from unittest import mock

        from coreyard import capabilities

        with mock.patch.dict(os.environ, {}, clear=True):
            known = {name for name, _, _ in capabilities.detect(load=False).summary()}
        for path, (_, needs, _) in cli.SUPPORT.items():
            for capability in needs:
                self.assertIn(capability, known, f"{path} needs unknown '{capability}'")


    def test_every_write_outside_out_names_the_flag_that_unlocks_it(self):
        """UX-03: help and output must identify external writes. A write with no named
        gate is one a customer can trigger without meaning to."""
        # Deliberately not cli.STORE: a Shopify write is gated the other way round, by
        # --dry-run rather than by --apply, so the sync commands correctly name no flag.
        ungated = [path for path, (effect, _, gate) in cli.SUPPORT.items()
                   if effect == cli.SOURCE and not gate]
        self.assertEqual(ungated, [])


class CheckedInInventory(unittest.TestCase):
    def test_the_checked_in_inventory_matches_the_live_command_tree(self):
        path = inventory.OUTPUT
        self.assertTrue(path.exists(), f"run `python scripts/inventory.py` to create {path}")
        self.assertEqual(path.read_text(encoding="utf-8"), inventory.render(),
                         "docs/INVENTORY.md is stale — run `python scripts/inventory.py`")

    def test_no_command_renders_as_undocumented(self):
        self.assertNotIn("UNDOCUMENTED", inventory.render())


class HelpWorksWithoutCredentials(unittest.TestCase):
    """UX-04: help must work without credentials, and must never hang on a prompt.

    Every one of these screens is what a customer reads before they have configured
    anything. A command whose `--help` raises is a command they cannot find out about.
    """

    def test_every_command_prints_help_and_exits_zero(self):
        import contextlib
        import io

        failures = []
        for path in sorted(paths()):
            argv = path.split() + ["--help"]
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    cli.main(argv)
            except SystemExit as exc:
                if exc.code not in (0, None):
                    failures.append((path, f"exit {exc.code}"))
            except Exception as exc:                          # pragma: no cover - a bug
                failures.append((path, f"{type(exc).__name__}: {exc}"))
            else:
                continue
            if not out.getvalue().strip():
                failures.append((path, "printed nothing"))
        self.assertEqual(failures, [])

    def test_the_root_help_lists_every_top_level_command(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main([])
        printed = out.getvalue()
        for name, _, _ in cli.COMMANDS:
            self.assertIn(name, printed)


class ExampleConfigurationIsGeneric(unittest.TestCase):
    """QA-01: CI validates the generic example configs.

    They are the files a new customer copies. `store.example.json` was already checked;
    the schema example was not, and it is the one that carries the shape of somebody else's
    installation if anyone is careless.
    """

    def test_the_example_schema_loads(self):
        from coreyard.config import bundled
        from coreyard.yms import schema

        mapping = schema.load(bundled("schema.example.json"))
        self.assertTrue(mapping.build_query(limit=1))


class VersionIsDeclaredOnce(unittest.TestCase):
    """REL-03: metadata, CLI and tag versions must agree from one authoritative source."""

    def test_pyproject_takes_its_version_from_the_package(self):
        from coreyard.config import REPO_ROOT

        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn('version = { attr = "coreyard.__version__" }', text)
        self.assertNotIn('\nversion = "', text,
                         "a literal version here can disagree with coreyard.__version__")

    def test_the_cli_reports_the_package_version(self):
        import contextlib
        import io

        from coreyard import __version__

        out = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(out):
            cli.main(["--version"])
        self.assertIn(__version__, out.getvalue())


class UnsupportedCombinationsRefuseFirst(unittest.TestCase):
    """REL-01: an unsupported combination fails before side effects, with an explanation.

    A tabular yard genuinely cannot serve `coreyard schema` or a database delta. The useful
    moment to say so is before the run starts — not as a traceback from inside the extract,
    after a lock has been taken and a log line written that reads like a job that ran.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmp.name) / "parts.csv"
        self.csv.write_text("r_number,part_type,price\n51,TAIL LAMP,89.00\n",
                            encoding="utf-8")
        self.environ = {"COREYARD_SOURCE": f"tabular:{self.csv}"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv):
        import contextlib
        import io
        from unittest import mock

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.environ, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue() + err.getvalue()

    def test_a_database_delta_is_refused_on_a_tabular_source(self):
        code, text = self.run_cli(["sync", "delta", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("cannot run on this installation", text)
        self.assertIn("delta", text)
        self.assertIn("Nothing was changed", text)

    def test_schema_introspection_is_refused_on_a_tabular_source(self):
        code, text = self.run_cli(["schema"])
        self.assertEqual(code, 2)
        self.assertIn("database", text)

    def test_the_refusal_names_the_source_the_operator_configured(self):
        _, text = self.run_cli(["schema"])
        self.assertIn(f"tabular:{self.csv}", text)

    def test_a_supported_command_is_not_refused(self):
        code, text = self.run_cli(["status"])
        self.assertEqual(code, 0)
        self.assertNotIn("cannot run on this installation", text)

    def test_nothing_is_refused_on_a_fully_configured_installation(self):
        """The guard must be a floor, not a gate: with every capability on, no command may
        be blocked. A preflight that refuses working commands is worse than none."""
        from unittest import mock

        from coreyard import capabilities

        class AllOn:
            source_kind, source_argument, source_spec = "database", "", "database"

            def enabled(self, name):
                return True

            def get(self, name):
                return capabilities.Capability(name, capabilities.ON, "on")

        caps = AllOn()
        blocked = [path for path, _ in cli.tree(cli.build_parser())
                   if cli.unmet(path, caps)]
        self.assertEqual(blocked, [])

    def test_every_command_is_reachable_on_the_installation_it_is_documented_for(self):
        """Each declared requirement must be a capability that can actually be turned on,
        or the command is unreachable everywhere — a support status nobody can satisfy."""
        import os as _os
        from unittest import mock

        from coreyard import capabilities

        with mock.patch.dict(_os.environ, {}, clear=True):
            names = {name for name, _, _ in capabilities.detect(load=False).summary()}
        for path, (_, needs, _) in cli.SUPPORT.items():
            self.assertTrue(set(needs) <= names, f"{path} needs {set(needs) - names}")


class CapabilityMatrix(unittest.TestCase):
    """REL-01: the matrix must describe the product that exists.

    A matrix is the document a customer reads to decide whether CoreYard fits their yard.
    Its failure mode is not being wrong in an argument — it is naming a test that was
    deleted or omitting a capability that was added, so it slowly describes a neighbouring
    product nobody can buy.
    """

    @property
    def text(self) -> str:
        from coreyard.config import REPO_ROOT

        return (REPO_ROOT / "docs" / "CAPABILITY_MATRIX.md").read_text(encoding="utf-8")

    def test_every_capability_is_documented(self):
        import re
        from unittest import mock

        from coreyard import capabilities

        with mock.patch.dict(os.environ, {}, clear=True):
            names = [name for name, _, _ in capabilities.detect(load=False).summary()]
        body = self.text
        undocumented = [name for name in names
                        if not re.search(rf"`{re.escape(name)}`", body)]
        self.assertEqual(undocumented, [],
                         "add these to docs/CAPABILITY_MATRIX.md")

    def test_every_test_file_it_cites_exists(self):
        import re

        from coreyard.config import REPO_ROOT

        cited = sorted(set(re.findall(r"tests/test_[a-z_]+\.py", self.text)))
        self.assertTrue(cited, "the matrix cites no evidence at all")
        missing = [name for name in cited if not (REPO_ROOT / name).is_file()]
        self.assertEqual(missing, [])

    def test_it_does_not_claim_a_source_kind_the_loader_rejects(self):
        import re

        from coreyard import source

        for spec in sorted(set(re.findall(r"`(database|tabular:[^`]+)`", self.text))):
            kind = spec.split(":")[0]
            self.assertIn(kind, ("database", "tabular"), spec)
        with self.assertRaises(ValueError):
            source.load("postgres")


class EvidenceLedger(unittest.TestCase):
    """QA-02: one row per criterion, and no criterion quietly without one.

    The failure mode of a hand-maintained ledger is not a wrong row — it is a criterion
    with no row at all, which is then never looked at again.
    """

    def test_every_acceptance_criterion_has_a_ledger_row(self):
        import scripts.ledger as ledger

        missing = sorted(set(ledger.criteria()) - set(ledger.rows()))
        self.assertEqual(missing, [], "add rows to docs/EVIDENCE_LEDGER.md")

    def test_no_ledger_row_names_a_criterion_that_is_gone(self):
        import scripts.ledger as ledger

        stale = sorted(set(ledger.rows()) - set(ledger.criteria()))
        self.assertEqual(stale, [])

    def test_every_row_carries_one_of_the_declared_statuses(self):
        import scripts.ledger as ledger

        for code, line in ledger.rows().items():
            self.assertTrue(any(status in line for status in ledger.STATUSES), code)


class ReadOnlyIsPerInvocation(unittest.TestCase):
    """`images` lists and downloads photographs — and deletes them from the share.

    Being on the read-only list meant the single most destructive image operation was the
    one nothing recorded in the run history. A history that omits exactly the deletions is
    worse than one that omits nothing.
    """

    @staticmethod
    def args(**values):
        return argparse.Namespace(**values)

    def test_listing_photos_is_still_read_only(self):
        self.assertTrue(cli._reports_only(self.args(command="images", delete=None)))

    def test_fetching_photos_is_still_read_only(self):
        self.assertTrue(cli._reports_only(
            self.args(command="images", delete=None, fetch=True)))

    def test_deleting_photos_from_the_share_is_recorded(self):
        self.assertFalse(cli._reports_only(
            self.args(command="images", delete=["04"], apply=True)))

    def test_a_planned_deletion_is_recorded_too(self):
        """The plan is the decision; recording only the applied half loses why."""
        self.assertFalse(cli._reports_only(
            self.args(command="images", delete=["04"], apply=False)))

    def test_the_other_read_only_commands_are_unaffected(self):
        for command in sorted(cli.READ_ONLY - {"images"}):
            self.assertTrue(cli._reports_only(self.args(command=command)), command)

    def test_a_writing_command_is_never_treated_as_a_report(self):
        self.assertFalse(cli._reports_only(self.args(command="sync")))


if __name__ == "__main__":
    unittest.main()


class SharedControlsMeanOneThing(unittest.TestCase):
    """UX-06: an option that appears on several commands has to behave the same on each.

    Seventeen commands take `--limit`, sixteen take `--dry-run`, fifteen take `--apply`. What
    makes that a feature rather than a coincidence is that knowing one teaches you the rest —
    so a flag that is a switch on four commands and takes a value on the fifth is worse than
    a differently-named flag would have been, because nothing warns you.
    """

    def flags(self):
        from collections import defaultdict

        from coreyard import cli

        found = defaultdict(list)
        for path, parser in cli.tree(cli.build_parser()):
            for action in parser._actions:
                for option in action.option_strings:
                    if option not in ("-h", "--help"):
                        found[option].append((path, action))
        return found

    @staticmethod
    def _accepts_bare(action) -> bool:
        """Can it be written on its own, with nothing after it?"""
        return action.nargs == 0 or action.nargs in ("?", "*")

    def test_an_option_written_bare_on_one_command_works_bare_on_all_of_them(self):
        """`--json` was a switch on `status`, `doctor` and `alert` and a *path* on `audit`,
        so `audit catalog --json | jq` failed with an argparse error rather than printing
        anything. It now accepts both, which is what the older spelling deserves."""
        for option, uses in sorted(self.flags().items()):
            with self.subTest(option=option):
                bare = {self._accepts_bare(action) for _, action in uses}
                self.assertEqual(len(bare), 1,
                                 f"{option} can be written bare on some of "
                                 f"{[path for path, _ in uses]} and not others")

    def test_every_option_explains_itself(self):
        """`--help` is the documentation an operator has in front of them at the moment they
        need it, and an option with nothing beside it is a question they have to take
        somewhere else."""
        for option, uses in sorted(self.flags().items()):
            for path, action in uses:
                with self.subTest(command=path, option=option):
                    self.assertTrue((action.help or "").strip(),
                                    f"`coreyard {path} {option}` has no help text")

    def test_one_word_means_stop_planning_and_write_it(self):
        """A second spelling is a second thing to remember at exactly the moment being wrong
        is expensive. `--apply` is that word everywhere it applies."""
        for rejected in ("--commit", "--no-dry-run", "--for-real", "--execute"):
            with self.subTest(option=rejected):
                self.assertNotIn(rejected, self.flags())

    def test_the_source_write_gate_is_spelled_the_same_everywhere(self):
        """It shipped as `--write-order` on `replay` and `--write-orders` on its three
        siblings. One letter, and the command it differs on is the one somebody reaches for
        while an order is already stuck."""
        commands = {path for path, _ in self.flags()["--write-orders"]}
        self.assertEqual(commands,
                         {"orders serve", "orders retry", "orders poll", "orders replay"})
