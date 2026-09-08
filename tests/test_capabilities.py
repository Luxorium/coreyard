"""What an installation is configured to do, and what a diagnostic may therefore claim.

Two defects motivate this file, and both are the same shape: a check that reports a problem
where none exists teaches people to stop reading checks.

* A yard running ``COREYARD_SOURCE=tabular:parts.csv`` has no SMB credentials, no photo
  share and no ``schema.json``. ``doctor`` reported three FAIL lines and told the operator
  to fix an installation that was already correct.
* Order booking and the listing portal are opt-in. Their absence is a choice, not a fault,
  and a release that treats "off" as "broken" cannot ship an optional feature at all.

Every test runs against a synthetic environment with ``load=False``, so the site's real
``.env`` cannot decide the outcome.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coreyard import capabilities
from coreyard.capabilities import MISSING, OFF, ON

# Enough of a source database installation to be "configured", with nothing real behind it.
DATABASE_ENV = {
    "COREYARD_SOURCE": "database",
    "SMB_HOST": "10.0.0.9", "SMB_USER": "reader", "SMB_PASSWORD": "secret",
    "SMB_IMAGES_SHARE": "Images",
}


def detect(**environ):
    """Capabilities for exactly this environment and nothing else."""
    with mock.patch.dict(os.environ, environ, clear=True):
        return capabilities.detect(load=False)


class TabularInstallation(unittest.TestCase):
    """A file-backed yard is a supported installation, not a broken database one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmp.name) / "parts.csv"
        self.csv.write_text("r_number,part_type,price\n51,TAIL LAMP,89.00\n",
                            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_readable_file_is_a_configured_source(self):
        caps = detect(COREYARD_SOURCE=f"tabular:{self.csv}")
        self.assertEqual(caps.source_kind, "tabular")
        self.assertTrue(caps.tabular)
        self.assertTrue(caps.enabled("source"))

    def test_it_does_not_ask_a_tabular_yard_for_smb_credentials(self):
        """The whole point of the tabular source is that it needs none."""
        caps = detect(COREYARD_SOURCE=f"tabular:{self.csv}")
        self.assertTrue(caps.enabled("source"))
        self.assertNotEqual(caps.get("source").state, MISSING)

    def test_schema_json_is_not_required_and_not_reported_missing(self):
        """A tabular file *is* its own mapping: the columns are Part field names."""
        caps = detect(COREYARD_SOURCE=f"tabular:{self.csv}")
        self.assertEqual(caps.get("schema").state, OFF)
        self.assertIn("tabular", caps.get("schema").detail)

    def test_database_only_capabilities_are_off_rather_than_missing(self):
        caps = detect(COREYARD_SOURCE=f"tabular:{self.csv}", YMS_WRITE_ORDERS="1")
        for name in ("order_booking", "delta", "donor_photos"):
            self.assertEqual(caps.get(name).state, OFF, name)

    def test_a_missing_file_is_a_real_failure(self):
        caps = detect(COREYARD_SOURCE="tabular:/nonexistent/parts.csv")
        self.assertEqual(caps.get("source").state, MISSING)
        self.assertIn("does not exist", caps.get("source").detail)

    def test_a_tabular_source_with_no_path_is_a_real_failure(self):
        self.assertEqual(detect(COREYARD_SOURCE="tabular").get("source").state, MISSING)

    def test_photos_come_from_a_directory_and_its_absence_is_a_failure(self):
        photos = Path(self.tmp.name) / "photos"
        caps = detect(COREYARD_SOURCE=f"tabular:{self.csv}",
                      COREYARD_SOURCE_IMAGES=str(photos))
        self.assertEqual(caps.get("photos").state, MISSING)
        photos.mkdir()
        caps = detect(COREYARD_SOURCE=f"tabular:{self.csv}",
                      COREYARD_SOURCE_IMAGES=str(photos))
        self.assertEqual(caps.get("photos").state, ON)

    def test_no_photo_directory_is_off_not_missing(self):
        """An unphotographed catalogue publishes without images; that is not a fault."""
        self.assertEqual(
            detect(COREYARD_SOURCE=f"tabular:{self.csv}").get("photos").state, OFF)


class DatabaseInstallation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.schema = Path(self.tmp.name) / "schema.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_smb_credentials_are_named_individually(self):
        caps = detect(COREYARD_SOURCE="database", SMB_HOST="10.0.0.9")
        self.assertEqual(caps.get("source").state, MISSING)
        self.assertIn("SMB_USER", caps.get("source").detail)
        self.assertIn("SMB_PASSWORD", caps.get("source").detail)

    def test_an_unmapped_schema_is_missing_and_says_how_to_fix_it(self):
        caps = detect(**DATABASE_ENV, COREYARD_SCHEMA=str(self.schema))
        self.assertEqual(caps.get("schema").state, MISSING)
        self.assertIn("coreyard schema", caps.get("schema").detail)

    def test_an_unreadable_schema_is_missing_rather_than_a_traceback(self):
        self.schema.write_text("{ not json", encoding="utf-8")
        caps = detect(**DATABASE_ENV, COREYARD_SCHEMA=str(self.schema))
        self.assertEqual(caps.get("schema").state, MISSING)
        self.assertIn("validate", caps.get("schema").detail)


class OptionalFeatures(unittest.TestCase):
    """Opt-in means the customer chooses, not that its absence is a defect (REL-02)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmp.name) / "parts.csv"
        self.csv.write_text("r_number,price\n51,89.00\n", encoding="utf-8")
        self.base = {"COREYARD_SOURCE": f"tabular:{self.csv}"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_shopify_credentials_is_off_because_the_csv_sink_still_works(self):
        cap = detect(**self.base).get("shopify")
        self.assertEqual(cap.state, OFF)
        self.assertIn("CSV", cap.detail)

    def test_half_configured_shopify_credentials_are_a_failure(self):
        """One of the pair set is a mistake in progress, not a deliberate choice."""
        cap = detect(**self.base, SHOPIFY_STORE="x.myshopify.com").get("shopify")
        self.assertEqual(cap.state, MISSING)
        self.assertIn("SHOPIFY_ADMIN_TOKEN", cap.detail)

    def test_both_shopify_credentials_enable_the_store(self):
        caps = detect(**self.base, SHOPIFY_STORE="x.myshopify.com",
                      SHOPIFY_ADMIN_TOKEN="shpat_x")
        self.assertTrue(caps.enabled("shopify"))

    def test_a_capability_report_never_repeats_a_secret(self):
        caps = detect(**self.base, SHOPIFY_STORE="x.myshopify.com",
                      SHOPIFY_ADMIN_TOKEN="shpat_supersecret",
                      SMB_PASSWORD="hunter2", EBAY_PORTAL_PASSWORD="hunter3")
        printed = json.dumps(caps.summary())
        for secret in ("shpat_supersecret", "hunter2", "hunter3"):
            self.assertNotIn(secret, printed)

    def test_orders_are_off_without_a_signing_secret(self):
        self.assertEqual(detect(**self.base).get("orders").state, OFF)

    def test_orders_turn_on_with_either_accepted_secret(self):
        for key in ("SHOPIFY_WEBHOOK_SECRET", "SHOPIFY_CLIENT_SECRET"):
            self.assertTrue(detect(**self.base, **{key: "s3cret"}).enabled("orders"), key)

    def test_the_portal_is_off_without_a_map(self):
        """A site that never configured the listing portal has not failed to configure it.

        The data root is redirected because a maintainer's own checkout carries a
        portal.json the default lookup would find, and the behaviour under test is what a
        customer's install does.
        """
        import coreyard.config as config

        with mock.patch.object(config, "DATA_ROOT", Path(self.tmp.name)):
            cap = detect(**self.base).get("portal")
        self.assertEqual(cap.state, OFF)
        self.assertIn("EBAY_PORTAL_FILE", cap.detail)

    def test_a_configured_portal_map_that_is_not_there_is_a_failure(self):
        cap = detect(**self.base, EBAY_PORTAL_FILE="/nonexistent/portal.json").get("portal")
        self.assertEqual(cap.state, MISSING)

    def test_unset_publications_is_off_and_explains_the_consequence(self):
        cap = detect(**self.base).get("publications")
        self.assertEqual(cap.state, OFF)
        self.assertIn("sales channel", cap.detail)


class UnknownSource(unittest.TestCase):
    def test_an_unsupported_source_fails_before_anything_else_is_attempted(self):
        """REL-01: an unsupported combination must fail with an explanation, not a
        traceback halfway through a run."""
        caps = detect(COREYARD_SOURCE="postgres://yard")
        self.assertEqual(caps.get("source").state, MISSING)
        self.assertIn("unknown source", caps.get("source").detail)


if __name__ == "__main__":
    unittest.main()
