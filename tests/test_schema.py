"""Query building from a site's schema mapping, offline with a synthetic mapping."""

import unittest

from coreyard.yms.schema import SchemaError, SourceSchema

MAPPING = SourceSchema(
    select={"r_number": "i.PartId", "part_type": "pt.Name", "price": "i.Price",
            "location": "i.Bin"},
    source="dbo.Parts i LEFT JOIN dbo.Types pt ON i.TypeId = pt.TypeId",
    scope="i.Price > 0 AND i.Qty > 0",
    order_by="i.PartId",
    images_filter="i.HasPhotos = 1",
)


class LookupQuery(unittest.TestCase):
    def test_selects_the_requested_parts(self):
        sql = MAPPING.build_lookup_query(["51", "52"])
        self.assertIn("i.PartId IN ('51', '52')", sql)
        self.assertIn("i.Bin AS location", sql)

    def test_ignores_scope(self):
        """A part that just sold has left scope; the work order still needs its detail."""
        sql = MAPPING.build_lookup_query(["51"])
        self.assertNotIn("i.Qty > 0", sql)

    def test_deduplicates_and_requires_input(self):
        self.assertEqual(MAPPING.build_lookup_query(["7", "7"]).count("'7'"), 1)
        with self.assertRaises(SchemaError):
            MAPPING.build_lookup_query([])

    def test_rejects_injection_from_an_order_sku(self):
        """A draft order can set a line item's SKU to arbitrary text."""
        for hostile in ["1'); DROP TABLE Parts--", "1 OR 1=1", "'", "1;2", "x" * 40, ""]:
            with self.assertRaises(SchemaError, msg=hostile):
                MAPPING.build_lookup_query([hostile])

    def test_accepts_non_numeric_but_plausible_ids(self):
        """R# is numeric here, but the engine must not assume every yard agrees."""
        self.assertIn("'A-1234_B'", MAPPING.build_lookup_query(["A-1234_B"]))


class ScopedQuery(unittest.TestCase):
    def test_applies_scope_and_order(self):
        sql = MAPPING.build_query()
        self.assertIn("WHERE i.Price > 0 AND i.Qty > 0", sql)
        self.assertIn("ORDER BY i.PartId", sql)
        self.assertNotIn("TOP", sql)

    def test_limit_and_images_filter(self):
        sql = MAPPING.build_query(limit=25, images_only=True)
        self.assertIn("SELECT TOP 25", sql)
        self.assertIn("AND i.HasPhotos = 1", sql)

    def test_inventory_count_is_distinct_scoped_and_optionally_photo_gated(self):
        sql = MAPPING.build_inventory_count_query()
        self.assertIn("COUNT_BIG(DISTINCT i.PartId) AS part_count", sql)
        self.assertIn("WHERE (i.Price > 0 AND i.Qty > 0)", sql)
        self.assertNotIn("i.HasPhotos = 1", sql)
        self.assertIn("i.HasPhotos = 1",
                      MAPPING.build_inventory_count_query(images_only=True))

    def test_required_fields_are_enforced(self):
        with self.assertRaises(SchemaError):
            SourceSchema.from_dict({"select": {"r_number": "x"}, "source": "t", "scope": "1=1"})

    def test_bounded_page_uses_stable_identity_cursor(self):
        sql = MAPPING.build_page_query(1000, after="A'2000", images_only=True)
        self.assertIn("SELECT TOP 1000", sql)
        self.assertIn("AND i.HasPhotos = 1", sql)
        self.assertIn("AND i.PartId > N'A''2000'", sql)
        self.assertIn("ORDER BY i.PartId", sql)

    def test_bounded_page_rejects_invalid_bounds(self):
        with self.assertRaises(ValueError):
            MAPPING.build_page_query(0)


if __name__ == "__main__":
    unittest.main()
