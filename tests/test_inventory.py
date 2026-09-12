"""Extract mapping: identifiers, value coercion, and schema-driven query building.

These tests never read the site's real ``schema.json`` — the repository ships no vendor
schema, so the fixture below stands in for one.
"""

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from coreyard.yms import schema
from coreyard.yms.inventory import (
    FETCH_PAGE_SIZE,
    _clean_note,
    fetch_parts,
    row_to_part,
    source_inventory_count,
)

FIXTURE = {
    "select": {
        "r_number": "i.PartId",
        "part_type": "RTRIM(CAST(pt.Name AS varchar(20)))",
        "price": "i.Price",
        "stock_number": "RTRIM(CAST(i.DonorId AS varchar(10)))",
    },
    "source": "dbo.Parts i LEFT JOIN dbo.PartTypes pt ON i.TypeId = pt.TypeId",
    "scope": "i.Price > 0 AND i.Qty > 0",
    "images_filter": "i.HasPhotos = 1",
    "order_by": "i.PartId",
}


class SchemaDrivenQuery(unittest.TestCase):
    def test_query_is_built_from_the_supplied_mapping(self):
        sql = schema.SourceSchema.from_dict(FIXTURE).build_query()
        self.assertIn("i.PartId AS r_number", sql)
        self.assertIn("FROM dbo.Parts i LEFT JOIN dbo.PartTypes pt", sql)
        self.assertIn("WHERE i.Price > 0 AND i.Qty > 0", sql)
        self.assertIn("ORDER BY i.PartId", sql)

    def test_limit_and_images_filter_are_optional(self):
        s = schema.SourceSchema.from_dict(FIXTURE)
        self.assertIn("TOP 25", s.build_query(limit=25))
        self.assertNotIn("TOP", s.build_query())
        self.assertIn("i.HasPhotos = 1", s.build_query(images_only=True))
        self.assertNotIn("HasPhotos", s.build_query(images_only=False))

    def test_missing_required_field_is_reported_clearly(self):
        broken = {**FIXTURE, "select": {"r_number": "i.PartId"}}
        with self.assertRaises(schema.SchemaError) as ctx:
            schema.SourceSchema.from_dict(broken)
        self.assertIn("part_type", str(ctx.exception))

    def test_missing_section_is_reported_clearly(self):
        with self.assertRaises(schema.SchemaError):
            schema.SourceSchema.from_dict({"select": FIXTURE["select"]})



class IdentifierMapping(unittest.TestCase):
    def test_identifiers_map_to_their_actual_meanings(self):
        part = row_to_part({
            "r_number": "51",
            "stock_number": "251026",
            "interchange_number": "545-01883",
            "interchange_code": "01883",
            "part_type_code": "545",
            "part_type": "Anti-lock Brake Pts",
            "price": "125.00",
            "quantity": "1",
        })
        self.assertEqual(part.r_number, "51")
        self.assertEqual(part.stock_number, "251026")
        self.assertEqual(part.interchange_number, "545-01883")
        self.assertEqual(part.interchange_code, "01883")
        self.assertEqual(part.price, Decimal("125.00"))

    def test_side_is_kept_off_the_part_type(self):
        part = row_to_part({"r_number": "9", "part_type": "Side View Mirror",
                            "left_right": "L", "price": "20.00"})
        self.assertEqual(part.side, "Left")
        self.assertEqual(part.part_type, "Side View Mirror")

    def test_null_arrives_as_a_string_and_is_coerced_away(self):
        part = row_to_part({"r_number": "9", "part_type": "Alternator",
                            "price": "10.00", "mileage": "NULL", "grade": "NULL"})
        self.assertIsNone(part.mileage)
        self.assertIsNone(part.grade)


class NoteCleaning(unittest.TestCase):
    def test_legacy_header_is_stripped(self):
        self.assertEqual(
            _clean_note("Old ID 6565||ENG||2060||10000.001,,,(3.0L, VIN C, 4th digit)"),
            "3.0L, VIN C, 4th digit",
        )

    def test_plain_text_passes_through(self):
        self.assertEqual(_clean_note("Good condition, tested"), "Good condition, tested")

    def test_empty_and_null_become_none(self):
        self.assertIsNone(_clean_note(""))
        self.assertIsNone(_clean_note("NULL"))


def _row(r_number: int) -> dict:
    return {"r_number": str(r_number), "part_type": "Engine", "price": "10.00"}


class PagedFetch(unittest.TestCase):
    @patch("coreyard.yms.inventory.query")
    @patch("coreyard.yms.inventory.connect")
    @patch("coreyard.yms.inventory.schema.load")
    def test_full_extract_reads_bounded_pages(self, load, connect, run_query):
        mapping = load.return_value
        mapping.build_page_query.side_effect = lambda size, after, **_: f"page:{after}:{size}"
        connect.return_value.__enter__.return_value = MagicMock()
        # Distinct R#s, because that is what keyset paging reads: the next page starts
        # *after* the last row's identifier, so a page of repeats could not occur — and a
        # repeated R# is now held back as ambiguous, which would make this test about
        # something else entirely.
        run_query.side_effect = [
            [_row(i) for i in range(1, FETCH_PAGE_SIZE + 1)],
            [_row(FETCH_PAGE_SIZE + 1)],
        ]

        parts = fetch_parts()

        self.assertEqual(len(parts), FETCH_PAGE_SIZE + 1)
        self.assertEqual(
            [call.args[1] for call in run_query.call_args_list],
            [f"page:None:{FETCH_PAGE_SIZE}", f"page:{FETCH_PAGE_SIZE}:{FETCH_PAGE_SIZE}"],
        )
        self.assertEqual(connect.call_count, 2)
    @patch("coreyard.yms.inventory.query")
    @patch("coreyard.yms.inventory.connect")
    @patch("coreyard.yms.inventory.schema.load")
    def test_limit_caps_the_last_page(self, load, connect, run_query):
        mapping = load.return_value
        mapping.build_page_query.side_effect = lambda size, after, **_: f"page:{after}:{size}"
        connect.return_value.__enter__.return_value = MagicMock()
        run_query.side_effect = [
            [_row(i) for i in range(1, FETCH_PAGE_SIZE + 1)],
            [_row(i) for i in range(FETCH_PAGE_SIZE + 1, FETCH_PAGE_SIZE + 251)],
        ]

        limit = FETCH_PAGE_SIZE + 250
        self.assertEqual(len(fetch_parts(limit=limit)), limit)
        self.assertEqual(
            [call.args[1] for call in run_query.call_args_list],
            [f"page:None:{FETCH_PAGE_SIZE}", f"page:{FETCH_PAGE_SIZE}:250"],
        )
        self.assertEqual(connect.call_count, 2)


class InventoryCount(unittest.TestCase):
    @patch("coreyard.yms.inventory.query")
    @patch("coreyard.yms.inventory.connect")
    @patch("coreyard.yms.inventory.schema.load")
    def test_exact_count_uses_one_aggregate_without_the_photo_gate(
        self, load, connect, run_query
    ):
        mapping = load.return_value
        mapping.build_inventory_count_query.return_value = "count sql"
        connect.return_value.__enter__.return_value = MagicMock()
        run_query.return_value = [{"part_count": "27163"}]

        self.assertEqual(source_inventory_count(), 27163)

        mapping.build_inventory_count_query.assert_called_once_with(images_only=False)
        run_query.assert_called_once()


if __name__ == "__main__":
    unittest.main()
