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
        self.assertEqual(DATA_ROOT, REPO_ROOT)

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
        """`REPO_ROOT` is not obsolete — it is now only ever the installation."""
        from coreyard import schedule
        from coreyard.yms import schema

        self.assertTrue(str(schema.EXAMPLE_PATH).startswith(str(REPO_ROOT)))
        self.assertTrue(str(schedule.launcher()).startswith(str(REPO_ROOT)))

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
