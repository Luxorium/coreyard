"""One store file instead of four, without moving anyone's cheese.

The risk in consolidating configuration is not that the new form fails — it is that the old
form quietly starts resolving somewhere else, and a storefront publishes wording nobody
reviewed. So the rule these tests pin is: an explicitly named file always wins.
"""

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from coreyard import store


def write(tmp, data, name="store.json"):
    path = Path(tmp) / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class Loading(unittest.TestCase):
    def setUp(self):
        store.forget()
        self.addCleanup(store.forget)

    def env(self, **values):
        return mock.patch.dict(os.environ, values, clear=False)

    def test_sections_are_read(self):
        with TemporaryDirectory() as tmp:
            path = write(tmp, {"version": 1, "profile": {"condition": "Used"},
                               "weights": {}, "shipping": {}, "orders": {}})
            with self.env(STORE_FILE=str(path)):
                self.assertEqual(store.section("profile"), {"condition": "Used"})

    def test_an_absent_section_is_none_not_an_error(self):
        with TemporaryDirectory() as tmp:
            path = write(tmp, {"version": 1, "profile": {}})
            with self.env(STORE_FILE=str(path)):
                self.assertIsNone(store.section("orders"))

    def test_a_named_file_that_does_not_exist_is_an_error(self):
        """The same rule the individual settings follow: asked-for and absent is a fault."""
        with self.env(STORE_FILE="/nonexistent/store.json"):
            with self.assertRaises(store.StoreFileError):
                store.load()

    def test_malformed_json_says_so(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            path.write_text("{not json", encoding="utf-8")
            with self.env(STORE_FILE=str(path)):
                with self.assertRaises(store.StoreFileError):
                    store.load()

    def test_an_unknown_section_is_refused_by_name(self):
        """A typo'd section would otherwise be silently ignored policy."""
        with TemporaryDirectory() as tmp:
            path = write(tmp, {"profile": {}, "wieghts": {}})
            with self.env(STORE_FILE=str(path)):
                with self.assertRaises(store.StoreFileError) as caught:
                    store.load()
        self.assertIn("wieghts", str(caught.exception))

    def test_a_section_must_be_an_object(self):
        with TemporaryDirectory() as tmp:
            path = write(tmp, {"profile": ["not", "an", "object"]})
            with self.env(STORE_FILE=str(path)):
                with self.assertRaises(store.StoreFileError):
                    store.load()

    def test_an_unknown_section_name_is_rejected_at_the_api(self):
        with self.assertRaises(ValueError):
            store.section("nonsense")


class Precedence(unittest.TestCase):
    """The half of this change that must never surprise an existing installation."""

    def setUp(self):
        store.forget()
        self.addCleanup(store.forget)

    def test_an_explicit_file_wins_over_the_section(self):
        seen = {}

        def loader(path):
            seen["path"] = path
            return "from-file"

        def from_dict(data):
            seen["data"] = data
            return "from-section"

        with TemporaryDirectory() as tmp:
            combined = write(tmp, {"profile": {"condition": "Used"}})
            with mock.patch.dict(os.environ, {"STORE_FILE": str(combined),
                                              "STORE_PROFILE_FILE": "/some/profile.json"}):
                result = store.resolve("profile", "STORE_PROFILE_FILE", loader, from_dict)
        self.assertEqual(result, "from-file")
        self.assertEqual(seen["path"], "/some/profile.json")
        self.assertNotIn("data", seen)

    def test_the_section_is_used_when_no_file_is_named(self):
        with TemporaryDirectory() as tmp:
            combined = write(tmp, {"profile": {"condition": "Used"}})
            with mock.patch.dict(os.environ, {"STORE_FILE": str(combined)}, clear=False):
                os.environ.pop("STORE_PROFILE_FILE", None)
                result = store.resolve("profile", "STORE_PROFILE_FILE",
                                       lambda p: "from-file", lambda d: d)
        self.assertEqual(result, {"condition": "Used"})

    def test_neither_falls_back_to_the_loaders_own_default(self):
        with TemporaryDirectory() as tmp:
            combined = write(tmp, {"version": 1})
            with mock.patch.dict(os.environ, {"STORE_FILE": str(combined)}, clear=False):
                os.environ.pop("STORE_PROFILE_FILE", None)
                result = store.resolve("profile", "STORE_PROFILE_FILE",
                                       lambda p: f"default({p})", lambda d: d)
        self.assertEqual(result, "default(None)")


class Validation(unittest.TestCase):
    """`coreyard validate` must catch a bad section before a run does."""

    def setUp(self):
        store.forget()
        self.addCleanup(store.forget)

    def check(self, data):
        from coreyard.validate import Result, check_store
        with TemporaryDirectory() as tmp:
            result = Result()
            check_store(str(write(tmp, data)), result)
            return result

    def test_a_valid_file_passes_and_says_what_it_found(self):
        result = self.check({"version": 1, "profile": {"condition": "Used"}})
        self.assertTrue(result.ok)
        self.assertTrue(any("profile" in note for note in result.notes))

    def test_an_unknown_profile_key_is_reported_against_its_section(self):
        result = self.check({"profile": {"conditon": "Used"}})
        self.assertFalse(result.ok)
        self.assertTrue(any("store file [profile]" in e for e in result.errors))

    def test_comment_keys_are_not_mistaken_for_policy(self):
        """JSON has no comments, so a leading underscore is how a file explains itself."""
        self.assertTrue(self.check({"profile": {"_comment": "hello"}}).ok)

    def test_no_store_file_is_not_an_error(self):
        from coreyard.validate import Result, check_store
        result = Result()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STORE_FILE", None)
            check_store(None, result)
        self.assertTrue(result.ok)


class ShippedExample(unittest.TestCase):
    def test_the_bundled_example_validates(self):
        """The file a newcomer copies must pass this project's own validator."""
        from coreyard.config import REPO_ROOT
        from coreyard.validate import Result, check_store
        store.forget()
        result = Result()
        check_store(str(REPO_ROOT / "store.example.json"), result)
        self.assertTrue(result.ok, result.errors)
