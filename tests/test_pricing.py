import os
import unittest
from decimal import Decimal

from coreyard.transform.pricing import charm, retail, retail_str


class Charm(unittest.TestCase):
    def test_whole_dollar_drops_a_penny(self):
        self.assertEqual(charm(Decimal("50.00")), Decimal("49.99"))
        self.assertEqual(charm(Decimal("8.00")), Decimal("7.99"))
        self.assertEqual(charm(Decimal("1500.00")), Decimal("1499.99"))

    def test_rounds_to_nearest_and_ties_go_up(self):
        self.assertEqual(charm(Decimal("50.40")), Decimal("49.99"))
        self.assertEqual(charm(Decimal("50.10")), Decimal("49.99"))
        # .49 is an exact tie (50c either way), and ties round up.
        self.assertEqual(charm(Decimal("50.49")), Decimal("50.99"))
        self.assertEqual(charm(Decimal("50.50")), Decimal("50.99"))
        self.assertEqual(charm(Decimal("67.50")), Decimal("67.99"))
        self.assertEqual(charm(Decimal("63.75")), Decimal("63.99"))

    def test_already_charm_is_unchanged(self):
        self.assertEqual(charm(Decimal("49.99")), Decimal("49.99"))
        self.assertEqual(charm(Decimal("129.99")), Decimal("129.99"))

    def test_never_below_the_floor(self):
        self.assertEqual(charm(Decimal("0.99")), Decimal("0.99"))
        self.assertEqual(charm(Decimal("0.25")), Decimal("0.99"))
        self.assertEqual(charm(Decimal("1.00")), Decimal("0.99"))


class Gate(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("STORE_CHARM_PRICES")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("STORE_CHARM_PRICES", None)
        else:
            os.environ["STORE_CHARM_PRICES"] = self._saved

    def test_off_by_default_leaves_price_alone(self):
        os.environ.pop("STORE_CHARM_PRICES", None)
        self.assertEqual(retail(Decimal("50.00")), Decimal("50.00"))
        self.assertEqual(retail_str(Decimal("50.00")), "50.00")

    def test_on_applies_rounding(self):
        os.environ["STORE_CHARM_PRICES"] = "true"
        self.assertEqual(retail(Decimal("50.00")), Decimal("49.99"))
        self.assertEqual(retail_str(Decimal("50.00")), "49.99")

    def test_unpriced_stays_unpriced(self):
        os.environ["STORE_CHARM_PRICES"] = "true"
        self.assertIsNone(retail(None))
        self.assertEqual(retail_str(None), "")
        self.assertEqual(retail_str(None, default="0.00"), "0.00")


if __name__ == "__main__":
    unittest.main()
