"""Guards around the one destructive thing CoreYard does: taking a part off sale.

A part vanishing from the extract is *usually* a sale, but it is also what a half-finished
run, a broken join, or a database that answered with nothing all look like. These pin the
rules that stop an unattended timer from wiping a live catalogue.
"""

import unittest
from argparse import Namespace

from coreyard.run_sync import _retirement_plan
from coreyard.state import DiffResult


def args(**kw) -> Namespace:
    base = dict(sink="api", limit=None, force_retire=False, max_retire_fraction=0.10,
                reconcile=False, retire_only=False)
    base.update(kw)
    return Namespace(**base)


def diff(removed=(), unchanged=(), added=(), changed=()) -> DiffResult:
    return DiffResult(added=list(added), changed=list(changed),
                      unchanged=list(unchanged), removed=list(removed))


class RetirementPlan(unittest.TestCase):
    def test_ordinary_sales_are_retired(self):
        plan, why = _retirement_plan(diff(removed=["1"], unchanged=[str(i) for i in range(100)]),
                                     args())
        self.assertEqual(plan, ["1"])
        self.assertEqual(why, "")

    def test_nothing_removed_is_a_no_op(self):
        self.assertEqual(_retirement_plan(diff(unchanged=["1"]), args()), ([], ""))

    def test_limit_blocks_retirement(self):
        """--limit means the run saw a slice of the yard, so 'missing' means nothing."""
        plan, why = _retirement_plan(diff(removed=["1", "2"], unchanged=["3"]),
                                     args(limit=25))
        self.assertEqual(plan, [])
        self.assertIn("--limit", why)

    def test_mass_removal_is_refused(self):
        """A broken extract looks exactly like the whole yard selling out at once."""
        plan, why = _retirement_plan(diff(removed=[str(i) for i in range(90)],
                                          unchanged=["a", "b"]), args())
        self.assertEqual(plan, [])
        self.assertIn("REFUSING", why)

    def test_force_retire_overrides_the_fraction_guard(self):
        plan, why = _retirement_plan(diff(removed=[str(i) for i in range(90)],
                                          unchanged=["a", "b"]), args(force_retire=True))
        self.assertEqual(len(plan), 90)
        self.assertEqual(why, "")

    def test_force_retire_does_not_override_limit(self):
        """--limit is about not having the facts; forcing cannot supply them."""
        plan, _ = _retirement_plan(diff(removed=["1"], unchanged=["2"]),
                                   args(limit=10, force_retire=True))
        self.assertEqual(plan, [])

    def test_empty_extract_is_refused(self):
        """The database answered but returned nothing — never read that as 'all sold'."""
        plan, why = _retirement_plan(diff(removed=[str(i) for i in range(500)]), args())
        self.assertEqual(plan, [])
        self.assertIn("REFUSING", why)

    def test_csv_sink_never_retires(self):
        plan, why = _retirement_plan(diff(removed=["1"], unchanged=["2"]), args(sink="csv"))
        self.assertEqual(plan, [])


if __name__ == "__main__":
    unittest.main()
