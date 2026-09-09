"""What a publish loop has banked when something stops it early.

A scheduled run is wrapped in ``--timeout``, which stops it by raising ``SystemExit`` from a
SIGALRM handler. Every part already published is real work that reached Shopify, so the
snapshot has to keep it; anything not banked is republished on the next tick.

That is not a theoretical tidiness point. The count-based checkpoint alone only fires once
CHECKPOINT_EVERY parts are done, so a run whose window is too short to reach the threshold
banked *nothing*, rebuilt the identical batch next tick and discarded it again. The backlog
only grows, so it never became small enough to finish: the five-minute delta livelocked on
2026-09-08 and again on 2026-09-09, republishing ~50 parts every five minutes for an hour
until the hourly full sync cleared it by accident.
"""

import unittest

from coreyard import run_sync
from coreyard.run_sync import CHECKPOINT_EVERY, _publish_batch


class FakePart:
    def __init__(self, key):
        self.key = key

    def uid(self):
        return self.key


class FakePublisher:
    """Publishes happily until it has done ``stop_after``, then behaves like the timeout."""

    def __init__(self, stop_after=None, fail=()):
        self.stop_after = stop_after
        self.fail = set(fail)
        self.published = []

    def publish(self, part, refresh_images=False, revive_status=None):
        if self.stop_after is not None and len(self.published) >= self.stop_after:
            raise SystemExit(124)
        if part.uid() in self.fail:
            raise RuntimeError("boom")
        self.published.append(part.uid())


def run(todo, publisher, revivals=None):
    """Drive the loop, collecting what it banked and when."""
    banked = []

    def bank(pending):
        banked.append(set(pending))

    published_ok = revived = None
    try:
        published_ok, revived = _publish_batch(
            publisher, todo, set(), revivals or {}, bank
        )
    except SystemExit:
        pass
    return banked, published_ok, revived


def parts(n):
    return [FakePart(str(i)) for i in range(1, n + 1)]


class InterruptedRuns(unittest.TestCase):
    def test_a_short_window_still_banks_what_it_published(self):
        """The regression. Fewer parts than CHECKPOINT_EVERY, stopped by the timeout."""
        publisher = FakePublisher(stop_after=50)
        banked, _, _ = run(parts(80), publisher)
        self.assertEqual(set().union(*banked), set(publisher.published),
                         "work that reached Shopify was not banked")
        self.assertEqual(len(publisher.published), 50)

    def test_the_next_run_therefore_has_less_to_do(self):
        """Two consecutive short windows must make progress, not repeat themselves."""
        todo = parts(80)
        first = FakePublisher(stop_after=50)
        banked, _, _ = run(todo, first)
        done = set().union(*banked)

        remaining = [p for p in todo if p.uid() not in done]
        second = FakePublisher(stop_after=50)
        run(remaining, second)

        self.assertEqual(len(remaining), 30, "the first window banked nothing")
        self.assertEqual(done | set(second.published), {p.uid() for p in todo},
                         "two windows did not finish an 80-part batch")

    def test_nothing_published_banks_nothing(self):
        publisher = FakePublisher(stop_after=0)
        banked, _, _ = run(parts(10), publisher)
        self.assertEqual(banked, [], "an empty bank was written")

    def test_a_part_is_banked_only_once(self):
        """The checkpoint and the final flush must not both claim the same part."""
        publisher = FakePublisher()
        banked, published_ok, _ = run(parts(CHECKPOINT_EVERY + 10), publisher)
        flat = [key for batch in banked for key in batch]
        self.assertEqual(len(flat), len(set(flat)), "a part was banked twice")
        self.assertEqual(set(flat), published_ok)

    def test_the_checkpoint_still_fires_mid_run(self):
        """A long run must not wait until the end to bank anything."""
        publisher = FakePublisher(stop_after=CHECKPOINT_EVERY + 5)
        banked, _, _ = run(parts(CHECKPOINT_EVERY * 3), publisher)
        self.assertGreaterEqual(len(banked), 2, "the interval checkpoint never fired")
        self.assertEqual(len(banked[0]), CHECKPOINT_EVERY)


class FailedPublishes(unittest.TestCase):
    def test_a_part_that_raised_is_never_banked(self):
        """Banking a failed publish would mark it up to date and lose the change."""
        publisher = FakePublisher(fail={"3"})
        banked, published_ok, _ = run(parts(5), publisher)
        self.assertNotIn("3", set().union(*banked))
        self.assertNotIn("3", published_ok)

    def test_the_rest_of_the_batch_still_lands(self):
        publisher = FakePublisher(fail={"3"})
        _, published_ok, _ = run(parts(5), publisher)
        self.assertEqual(published_ok, {"1", "2", "4", "5"})


class Revivals(unittest.TestCase):
    def test_only_revived_parts_are_reported_as_revived(self):
        publisher = FakePublisher()
        _, published_ok, revived = run(parts(4), publisher, revivals={"2": "ACTIVE"})
        self.assertEqual(revived, {"2"})
        self.assertEqual(published_ok, {"1", "2", "3", "4"})

    def test_a_revival_that_failed_is_not_reported_as_revived(self):
        """clear_retired() must not forget a part that is still archived in the store."""
        publisher = FakePublisher(fail={"2"})
        _, _, revived = run(parts(4), publisher, revivals={"2": "ACTIVE"})
        self.assertEqual(revived, set())


class BothSchedulersUseIt(unittest.TestCase):
    """The delta and the full sync had this loop written out twice, and only one was fixed.

    Keeping them on one implementation is the point: the delta is the path with the short
    timeout, but the full sync loses up to CHECKPOINT_EVERY parts of work the same way.
    """

    def test_the_source_has_exactly_one_publish_loop(self):
        source = run_sync.__file__
        with open(source, encoding="utf-8") as handle:
            body = handle.read()
        self.assertEqual(body.count("publisher.publish(part"), 1,
                         "a second publish loop has appeared; it needs the same banking")
        self.assertEqual(body.count("= _publish_batch("), 2,
                         "the delta and full paths should both call _publish_batch")


if __name__ == "__main__":
    unittest.main()
