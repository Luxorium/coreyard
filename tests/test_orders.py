"""Booking a storefront sale into the source database — offline.

Every test here runs against the placeholders in ``schema.example.json``. That is deliberate
twice over: it keeps a real installation's schema out of the repository, and it means the
published example is executed rather than merely documented, so it cannot quietly rot.

The batch is never sent anywhere. What is asserted is the SQL text, because for a write path
the text *is* the behaviour — the guards, the escaping and the tax stance are all statements
in it, and by the time a live database could tell us they were wrong it would be too late.
"""

import json
import unittest
from decimal import Decimal

from coreyard.config import bundled
from coreyard.models import Part
from coreyard.yms import schema
from coreyard.yms.orders import (
    MAX, OrderLine, OrderWriteError, SalesOrder, build_batch, from_shopify,
)

WRITE = schema.load(bundled("schema.example.json")).order_write

ORDER = SalesOrder(
    reference="#1042",
    lines=[OrderLine("51", 1, Decimal("89.00"), "MIRROR", "545-01883")],
    ship_name="Dana Ruiz",
    ship_address1="88 Elm St",
    ship_city="Austin",
    ship_state="TX",
    ship_postal="78701",
    ship_country="US",
)


def batch(order=ORDER, write=None):
    return build_batch(order, write or WRITE)


class ExampleMapping(unittest.TestCase):
    def test_the_shipped_example_is_complete_enough_to_build_from(self):
        """If the example lost a fragment, every installation copying it would break."""
        self.assertIsNotNone(WRITE)
        self.assertIn("INSERT", batch())

    def test_tax_flags_require_real_json_booleans(self):
        raw = json.loads(bundled("schema.example.json")
                         .read_text(encoding="utf-8"))["order_write"]
        for key in ("line_items_taxable", "recalculate_taxes",
                    "apply_customer_tax_rate"):
            with self.subTest(key=key):
                invalid = {**raw, key: "false"}
                with self.assertRaisesRegex(schema.SchemaError, "JSON boolean"):
                    schema.OrderWrite.from_dict(invalid)


class Escaping(unittest.TestCase):
    """Buyer-supplied text reaches a db_owner connection with no parameter binding."""

    def test_an_apostrophe_in_a_name_is_doubled(self):
        sql = batch(SalesOrder(reference="#1", lines=ORDER.lines, ship_name="Dana O'Brien"))
        self.assertIn("N'Dana O''Brien'", sql)

    def test_a_quoted_injection_cannot_close_the_literal(self):
        hostile = "'); DROP TABLE X; --"
        sql = batch(SalesOrder(reference="#1", lines=ORDER.lines, ship_address1=hostile))
        # The payload survives as data, doubled, and never as a statement boundary.
        self.assertIn("N'''); DROP TABLE X; --'", sql)
        self.assertNotIn("; DROP TABLE X; --'", sql.replace("N'''); DROP TABLE X; --'", ""))

    def test_the_order_reference_is_escaped_too(self):
        """It is the duplicate-check value, so it reaches a WHERE clause as well."""
        sql = batch(SalesOrder(reference="#1'--", lines=ORDER.lines))
        self.assertIn("N'#1''--'", sql)

    def test_control_characters_are_dropped(self):
        """A NUL truncates the string in some drivers, silently shortening an address."""
        sql = batch(SalesOrder(reference="#1", lines=ORDER.lines,
                               ship_address1="88 Elm\x00 St\r\n"))
        self.assertIn("N'88 Elm St'", sql)

    def test_overlong_values_are_cut_to_the_column_width(self):
        sql = batch(SalesOrder(reference="#1", lines=ORDER.lines, ship_city="x" * 200))
        self.assertIn("N'" + "x" * MAX["city"] + "'", sql)
        self.assertNotIn("x" * (MAX["city"] + 1), sql)


class Tax(unittest.TestCase):
    """The storefront collects and remits the sales tax; the yard system must not add it."""

    def test_line_items_are_not_taxable_by_default(self):
        self.assertIn("DECLARE @taxable    bit      = 0;", batch())

    def test_a_site_can_still_opt_into_taxable_lines(self):
        taxable = schema.OrderWrite(**{**WRITE.__dict__, "line_items_taxable": True})
        self.assertIn("DECLARE @taxable    bit      = 1;", batch(write=taxable))

    def test_recalculation_is_off_so_nothing_recomputes_tax_later(self):
        self.assertIn("DECLARE @recalc_tax bit      = 0;", batch())


class Guards(unittest.TestCase):
    def test_the_whole_order_is_one_abortable_transaction(self):
        sql = batch()
        self.assertIn("SET XACT_ABORT ON;", sql)
        self.assertEqual(sql.count("BEGIN TRANSACTION;"), 1)
        self.assertEqual(sql.count("COMMIT TRANSACTION;"), 1)

    def test_a_repeat_delivery_is_refused_before_anything_is_written(self):
        """Shopify redelivers on any doubt; a retry must not buy the part twice."""
        sql = batch()
        self.assertIn("@existing", sql)
        head, _, tail = sql.partition("IF @existing IS NOT NULL")
        self.assertIn("ROLLBACK TRANSACTION", tail.split("ELSE")[0])
        self.assertNotIn("INSERT", head)

    def test_ids_are_allocated_in_one_statement_never_read_then_written(self):
        """The application allocates from these same rows while we run."""
        sql = batch()
        self.assertEqual(sql.count("OUTPUT INSERTED.CounterValue INTO @out"), 4)
        self.assertIn("UPDLOCK", sql)

    def test_an_allocated_id_that_already_exists_aborts(self):
        sql = batch()
        for code in ("50002", "50003", "50004"):
            self.assertIn(f"THROW {code}", sql)

    def test_inventory_cannot_go_negative_or_miss(self):
        sql = batch()
        self.assertIn("QUANTITY_COLUMN >= @li_qty", sql)
        self.assertIn("IF @@ROWCOUNT <> 1", sql)
        self.assertIn("THROW 50005", sql)

    def test_the_oversell_message_names_the_part(self):
        """The error is what somebody reads at 6am; it has to say which part."""
        self.assertIn("R#51 is not available", batch())

    def test_nothing_is_ever_deleted(self):
        self.assertNotIn("DELETE FROM dbo.", batch())
        self.assertNotIn("DROP ", batch())


class Refusals(unittest.TestCase):
    def test_an_order_with_no_lines_is_refused(self):
        with self.assertRaises(OrderWriteError):
            batch(SalesOrder(reference="#1", lines=[]))

    def test_a_blank_reference_is_refused_because_it_is_the_duplicate_guard(self):
        with self.assertRaises(OrderWriteError):
            batch(SalesOrder(reference="   ", lines=ORDER.lines))

    def test_a_non_numeric_sku_never_reaches_the_sql(self):
        """SKUs come from Shopify, where a draft order can set them to arbitrary text."""
        with self.assertRaises(OrderWriteError):
            batch(SalesOrder(reference="#1", lines=[OrderLine("51; DROP TABLE X", 1)]))

    def test_a_negative_price_is_refused(self):
        with self.assertRaises(OrderWriteError):
            batch(SalesOrder(reference="#1",
                             lines=[OrderLine("51", 1, Decimal("-5.00"))]))

    def test_an_absurd_price_is_refused(self):
        with self.assertRaises(OrderWriteError):
            batch(SalesOrder(reference="#1",
                             lines=[OrderLine("51", 1, Decimal("9999999.00"))]))

    def test_a_zero_quantity_is_refused(self):
        with self.assertRaises(OrderWriteError):
            batch(SalesOrder(reference="#1", lines=[OrderLine("51", 0, Decimal("1"))]))


class Totals(unittest.TestCase):
    def test_the_header_total_is_the_sum_of_the_lines(self):
        order = SalesOrder(reference="#1", lines=[
            OrderLine("51", 2, Decimal("89.00")),
            OrderLine("52", 1, Decimal("50.00")),
        ])
        self.assertIn("DECLARE @amount     money    = 228.00;", batch(order))

    def test_each_line_gets_its_own_block(self):
        order = SalesOrder(reference="#1", lines=[
            OrderLine("51", 1, Decimal("89.00")),
            OrderLine("52", 1, Decimal("50.00")),
        ])
        sql = batch(order)
        self.assertIn("SET @li_rnum = 51;", sql)
        self.assertIn("SET @li_rnum = 52;", sql)
        self.assertEqual(sql.count("-- take it off the shelf"), 2)


class FromShopify(unittest.TestCase):
    PAYLOAD = {
        "name": "#1042",
        "email": "buyer@example.com",
        "shipping_address": {"first_name": "Dana", "last_name": "Ruiz",
                             "address1": "88 Elm St", "city": "Austin",
                             "province_code": "TX", "zip": "78701",
                             "country_code": "US", "phone": "512-555-0134"},
        "line_items": [
            {"sku": "51", "title": "2008 Ford F150 Driver Side Left Mirror",
             "quantity": 1, "price": "89.00"},
            {"sku": "", "title": "Shipping", "quantity": 1, "price": "12.00"},
            {"sku": "99999", "title": "Unknown Widget", "quantity": 1, "price": "10.00"},
        ],
    }
    PARTS = {"51": Part(r_number="51", part_type="MIRROR", interchange_number="545-01883",
                        price=Decimal("89.00"))}

    def setUp(self):
        self.order, self.skipped = from_shopify(self.PAYLOAD, self.PARTS)

    def test_only_real_yard_parts_become_lines(self):
        """A shipping charge and an unmatched SKU are not parts and must not be booked."""
        self.assertEqual([l.r_number for l in self.order.lines], ["51"])
        self.assertEqual(len(self.skipped), 2)

    def test_the_shipping_address_carries_over(self):
        self.assertEqual(self.order.ship_name, "Dana Ruiz")
        self.assertEqual(self.order.ship_city, "Austin")
        self.assertEqual(self.order.ship_postal, "78701")

    def test_the_reference_is_the_shopify_order_name(self):
        """It is the duplicate guard and the human cross-reference back to the storefront."""
        self.assertEqual(self.order.reference, "#1042")

    def test_the_line_uses_the_yards_own_part_wording_not_the_storefront_title(self):
        """The description column is ~20 characters; a storefront title is a sentence."""
        self.assertEqual(self.order.lines[0].description, "MIRROR")

    def test_an_order_with_nothing_of_ours_yields_no_lines(self):
        order, skipped = from_shopify(self.PAYLOAD, {})
        self.assertEqual(order.lines, [])
        self.assertEqual(len(skipped), 3)


if __name__ == "__main__":
    unittest.main()
