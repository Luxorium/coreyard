"""Promoting a booked work order into an invoice — offline.

Like ``test_orders``, everything here runs against the placeholders in
``schema.example.json``, so the published example is executed rather than merely documented.
The batch is never sent anywhere: for a write path the SQL text *is* the behaviour, and this
one moves money, so the guards are asserted as text rather than discovered in production.
"""

import unittest

from coreyard.config import bundled
from coreyard.orders.lifecycle import SourceOrder
from coreyard.orders.promote import Promotion, decide, tracking_of
from coreyard.yms import schema
from coreyard.yms.invoices import InvoiceWriteError, build_batch

WRITE = schema.load(bundled("schema.example.json")).invoice_write


# The example's placeholder for the parts table. Every identifier in the shipped mapping is
# a placeholder — the real ones live in each installation's gitignored `schema.json`.
PARTS_TABLE = "dbo.PARTS_TABLE"


def batch(order_number=2172, tracking="1Z999AA10123456784", write=None):
    return build_batch(order_number, tracking, write or WRITE)


def indented(statement: str) -> str:
    """One mapped statement as `build_batch` lays it into the transaction body."""
    lines = statement.strip().rstrip(";").splitlines()
    return "\n".join("  " + line for line in lines) + ";"


class ExampleMapping(unittest.TestCase):
    def test_the_shipped_example_carries_a_complete_invoice_write(self):
        self.assertIsNotNone(WRITE)
        self.assertIn("INSERT", batch())

    def test_an_incomplete_block_names_what_is_missing(self):
        with self.assertRaisesRegex(schema.SchemaError, "header_insert"):
            schema.InvoiceWrite.from_dict({"defaults": {"payment_type": 1}})

    def test_a_tender_must_be_named(self):
        """An invoice under the wrong tender is only found when the books are reconciled."""
        raw = {k: "x" for k in schema.InvoiceWrite._REQUIRED}
        with self.assertRaisesRegex(schema.SchemaError, "payment_type"):
            schema.InvoiceWrite.from_dict(raw)
        self.assertIsNotNone(
            schema.InvoiceWrite.from_dict({**raw, "defaults": {"payment_type": 5}}))


class Guards(unittest.TestCase):
    def test_the_whole_promotion_is_one_abortable_transaction(self):
        sql = batch()
        self.assertIn("SET XACT_ABORT ON;", sql)
        self.assertEqual(sql.count("BEGIN TRANSACTION;"), 1)
        self.assertEqual(sql.count("COMMIT TRANSACTION;"), 1)
        self.assertGreater(sql.count("ROLLBACK TRANSACTION;"), 1)

    def test_an_already_invoiced_work_order_returns_its_existing_number(self):
        """A fulfillment can be reported twice; the second must not raise a second invoice."""
        sql = batch()
        self.assertIn("@existing", sql)
        self.assertIn("SELECT @existing AS invoice_number, 1 AS duplicate;", sql)

    def test_the_work_order_is_held_for_the_life_of_the_transaction(self):
        self.assertIn("UPDLOCK, HOLDLOCK", batch())

    def test_a_missing_work_order_aborts_rather_than_inventing_one(self):
        self.assertIn("THROW 50010", batch())

    def test_a_work_order_with_no_open_lines_is_refused(self):
        self.assertIn("THROW 50012", batch())

    def test_the_line_count_must_match_what_was_written(self):
        """A silent mismatch would invoice fewer parts than the customer bought."""
        sql = batch()
        self.assertIn("IF @@ROWCOUNT <> @li_count", sql)
        self.assertIn("THROW 50016", sql)

    def test_the_work_order_is_closed_so_it_cannot_be_promoted_twice(self):
        """Asserted through the mapping rather than through any site's column names.

        Which column holds a line's status is the site's to say, so a test that spells one
        out is testing this yard's database instead of the contract — and it puts a vendor's
        schema in the tree, which `scripts/check_neutrality.py` exists to prevent.
        """
        sql = batch()
        self.assertIn("THROW 50017", sql)
        self.assertIn(indented(WRITE.lines_close), sql)
        self.assertIn(indented(WRITE.order_close), sql)

    def test_nothing_touches_inventory(self):
        """Booking already took the part off the shelf; promoting must not take it again."""
        sql = batch()
        self.assertIn(f"LEFT JOIN {PARTS_TABLE}", sql)         # read, for the description
        for line in sql.splitlines():
            if line.strip().startswith(("UPDATE ", "INSERT ", "DELETE ")):
                self.assertNotIn(PARTS_TABLE, line)

    def test_line_ids_come_from_one_block_allocation(self):
        sql = batch()
        self.assertIn("CounterValue + @li_count", sql)
        self.assertIn("SET @li_first = @li_last - @li_count + 1;", sql)


class Refusals(unittest.TestCase):
    def test_a_non_numeric_work_order_never_reaches_the_sql(self):
        for bad in ("2172; DROP TABLE dbo.INVOICE_TABLE", "", None, "abc"):
            with self.subTest(bad=bad):
                with self.assertRaises((InvoiceWriteError, RuntimeError)):
                    batch(order_number=bad)

    def test_a_zero_work_order_number_is_refused(self):
        with self.assertRaisesRegex(InvoiceWriteError, "implausible"):
            batch(order_number=0)


class Escaping(unittest.TestCase):
    """A tracking number is carrier-supplied text reaching a db_owner connection."""

    def test_an_apostrophe_in_a_tracking_number_is_doubled(self):
        self.assertIn("N'1Z'''", batch(tracking="1Z'"))

    def test_a_quoted_injection_cannot_close_the_literal(self):
        hostile = "'); DROP TABLE X; --"
        sql = batch(tracking=hostile)
        # The payload survives as data, doubled, never as a statement boundary.
        self.assertIn("N'''); DROP TABLE X; --'", sql)
        self.assertNotIn("; DROP TABLE X; --'",
                         sql.replace("N'''); DROP TABLE X; --'", ""))

    def test_an_overlong_tracking_number_is_trimmed_to_the_column(self):
        self.assertIn("N'" + "9" * 50 + "'", batch(tracking="9" * 200))


class Deciding(unittest.TestCase):
    """Which booked orders have earned an invoice. Pure; no network, no database."""

    BOOKED = SourceOrder(order_reference="#1017", external_reference="2172", status="booked")

    def order(self, **overrides):
        base = {"name": "#1017", "sourceName": "web",
                "displayFinancialStatus": "PAID",
                "displayFulfillmentStatus": "FULFILLED",
                "fulfillments": [{"status": "SUCCESS",
                                  "trackingInfo": [{"number": "1Z999"}]}]}
        return {**base, **overrides}

    def test_a_paid_shipped_order_is_promoted_with_its_tracking(self):
        got = decide(self.BOOKED, self.order())
        self.assertTrue(got.wanted)
        self.assertEqual(got.work_order, "2172")
        self.assertEqual(got.tracking, "1Z999")

    def test_an_unfulfilled_order_waits(self):
        got = decide(self.BOOKED, self.order(displayFulfillmentStatus="UNFULFILLED"))
        self.assertFalse(got.wanted)
        self.assertIn("unfulfilled", got.skip)

    def test_a_partly_shipped_order_waits_for_the_rest(self):
        """One invoice for the whole work order, once it has all gone."""
        got = decide(self.BOOKED, self.order(displayFulfillmentStatus="PARTIALLY_FULFILLED"))
        self.assertFalse(got.wanted)
        self.assertIn("partially fulfilled", got.skip)

    def test_a_refunded_order_is_left_for_a_person(self):
        """A refund is settled by a credit invoice somebody decides on, not by this."""
        got = decide(self.BOOKED, self.order(displayFinancialStatus="REFUNDED"))
        self.assertFalse(got.wanted)
        self.assertIn("REFUNDED", got.skip)

    def test_an_in_store_sale_is_never_promoted(self):
        """Shopify fulfills a counter sale at the till, before anybody pulls the part."""
        for source in ("quick_sale", "pos"):
            with self.subTest(source=source):
                got = decide(self.BOOKED, self.order(sourceName=source))
                self.assertFalse(got.wanted)
                self.assertIn("in-store", got.skip)

    def test_an_already_invoiced_order_is_not_promoted_again(self):
        invoiced = SourceOrder(order_reference="#1013", external_reference="2138",
                               status="invoiced")
        self.assertFalse(decide(invoiced, self.order()).wanted)

    def test_an_order_missing_from_the_storefront_is_reported_not_guessed(self):
        got = decide(self.BOOKED, None)
        self.assertFalse(got.wanted)
        self.assertIn("no such order", got.skip)

    def test_a_shipped_order_with_no_tracking_still_invoices(self):
        """A freight shipment booked by phone has no tracking number; it still shipped."""
        got = decide(self.BOOKED, self.order(fulfillments=[]))
        self.assertTrue(got.wanted)
        self.assertEqual(got.tracking, "")


class Tracking(unittest.TestCase):
    def test_a_cancelled_fulfillment_is_not_a_shipment(self):
        order = {"fulfillments": [
            {"status": "CANCELLED", "trackingInfo": [{"number": "OLD"}]},
            {"status": "SUCCESS", "trackingInfo": [{"number": "NEW"}]}]}
        self.assertEqual(tracking_of(order), "NEW")

    def test_an_empty_tracking_number_is_not_returned(self):
        order = {"fulfillments": [{"status": "SUCCESS",
                                   "trackingInfo": [{"number": "  "}]}]}
        self.assertEqual(tracking_of(order), "")


if __name__ == "__main__":
    unittest.main()
