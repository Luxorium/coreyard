"""One definition of "listable", shared by sync, reconciliation, bulk publishing and audit.

When those four disagree the storefront ends up in a state none of them can explain: sync
publishes an unphotographed part, reconciliation calls it unlistable and archives it, and
the next sync publishes it again. The photo policy therefore lives in one function that all
of them call.
"""

import os
import unittest

from coreyard import config
from coreyard.yms import inventory
from coreyard.yms.schema import SchemaError, SourceSchema

WITH_PHOTOS = SourceSchema(
    select={"r_number": "i.PartId", "part_type": "pt.Name", "price": "i.Price"},
    source="dbo.Parts i",
    scope="i.Price > 0 AND i.Qty > 0",
    order_by="i.PartId",
    images_filter="i.HasPhoto = 1",
)

WITHOUT_PHOTOS = SourceSchema(
    select={"r_number": "i.PartId", "part_type": "pt.Name", "price": "i.Price"},
    source="dbo.Parts i",
    scope="i.Price > 0 AND i.Qty > 0",
    order_by="i.PartId",
)


class Setting(unittest.TestCase):
    def setUp(self):
        original = os.environ.get("STORE_REQUIRE_IMAGES")
        self.addCleanup(
            lambda: os.environ.__setitem__("STORE_REQUIRE_IMAGES", original)
            if original is not None else os.environ.pop("STORE_REQUIRE_IMAGES", None))

    def _set(self, value):
        if value is None:
            os.environ.pop("STORE_REQUIRE_IMAGES", None)
        else:
            os.environ["STORE_REQUIRE_IMAGES"] = value

    def test_off_by_default_so_an_upgrade_changes_nothing(self):
        self._set(None)
        self.assertFalse(config.require_images())
        self.assertFalse(inventory.photos_required(WITH_PHOTOS))

    def test_a_site_can_turn_it_on(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            self._set(value)
            self.assertTrue(inventory.photos_required(WITH_PHOTOS), msg=value)

    def test_an_explicit_no_is_respected(self):
        for value in ("0", "false", "no", "off"):
            self._set(value)
            self.assertFalse(inventory.photos_required(WITH_PHOTOS), msg=value)

    def test_an_unreadable_value_is_an_error_rather_than_a_silent_no(self):
        """A policy that stopped applying because of a typo is invisible in a listing."""
        self._set("maybe")
        with self.assertRaises(RuntimeError):
            config.require_images()

    def test_requiring_photos_without_a_way_to_tell_is_refused(self):
        self._set("true")
        with self.assertRaises(SchemaError):
            inventory.photos_required(WITHOUT_PHOTOS)

    def test_checking_the_gate_does_not_pull_in_dotenv(self):
        before = dict(os.environ)
        self._set("false")
        config.require_images()
        os.environ.pop("STORE_REQUIRE_IMAGES", None)
        self.assertEqual({k: v for k, v in os.environ.items()},
                         {k: v for k, v in before.items()})


class IdentityQuery(unittest.TestCase):
    """Reconciliation asks "which R#s may be listed", not "render me a catalogue"."""

    def test_it_selects_only_the_identity_column(self):
        sql = WITH_PHOTOS.build_identity_page_query(250)
        self.assertIn("SELECT TOP 250", sql)
        self.assertIn("i.PartId AS r_number", sql)
        self.assertNotIn("pt.Name", sql)

    def test_it_carries_the_same_scope_as_the_extract(self):
        sql = WITH_PHOTOS.build_identity_page_query(250)
        self.assertIn("i.Price > 0 AND i.Qty > 0", sql)

    def test_the_photo_predicate_is_applied_only_when_asked(self):
        self.assertIn("i.HasPhoto = 1",
                      WITH_PHOTOS.build_identity_page_query(250, images_only=True))
        self.assertNotIn("i.HasPhoto = 1",
                         WITH_PHOTOS.build_identity_page_query(250, images_only=False))

    def test_paging_is_keyset_on_the_identity(self):
        sql = WITH_PHOTOS.build_identity_page_query(250, after="1200")
        self.assertIn("i.PartId > N'1200'", sql)
        self.assertTrue(sql.rstrip().endswith("ORDER BY i.PartId"))

    def test_a_quote_in_the_cursor_cannot_break_out(self):
        sql = WITH_PHOTOS.build_identity_page_query(10, after="1200' OR 1=1 --")
        self.assertIn("N'1200'' OR 1=1 --'", sql)

    def test_a_zero_page_is_refused(self):
        with self.assertRaises(ValueError):
            WITH_PHOTOS.build_identity_page_query(0)


class DeltaAgreement(unittest.TestCase):
    """A delta run must reach the same listable verdict a full run would.

    Otherwise a delta publishes an unphotographed part, the next full sync archives it, and
    the two argue about it every few minutes forever.
    """

    MAPPING = SourceSchema(
        select={"r_number": "i.PartId", "part_type": "pt.Name", "price": "i.Price"},
        source="dbo.Parts i",
        scope="i.Price > 0",
        order_by="i.PartId",
        images_filter="i.HasPhoto = 1",
        modified_at="i.Changed",
    )

    def test_the_photo_predicate_joins_the_scope_verdict(self):
        sql = self.MAPPING.build_delta_query("2026-08-20T00:00:00", 250, images_only=True)
        self.assertIn("CASE WHEN ((i.Price > 0) AND (i.HasPhoto = 1)) THEN 1 ELSE 0 END", sql)

    def test_without_the_policy_the_verdict_is_scope_alone(self):
        sql = self.MAPPING.build_delta_query("2026-08-20T00:00:00", 250)
        self.assertIn("CASE WHEN (i.Price > 0) THEN 1 ELSE 0 END", sql)
        self.assertNotIn("HasPhoto", sql)

    def test_the_changed_since_filter_is_never_narrowed_by_the_policy(self):
        """A part that lost its photos must still be READ, so it can be reported out."""
        sql = self.MAPPING.build_delta_query("2026-08-20T00:00:00", 250, images_only=True)
        where = sql.split("WHERE", 1)[1]
        self.assertIn("i.Changed >", where)
        self.assertNotIn("AND i.HasPhoto", where)

    def test_the_photo_only_lookup_reaches_the_same_verdict(self):
        """A part looked up because its photos moved has to be judged the same way."""
        sql = self.MAPPING.build_lookup_query(["51"], with_scope=True, images_only=True)
        self.assertIn("CASE WHEN ((i.Price > 0) AND (i.HasPhoto = 1)) THEN 1 ELSE 0 END", sql)
        plain = self.MAPPING.build_lookup_query(["51"], with_scope=True)
        self.assertNotIn("HasPhoto", plain)


if __name__ == "__main__":
    unittest.main()
