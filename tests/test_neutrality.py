"""The one exception to vendor neutrality, and how narrow it is.

A public project needs a reachable maintainer, so `scripts/check_neutrality.py` permits one
contact address in the four files whose purpose is to carry one. An exception to a rule that
exists to stop a leak is worth a test: the failure mode is not that it stops working, it is
that it quietly widens until the rule it qualifies means nothing.

The forbidden term is derived from the permitted address rather than written here, because
writing it would fail the very check under test.
"""

import unittest

import scripts.check_neutrality as neutrality

CONTACT = neutrality.CONTACT
IDENTITY = CONTACT.split("@")[1].split(".")[0]      # the banned word inside the address


class ThePermittedContact(unittest.TestCase):
    def test_it_is_allowed_in_a_file_that_carries_a_contact(self):
        for rel in sorted(neutrality.CONTACT_FILES):
            self.assertIsNone(neutrality.offending_reason_in(f"Email {CONTACT} for help.", rel),
                              f"the contact should be permitted in {rel}")

    def test_case_does_not_defeat_the_permission(self):
        self.assertIsNone(
            neutrality.offending_reason_in(f"Email {CONTACT.upper()}.", "README.md"))

    def test_it_is_still_forbidden_anywhere_else(self):
        for rel in ("docs/SETUP.md", "coreyard/config.py", ".env.example"):
            self.assertIsNotNone(neutrality.offending_reason_in(f"Email {CONTACT}.", rel),
                                 f"the contact should not be permitted in {rel}")


class TheRuleItQualifies(unittest.TestCase):
    def test_the_bare_identity_is_still_forbidden_in_a_contact_file(self):
        """Permitting the address must not permit the words inside it."""
        self.assertIsNotNone(
            neutrality.offending_reason_in(f"SMB_HOST={IDENTITY}-server.local", "README.md"))

    def test_a_second_occurrence_on_the_same_line_is_still_caught(self):
        self.assertIsNotNone(
            neutrality.offending_reason_in(f"Email {CONTACT} — host {IDENTITY}.local",
                                           "SECURITY.md"))

    def test_the_permission_covers_exactly_the_files_it_names(self):
        self.assertEqual(
            neutrality.CONTACT_FILES,
            {"README.md", "SECURITY.md", "CONTRIBUTING.md",
             ".github/ISSUE_TEMPLATE/config.yml", "scripts/check_neutrality.py"},
            "widening this set widens the exception; do it deliberately or not at all")
