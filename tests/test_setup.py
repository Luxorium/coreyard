"""`coreyard init` — the first command a new installation runs.

The tests that matter here are not that it writes a file. They are that what it writes is
*valid*: a .env this project's own parser reads back, and a store.json this project's own
loader accepts. A setup command that emits something the tool then rejects is worse than no
setup command, because it fails at the point where a newcomer has least context.
"""

import contextlib
import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from coreyard import setup_wizard, store


def parse(args):
    import argparse
    return setup_wizard.add_arguments(argparse.ArgumentParser()).parse_args(args)


class Rendering(unittest.TestCase):
    def test_a_tabular_source_asks_for_no_credentials(self):
        body = setup_wizard.render_env({"vendor": "Y", "source": "tabular:parts.csv"})
        self.assertIn("COREYARD_SOURCE=tabular:parts.csv", body)
        for absent in ("YMS_DB_PASSWORD", "SMB_PASSWORD", "YMS_DB_HOST"):
            self.assertNotIn(absent, body)

    def test_a_database_source_writes_the_connection_block(self):
        body = setup_wizard.render_env({"vendor": "Y", "source": "database",
                                        "db_host": "sql.example", "smb_host": "files.example"})
        self.assertIn("YMS_DB_HOST=sql.example", body)
        self.assertIn("SMB_HOST=files.example", body)
        self.assertNotIn("COREYARD_SOURCE=", body)

    def test_identity_reaches_the_file(self):
        body = setup_wizard.render_env(
            {"vendor": "A Yard", "city": "Springfield, IL", "handle_prefix": "yard"})
        self.assertIn("SHOPIFY_VENDOR=A Yard", body)
        self.assertIn("STORE_CITY=Springfield, IL", body)
        self.assertIn("SHOPIFY_HANDLE_PREFIX=yard", body)

    def test_the_generated_store_file_claims_nothing_on_the_sellers_behalf(self):
        """A generated file full of guessed policy is worse than an absent one."""
        rendered = setup_wizard.render_store({"vendor": "Y"})
        profile = rendered["profile"]
        self.assertEqual([k for k in profile if not k.startswith("_")], [])


class Output(unittest.TestCase):
    def setUp(self):
        store.forget()
        self.addCleanup(store.forget)

    def demo(self, tmp, extra=()):
        args = parse(["--demo", "--env", str(Path(tmp) / ".env"),
                      "--store", str(Path(tmp) / "store.json"), *extra])
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return args, setup_wizard.run(args)

    def test_the_demo_writes_both_files(self):
        with TemporaryDirectory() as tmp:
            args, code = self.demo(tmp)
            self.assertEqual(code, 0)
            self.assertTrue(Path(args.env).is_file())
            self.assertTrue(Path(args.store).is_file())

    def test_the_env_is_owner_only_because_it_can_hold_credentials(self):
        with TemporaryDirectory() as tmp:
            args, _ = self.demo(tmp)
            self.assertEqual(os.stat(args.env).st_mode & 0o777, 0o600)

    def test_it_refuses_to_clobber(self):
        with TemporaryDirectory() as tmp:
            self.demo(tmp)
            args, code = self.demo(tmp)
            self.assertEqual(code, 1)

    def test_force_overwrites(self):
        with TemporaryDirectory() as tmp:
            self.demo(tmp)
            _, code = self.demo(tmp, extra=["--force"])
            self.assertEqual(code, 0)

    def test_it_refuses_to_guess_when_nothing_was_asked_and_nobody_is_there(self):
        """Non-interactive with no --demo and no --vendor writes nothing."""
        with TemporaryDirectory() as tmp:
            args = parse(["--env", str(Path(tmp) / ".env"),
                          "--store", str(Path(tmp) / "store.json")])
            with mock.patch("sys.stdin.isatty", return_value=False), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(setup_wizard.run(args), 2)
            self.assertFalse(Path(args.env).exists())


class Validity(unittest.TestCase):
    """What it writes must be readable by the code that reads it."""

    def setUp(self):
        store.forget()
        self.addCleanup(store.forget)

    def test_the_generated_env_parses_with_this_projects_own_parser(self):
        from coreyard.config import load_env
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            setup_wizard.write({"vendor": "A Yard", "city": "Springfield, IL",
                                "source": "tabular:parts.csv", "handle_prefix": "yard"},
                               env, Path(tmp) / "store.json")
            with mock.patch.dict(os.environ, {}, clear=False):
                for key in ("SHOPIFY_VENDOR", "STORE_CITY", "COREYARD_SOURCE"):
                    os.environ.pop(key, None)
                load_env(env)
                self.assertEqual(os.environ["SHOPIFY_VENDOR"], "A Yard")
                self.assertEqual(os.environ["STORE_CITY"], "Springfield, IL")
                self.assertEqual(os.environ["COREYARD_SOURCE"], "tabular:parts.csv")

    def test_the_generated_store_file_loads(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "store.json"
            setup_wizard.write({"vendor": "Y", "source": "tabular:x.csv"},
                               Path(tmp) / ".env", target)
            with mock.patch.dict(os.environ, {"STORE_FILE": str(target)}, clear=False):
                self.assertIn("profile", store.load())

    def test_the_generated_profile_section_builds_a_real_profile(self):
        from coreyard.profile import from_dict
        rendered = setup_wizard.render_store({"vendor": "Y"})
        self.assertIsNotNone(from_dict(rendered["profile"]))


class DemoAnswers(unittest.TestCase):
    def test_the_demo_points_at_the_bundled_export(self):
        answers = setup_wizard.collect(parse(["--demo"]), interactive=False)
        self.assertEqual(answers["source"], setup_wizard.DEMO_SOURCE)

    def test_the_bundled_export_named_by_the_demo_exists(self):
        """The one path a newcomer takes first must not be a broken promise."""
        self.assertTrue(setup_wizard.bundled_demo_csv().is_file())

    def test_the_demo_source_names_where_the_demo_is_installed(self):
        """`.env` says `tabular:examples/parts.csv`; something has to put a file there.

        The two used to be joined only by the launcher's `cd` into the checkout. Installed
        as a package there is no checkout to cd into, so the demo has to place its own file
        under the data root the configuration is resolved against.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            written = setup_wizard.install_demo_source(root)
            relative = setup_wizard.DEMO_SOURCE.split(":", 1)[1]
            self.assertEqual(written, root / relative)
            self.assertTrue(written.is_file())
            self.assertEqual(written.read_bytes(),
                             setup_wizard.bundled_demo_csv().read_bytes())

    def test_installing_the_demo_twice_keeps_the_first_copy(self):
        """Someone who edited the example to see what a column does meant to."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            written = setup_wizard.install_demo_source(root)
            written.write_text("edited", encoding="utf-8")
            self.assertEqual(setup_wizard.install_demo_source(root).read_text(), "edited")

    def test_flags_win_over_demo_defaults(self):
        answers = setup_wizard.collect(
            parse(["--demo", "--vendor", "Real Yard"]), interactive=False)
        self.assertEqual(answers["vendor"], "Real Yard")
