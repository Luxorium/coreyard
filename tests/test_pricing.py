import unittest
from decimal import Decimal

from coreyard.transform.pricing import money_str, parse_money


class ExternalMoney(unittest.TestCase):
    def test_formatted_marketplace_money_parses_without_float_rounding(self):
        self.assertEqual(parse_money("$1,299.50"), Decimal("1299.50"))
        self.assertEqual(parse_money(57.5), Decimal("57.5"))

    def test_unreadable_money_is_missing(self):
        self.assertIsNone(parse_money("not priced"))
        self.assertIsNone(parse_money(None))

    def test_round_trip_format_always_has_cents(self):
        self.assertEqual(money_str(45), "45.00")

    def test_source_amount_is_not_adjusted(self):
        self.assertEqual(money_str(Decimal("50.00")), "50.00")
        self.assertEqual(money_str(Decimal("67.50")), "67.50")


if __name__ == "__main__":
    unittest.main()
