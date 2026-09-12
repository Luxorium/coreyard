"""Guards around the one destructive thing CoreYard does: taking a part off sale.

A part vanishing from the extract is *usually* a sale, but it is also what a half-finished
run, a broken join, or a database that answered with nothing all look like. These pin the
rules that stop an unattended timer from wiping a live catalogue.
"""

import contextlib
import io
import unittest
from argparse import Namespace

from coreyard.run_sync import _delta_retirement_plan, _retire_batch, _retirement_plan
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
        self.assertIn("no listable parts at all", why)

    def test_force_retire_does_not_override_an_empty_extract(self):
        """Forcing says "that 15% really did sell". It cannot say anything about a run that
        saw nothing: impacket returns a refused statement as an empty result, so this is
        what a broken read looks like as well as an impossible yard."""
        plan, why = _retirement_plan(diff(removed=[str(i) for i in range(500)]),
                                     args(force_retire=True))
        self.assertEqual(plan, [])
        self.assertIn("--force-retire does not lift this", why)

    def test_one_surviving_part_is_enough_to_reason_from(self):
        """The guard is about an extract with nothing in it, not about a small yard."""
        plan, why = _retirement_plan(diff(removed=["1", "2"], unchanged=["3"]),
                                     args(force_retire=True))
        self.assertEqual(plan, ["1", "2"])
        self.assertEqual(why, "")

    def test_csv_sink_never_retires(self):
        plan, why = _retirement_plan(diff(removed=["1"], unchanged=["2"]), args(sink="csv"))
        self.assertEqual(plan, [])


class Changes:
    """What the delta reader hands back: rows that moved, and rows that left scope."""

    def __init__(self, left_scope=(), truncated=False):
        self.left_scope = list(left_scope)
        self.truncated = truncated


class DeltaRetirement(unittest.TestCase):
    """A catch-up run retires on evidence — a row that says it is no longer listable —
    rather than on absence, so its guards are not the full run's guards."""

    def plan(self, changes, published=None, **kw):
        published = {str(i): "fp" for i in range(100)} if published is None else published
        return _delta_retirement_plan(changes, published, args(**kw))

    def test_a_part_that_left_scope_is_retired(self):
        self.assertEqual(self.plan(Changes(left_scope=["1", "2"])), (["1", "2"], ""))

    def test_a_row_this_installation_never_published_is_not_a_retirement(self):
        plan, why = self.plan(Changes(left_scope=["1", "nope"]))
        self.assertEqual(plan, ["1"])
        self.assertEqual(why, "")

    def test_a_truncated_read_retires_nothing(self):
        """It is no longer a delta, which is also why its cursor is held."""
        self.assertEqual(self.plan(Changes(left_scope=["1", "2"], truncated=True)), ([], ""))

    def test_a_mass_exit_is_refused(self):
        plan, why = self.plan(Changes(left_scope=[str(i) for i in range(50)]))
        self.assertEqual(plan, [])
        self.assertIn("REFUSING", why)

    def test_force_retire_allows_it(self):
        plan, why = self.plan(Changes(left_scope=[str(i) for i in range(50)]),
                              force_retire=True)
        self.assertEqual(len(plan), 50)
        self.assertEqual(why, "")

    def test_nothing_published_yet_retires_nothing(self):
        self.assertEqual(self.plan(Changes(left_scope=["1"]), published={}), ([], ""))


class FakePublisher:
    def __init__(self, fail=(), drafts=()):
        self.fail, self.drafts = set(fail), set(drafts)
        self.retired: list[str] = []
        self.recorded: list[str] = []

    def retire(self, r_number, record_prior=None, skip_draft=False):
        if r_number in self.fail:
            raise RuntimeError("productUpdate: throttled")
        self.retired.append(r_number)
        if record_prior is not None:
            record_prior(r_number, "ACTIVE")
        return "draft" if r_number in self.drafts else "retired"


class RetireBatch(unittest.TestCase):
    """What the snapshot is allowed to forget afterwards."""

    def run_batch(self, publisher, retire):
        prior: list[tuple[str, str]] = []
        with contextlib.redirect_stdout(io.StringIO()):
            done = _retire_batch(publisher, retire,
                                 lambda r, status: prior.append((r, status)))
        return done, prior

    def test_what_came_off_sale_is_what_is_returned(self):
        done, _ = self.run_batch(FakePublisher(), ["1", "2"])
        self.assertEqual(done, {"1", "2"})

    def test_a_part_that_could_not_be_retired_stays_pending(self):
        """Forgetting it would mean nothing ever proposes retiring it again, and the
        shopper keeps seeing a part the yard no longer has."""
        publisher = FakePublisher(fail={"2"})
        done, _ = self.run_batch(publisher, ["1", "2", "3"])
        self.assertEqual(done, {"1", "3"})

    def test_one_refusal_does_not_strand_the_rest_of_the_batch(self):
        publisher = FakePublisher(fail={"1"})
        self.run_batch(publisher, ["1", "2", "3"])
        self.assertEqual(publisher.retired, ["2", "3"])

    def test_the_status_being_replaced_is_remembered_for_a_revival(self):
        _, prior = self.run_batch(FakePublisher(), ["1"])
        self.assertEqual(prior, [("1", "ACTIVE")])

    def test_an_already_invisible_draft_counts_as_retired(self):
        """Archiving it would destroy the difference between "not ready" and "gone", and
        keeping it in the snapshot would re-propose the same no-op every run."""
        done, _ = self.run_batch(FakePublisher(drafts={"1"}), ["1"])
        self.assertEqual(done, {"1"})


if __name__ == "__main__":
    unittest.main()
