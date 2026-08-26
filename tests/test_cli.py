"""Every command the launcher offers exists, imports, and can build its parser.

Cheap, and it catches a class of mistake unit tests otherwise miss entirely: a launcher line
pointing at a module that was renamed, an argument parser that raises at construction, or a
package whose `__init__` shadows one of its own submodules — which happened here, and made
`reconcile` fail on its first real invocation with an AttributeError rather than at import.
"""

import contextlib
import importlib
import io
import os
import sys
import unittest

from coreyard.config import REPO_ROOT

# Modules whose `main(argv)` takes an argument list, so their parser can be built here.
ENTRY_POINTS = [
    "coreyard.run_sync",
    "coreyard.webhook",
    "coreyard.reconcile.cli",
    "coreyard.repair.cli",
    "coreyard.audit.cli",
    "coreyard.sink.shopify_bulk",
    "coreyard.sink.backfill_alt",
    "coreyard.schedule",
]

# Entry points that read `sys.argv` themselves; only their importability is checked.
IMPORT_ONLY = ["coreyard.sink.shopify_oauth"]


@contextlib.contextmanager
def sealed_environment():
    """Run something that may call `load_env`, and leave the environment as it was.

    Several CLIs load `.env` before building their parser, which is correct — a
    a `YMS_WRITE_ORDERS` in there has to be seen as an argparse default. In a test process
    it is poison: the installation's real settings land in `os.environ` and stay there for
    every later test, which is how a `STORE_CHARM_PRICES=true` on this machine turned an
    unrelated price assertion red.
    """
    before = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


def _help(module, argv):
    """Run a CLI's --help, returning its output. Raises whatever the parser raises."""
    out = io.StringIO()
    with sealed_environment(), contextlib.redirect_stdout(out), \
            contextlib.suppress(SystemExit):
        module.main(argv)
    return out.getvalue()


class EntryPoints(unittest.TestCase):
    def test_every_entry_point_imports_and_parses(self):
        for name in ENTRY_POINTS:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                self.assertTrue(hasattr(module, "main"), f"{name} has no main()")
                self.assertIn("usage", _help(module, ["--help"]).lower())

    def test_the_remaining_entry_points_import(self):
        for name in IMPORT_ONLY:
            with self.subTest(module=name):
                self.assertTrue(hasattr(importlib.import_module(name), "main"))

    def test_repair_and_reconcile_subcommands_build(self):
        repair = importlib.import_module("coreyard.repair.cli")
        for what in ("titles", "tags", "seo", "weights", "descriptions", "all",
                     "fingerprints"):
            with self.subTest(subcommand=what):
                self.assertIn("usage", _help(repair, [what, "--help"]).lower())

    def test_order_transports_and_status_sync_build(self):
        webhook = importlib.import_module("coreyard.webhook")
        for action in ("serve", "poll", "sync-status", "status", "retry", "replay",
                       "register"):
            with self.subTest(action=action):
                self.assertIn("usage", _help(webhook, [action, "--help"]).lower())

    def test_audit_catalog_builds(self):
        audit = importlib.import_module("coreyard.audit.cli")
        self.assertIn("usage", _help(audit, ["catalog", "--help"]).lower())


class EnvironmentHygiene(unittest.TestCase):
    def test_building_a_parser_does_not_leak_the_installations_env(self):
        """The suite must not depend on whether this machine has a configured `.env`."""
        webhook = importlib.import_module("coreyard.webhook")
        before = dict(os.environ)
        _help(webhook, ["--help"])
        self.assertEqual(dict(os.environ), before)


class CommandTree(unittest.TestCase):
    """The root parser is the command list, so the tests walk it rather than the installer.

    The old test scraped a `case` statement out of install.sh with a regular expression,
    which is all it could do while routing lived in Bash. It could tell you a module named
    in the heredoc existed; it could not tell you the command was reachable, that its flags
    parsed, or that `--help` mentioned it.
    """

    def _root(self):
        from coreyard import cli

        with sealed_environment():
            return cli.build_parser()

    def _subparsers(self, parser):
        import argparse

        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action.choices
        return {}

    def test_every_declared_command_is_mounted(self):
        from coreyard import cli

        mounted = self._subparsers(self._root())
        for name, _module, _help in cli.COMMANDS:
            with self.subTest(command=name):
                self.assertIn(name, mounted)

    def test_every_command_module_supplies_its_own_flags(self):
        """A command is mounted by calling the module's own add_arguments, which is the
        same function its legacy `python -m coreyard.X` entry point calls. If a module
        stopped exporting it, the two surfaces could drift apart silently."""
        from coreyard import cli

        for name, module_path, _help in cli.COMMANDS:
            with self.subTest(command=name):
                module = importlib.import_module(module_path)
                self.assertTrue(callable(getattr(module, "add_arguments", None)),
                                f"{module_path} has no add_arguments()")

    def test_root_help_lists_every_command(self):
        from coreyard import cli

        text = _help(cli, ["--help"])
        for name, _module, _help_text in cli.COMMANDS:
            with self.subTest(command=name):
                self.assertIn(name, text)

    def test_every_command_and_subcommand_builds_its_help(self):
        """Walk the whole tree. This is what catches a parser that raises at construction
        or a subcommand nobody can reach."""
        from coreyard import cli

        root = self._root()
        for name, parser in self._subparsers(root).items():
            with self.subTest(command=name):
                self.assertIn("usage", _help(cli, [name, "--help"]).lower())
            for sub_name in self._subparsers(parser):
                with self.subTest(command=f"{name} {sub_name}"):
                    self.assertIn("usage",
                                  _help(cli, [name, sub_name, "--help"]).lower())

    def test_python_dash_m_coreyard_runs_the_same_cli(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "coreyard", "--help"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("coreyard", result.stdout)

    def test_bare_invocation_prints_help_rather_than_syncing(self):
        """`coreyard` with no command used to fall through to the sync parser and start
        a run against the live store."""
        from coreyard import cli

        out = io.StringIO()
        with sealed_environment(), contextlib.redirect_stdout(out):
            code = cli.main([])
        self.assertEqual(code, 0)
        self.assertIn("usage", out.getvalue().lower())


class Launcher(unittest.TestCase):
    """`bin/coreyard` is generated by install.sh, so the installer is the source of truth."""

    def _launcher_body(self, text: str) -> str:
        marker = "<<'LAUNCHER'"
        if marker in text:                      # the installer, with the heredoc around it
            text = text.split(marker, 1)[1].split("\nLAUNCHER", 1)[0]
        return text

    def test_the_installer_launcher_holds_no_command_list(self):
        """Routing belongs in cli.py. A `case` statement here is a second command list that
        `--help` cannot show and no test can walk."""
        body = self._launcher_body(
            (REPO_ROOT / "install.sh").read_text(encoding="utf-8"))
        self.assertIn("-m coreyard", body)
        self.assertNotIn("case ", body)

    def test_an_installed_launcher_matches_the_installer(self):
        launcher = REPO_ROOT / "bin" / "coreyard"
        if not launcher.exists():           # a fresh checkout has not run install.sh
            self.skipTest("bin/coreyard is generated by install.sh")
        installed = launcher.read_text(encoding="utf-8").strip()
        from_installer = self._launcher_body(
            (REPO_ROOT / "install.sh").read_text(encoding="utf-8")).strip()
        self.assertEqual(installed, from_installer)


class PackageShape(unittest.TestCase):
    def test_a_package_does_not_shadow_its_own_submodule(self):
        """`from pkg import name` must not hand back a function where a module is meant."""
        from coreyard.reconcile import planner, store

        for module in (planner, store):
            self.assertTrue(hasattr(module, "__file__"), f"{module!r} is not a module")
        self.assertTrue(callable(planner.plan))
        self.assertEqual(planner.ACTIVE, "ACTIVE")

    def test_the_documented_config_files_are_optional(self):
        """A bare installation must run with no profile, weight table, or order policy."""
        from coreyard.orders.policy import load as load_policy
        from coreyard.profile import load as load_profile
        from coreyard.transform.weights import load as load_weights

        self.assertEqual(load_profile(None).condition, "Used")
        self.assertEqual(load_weights(None).rules, ())
        self.assertFalse(load_policy(None).fulfillment.enabled)


if __name__ == "__main__":
    unittest.main()
