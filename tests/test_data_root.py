"""Where CoreYard writes, and why it is not where CoreYard is installed.

DIST-04: an installed package must work from an arbitrary working directory, must not
require writing into its own installation, and two installations on one host must keep
their store identity, credentials, locks and state separate.

Anchoring the state database, `.env` and the lock directory to the package's own parent
directory satisfies none of that once CoreYard is installed rather than checked out.
`REPO_ROOT` would be inside `site-packages`: writing there fails outright when the tree is
read-only, and is wrong even when it is not, because the next upgrade replaces the
directory holding the yard's sync state.

The resolution is deliberately ordered so an existing installation moves nothing — a
source checkout still resolves to itself, byte for byte.
"""

import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from coreyard.config import DATA_ROOT, REPO_ROOT, _data_root, out_dir

# `DATA_ROOT` is resolved when `coreyard.config` is imported, which is the right moment —
# one process, one answer, no path that changes under a running sync. It also means these
# tests cannot patch the environment in-process and re-ask, so they ask a subprocess.
PROBE = (
    "from coreyard.config import DATA_ROOT, ENV_PATH, out_dir;"
    "from coreyard.state import DEFAULT_STATE_DB;"
    "from coreyard.orders.pipeline import QUEUE_DB;"
    "print(DATA_ROOT);print(ENV_PATH);print(out_dir());"
    "print(DEFAULT_STATE_DB);print(QUEUE_DB)"
)


def probe(environ: dict, cwd=None) -> list[str]:
    """Where a fresh interpreter decides this installation's files live."""
    env = dict(os.environ)
    env.pop("COREYARD_HOME", None)
    env.pop("XDG_DATA_HOME", None)
    env.update(environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run([sys.executable, "-c", PROBE], env=env, cwd=cwd,
                            capture_output=True, text=True, timeout=60)
    if result.returncode:                                     # pragma: no cover - a bug
        raise AssertionError(result.stderr)
    return result.stdout.strip().splitlines()


class ASourceCheckoutIsUnchanged(unittest.TestCase):
    """The one property that matters most: an upgrade must not move a yard's state."""

    def test_a_checkout_resolves_to_itself(self):
        """Asked of the resolver, not of `DATA_ROOT`: the suite deliberately runs with
        `COREYARD_HOME` set elsewhere, which is rule 1 and would answer a different
        question."""
        environment = dict(os.environ)
        environment.pop("COREYARD_HOME", None)
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(_data_root(), REPO_ROOT)

    def test_the_state_database_is_where_it_has_always_been(self):
        root, env_path, workspace, state, queue = probe({})
        self.assertEqual(root, str(REPO_ROOT))
        self.assertEqual(env_path, str(REPO_ROOT / ".env"))
        self.assertEqual(workspace, str(REPO_ROOT / "out"))
        self.assertEqual(state, str(REPO_ROOT / "coreyard_sync_state.sqlite3"))
        self.assertEqual(queue, str(REPO_ROOT / "coreyard_webhook_queue.sqlite3"))

    def test_the_answer_does_not_depend_on_the_working_directory(self):
        """DIST-04: an installed method works from an arbitrary working directory."""
        with tempfile.TemporaryDirectory() as elsewhere:
            self.assertEqual(probe({}, cwd=elsewhere), probe({}, cwd=str(REPO_ROOT)))


class AnInstalledPackageWritesElsewhere(unittest.TestCase):
    def test_a_read_only_installation_falls_back_to_the_user_data_directory(self):
        """The failure this prevents: `pip install` into a read-only tree, then a sync
        that tries to create its state database inside site-packages."""
        from unittest import mock

        import coreyard.config as config

        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as installed:
            # An installed package: a directory holding the package, with no pyproject.toml
            # beside it — which is exactly how `site-packages` looks.
            with mock.patch.object(config, "REPO_ROOT", Path(installed)), \
                    mock.patch.dict(os.environ, {"XDG_DATA_HOME": home}, clear=False):
                os.environ.pop("COREYARD_HOME", None)
                self.assertEqual(config._data_root(), Path(home) / "coreyard")

    def test_an_unwritable_checkout_also_falls_back(self):
        """A checkout owned by another user is still not somewhere to write state."""
        from unittest import mock

        import coreyard.config as config

        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as installed:
            (Path(installed) / "pyproject.toml").write_text("", encoding="utf-8")
            os.chmod(installed, 0o555)
            try:
                with mock.patch.object(config, "REPO_ROOT", Path(installed)), \
                        mock.patch.dict(os.environ, {"XDG_DATA_HOME": home}, clear=False):
                    os.environ.pop("COREYARD_HOME", None)
                    self.assertEqual(config._data_root(), Path(home) / "coreyard")
            finally:
                os.chmod(installed, 0o755)

    def test_xdg_data_home_is_honoured(self):
        with tempfile.TemporaryDirectory() as home:
            root, env_path, _, state, _ = probe({"XDG_DATA_HOME": home})
            # A checkout wins over XDG, which is the point of rule 2 — so this asserts the
            # ordering rather than the fallback, which the unit above covers directly.
            self.assertEqual(root, str(REPO_ROOT))
            self.assertTrue(env_path.startswith(str(REPO_ROOT)))
            self.assertTrue(state.startswith(str(REPO_ROOT)))


class TwoInstallationsOnOneHost(unittest.TestCase):
    """DIST-04: separate store identities, locks, credentials and state."""

    def test_coreyard_home_moves_every_writable_path(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            a = probe({"COREYARD_HOME": first})
            b = probe({"COREYARD_HOME": second})
            self.assertEqual(a[0], first)
            self.assertEqual(b[0], second)
            for path_a, path_b in zip(a, b):
                self.assertTrue(path_a.startswith(first), path_a)
                self.assertTrue(path_b.startswith(second), path_b)
                self.assertNotEqual(path_a, path_b)

    def test_nothing_writable_is_left_under_the_installation(self):
        with tempfile.TemporaryDirectory() as home:
            for path in probe({"COREYARD_HOME": home}):
                self.assertFalse(path.startswith(str(REPO_ROOT / "coreyard")), path)

    def test_a_home_with_spaces_in_the_path_works(self):
        """DIST-03 asks for paths with spaces explicitly."""
        with tempfile.TemporaryDirectory() as base:
            home = Path(base) / "a yard with spaces"
            home.mkdir()
            self.assertEqual(probe({"COREYARD_HOME": str(home)})[0], str(home))


class CodeAndDataAreSeparate(unittest.TestCase):
    def test_the_launcher_and_bundled_examples_stay_with_the_code(self):
        """`REPO_ROOT` is not obsolete — it is now only ever the installation.

        The bundled files are anchored to the *package* rather than to the checkout, which
        is the whole reason an installed wheel can find its own example yard and config
        templates. Anchoring them to the checkout again would pass every test in a clone and
        fail for every customer who installed the package.
        """
        import coreyard
        from coreyard.config import bundled

        package = Path(coreyard.__file__).resolve().parent
        for name in ("parts.csv", "schema.example.json", "store.example.json"):
            path = bundled(name)
            self.assertTrue(path.is_file(), f"{name} is not shipped with the package")
            self.assertTrue(str(path).startswith(str(package)),
                            f"{name} must travel with the code, not with the checkout")

    def test_no_module_anchors_a_writable_path_to_the_installation(self):
        """A regression guard with teeth: the sweep that moved these is easy to undo one
        line at a time, and each undone line is a file written into site-packages."""
        import re

        offenders = []
        for source in sorted((REPO_ROOT / "coreyard").rglob("*.py")):
            text = source.read_text(encoding="utf-8")
            for match in re.finditer(r'REPO_ROOT / "([^"]+)"', text):
                target = match.group(1)
                if target in ("out", ".env", "store.json", "portal.json", "schema.json"):
                    offenders.append(f"{source.name}: {match.group(0)}")
        self.assertEqual(offenders, [], "these belong under DATA_ROOT")

    def test_the_output_directory_lives_under_the_data_root(self):
        self.assertEqual(out_dir(), DATA_ROOT / "out")


if __name__ == "__main__":
    unittest.main()


class AConfiguredPathIsNotRelativeToWhereverYouStarted(unittest.TestCase):
    """A relative path in `.env` names one of this installation's files.

    Resolved against the process directory instead, the same configuration works from a
    shell in the checkout and fails from anywhere else. That is not hypothetical: the health
    check is the one scheduled job whose crontab line has no `cd` in front of it, so cron ran
    it from the home directory, `STORE_CATALOG_OVERRIDES_FILE=out/engine-catalog-overrides.json`
    resolved to a file that was never there, and `doctor` reported a perfectly good override
    file as missing every fifteen minutes for days. The error named the relative path it was
    handed, so it read as "the file is gone" rather than "I looked in the wrong place".
    """

    def test_a_relative_path_resolves_against_the_data_root(self):
        from coreyard.config import data_path

        self.assertEqual(data_path("out/overrides.json"), DATA_ROOT / "out/overrides.json")

    def test_it_does_not_depend_on_the_working_directory(self):
        from coreyard.config import data_path

        here = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            moved = data_path("out/overrides.json")
        finally:
            os.chdir(here)
        self.assertEqual(moved, DATA_ROOT / "out/overrides.json")

    def test_an_absolute_path_is_left_exactly_as_written(self):
        from coreyard.config import data_path

        self.assertEqual(data_path("/etc/coreyard/overrides.json"),
                         Path("/etc/coreyard/overrides.json"))

    def test_a_home_relative_path_is_expanded_not_nested(self):
        from coreyard.config import data_path

        resolved = data_path("~/overrides.json")
        self.assertEqual(resolved, Path.home() / "overrides.json")
        self.assertTrue(resolved.is_absolute())

    def test_nothing_configured_stays_nothing(self):
        from coreyard.config import data_path

        for empty in (None, "", "   "):
            self.assertIsNone(data_path(empty))

    def test_the_override_loader_uses_it(self):
        """The specific path that produced the popup."""
        from coreyard import overrides

        here = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            with self.assertRaises(overrides.OverrideError) as caught:
                overrides.load("out/definitely-not-there.json")
        finally:
            os.chdir(here)
        # The message must name where it actually looked, or the next person reads
        # "not found" and goes hunting for a file that is sitting right there.
        self.assertIn(str(DATA_ROOT), str(caught.exception))


class TheSuiteIsNotTheInstallation(unittest.TestCase):
    """A test that writes into the yard's own data root is not an offline test.

    `cli` records every run in the history, refusals included, so that a scheduled job which
    starts refusing does not read as one nobody scheduled. An in-process CLI test therefore
    filed a failed `sync delta` into the live sync-state database every time the suite ran,
    and `status` showed it to the operator as the last delta run.

    `tests/__init__.py` points `COREYARD_HOME` at a scratch directory, and it is imported
    only when the tests are discovered as a package — which is why the documented command
    passes `-t .`. This asserts the arrangement actually took effect, so running the suite
    the old way fails loudly here instead of quietly writing to production.
    """

    def test_the_suite_runs_against_a_scratch_data_root(self):
        self.assertNotEqual(
            DATA_ROOT, REPO_ROOT,
            "the suite is writing into the installation itself — run it as documented: "
            "python -m unittest discover -t . -s tests")

    def test_the_state_database_is_not_the_installations(self):
        from coreyard.state import DEFAULT_STATE_DB

        self.assertFalse(str(DEFAULT_STATE_DB).startswith(str(REPO_ROOT)))


class TwoInstallationsRunningAtOnce(unittest.TestCase):
    """DIST-04: two yards, one machine, and nothing shared between them *while they run*.

    `TwoInstallationsOnOneHost` above proves the paths separate by construction, which is
    the rule. This is the arrangement an operator actually creates — one host, two crontabs,
    two stores — so it starts both and then asks each home what it believes.

    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def home(self, name: str) -> Path:
        path = Path(self.tmp.name) / name
        path.mkdir()
        return path

    def spawn(self, home: Path, *args: str) -> subprocess.Popen:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("COREYARD_", "SMB_", "YMS_", "SHOPIFY_", "STORE_"))}
        env["COREYARD_HOME"] = str(home)
        env["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.Popen([sys.executable, "-m", "coreyard", *args],
                                env=env, cwd=home, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    def test_each_keeps_its_own_state_while_the_other_is_running(self):
        first, second = self.home("yard-a"), self.home("yard-b")
        for home in (first, second):
            setup = self.spawn(home, "init", "--demo")
            setup.communicate(timeout=180)
            self.assertEqual(setup.returncode, 0)

        # Both at once, against the same installed code.
        runs = [self.spawn(home, "sync", "--sink", "csv") for home in (first, second)]
        outputs = [run.communicate(timeout=180)[0] for run in runs]
        for home, run, output in zip((first, second), runs, outputs):
            with self.subTest(home=home.name):
                self.assertEqual(run.returncode, 0, output)

        for home in (first, second):
            with self.subTest(home=home.name):
                self.assertTrue((home / "coreyard_sync_state.sqlite3").exists())
                self.assertTrue((home / "out" / "products.csv").exists())

    def test_neither_leaves_anything_in_the_other(self):
        first, second = self.home("yard-a"), self.home("yard-b")
        self.spawn(first, "init", "--demo").communicate(timeout=180)
        self.spawn(first, "sync", "--sink", "csv").communicate(timeout=180)
        self.assertEqual(sorted(p.name for p in second.iterdir()), [])

    def test_their_locks_are_not_the_same_lock(self):
        """A shared lock would make two unrelated yards take turns for no reason."""
        first, second = self.home("yard-a"), self.home("yard-b")
        for home in (first, second):
            self.spawn(home, "init", "--demo").communicate(timeout=180)
            self.spawn(home, "--lock", "sync", "status").communicate(timeout=180)
        locks = [next((home / "out").glob(".sync.lock"), None) for home in (first, second)]
        self.assertTrue(all(locks), "each run takes a lock under its own data root")
        self.assertNotEqual(locks[0], locks[1])
