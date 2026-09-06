"""The interchange application cache: what it stores, and what makes it let go."""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from coreyard.yms import fitment_cache
from coreyard.yms.fitment_cache import FitmentCache


class Schema:
    def __init__(self, apps="SELECT app FROM t WHERE x={part_type_code}", makes="SELECT m"):
        self.interchange_applications = apps
        self.interchange_makes = makes


class Storing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "c.sqlite3"

    def cache(self, fingerprint="fp", max_age=None):
        c = FitmentCache(self.path, fingerprint, max_age=max_age)
        self.addCleanup(c.close)
        return c

    def test_a_group_survives_the_process_that_fetched_it(self):
        first = self.cache()
        first.put(166, "01234", ["ACADIA 08", "ENCLAVE 09-11"])
        first.close()
        self.assertEqual(self.cache().get(166, "01234"), ["ACADIA 08", "ENCLAVE 09-11"])

    def test_a_part_that_fits_nothing_is_a_real_answer_and_is_kept(self):
        # Otherwise every run re-asks the same question and gets the same empty answer.
        c = self.cache()
        c.put(166, "99999", [])
        self.assertEqual(c.get(166, "99999"), [])

    def test_an_unknown_group_is_a_miss_not_an_empty_answer(self):
        self.assertIsNone(self.cache().get(166, "nothing-here"))

    def test_hits_and_misses_are_counted(self):
        c = self.cache()
        c.put(1, "a", ["X"])
        c.get(1, "a")
        c.get(1, "b")
        self.assertEqual((c.hits, c.misses), (1, 1))
        self.assertIn("1 hit", c.summary())


class LettingGo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "c.sqlite3"

    def test_a_changed_query_voids_every_stored_row(self):
        # The rows answered a different question; serving them would publish fitment the
        # site's own SQL no longer asks for.
        first = FitmentCache(self.path, "fingerprint-one")
        first.put(166, "01234", ["ACADIA 08"])
        first.close()
        second = FitmentCache(self.path, "fingerprint-two")
        self.addCleanup(second.close)
        self.assertIsNone(second.get(166, "01234"))

    def test_an_unchanged_query_keeps_them(self):
        first = FitmentCache(self.path, "same")
        first.put(166, "01234", ["ACADIA 08"])
        first.close()
        second = FitmentCache(self.path, "same")
        self.addCleanup(second.close)
        self.assertEqual(second.get(166, "01234"), ["ACADIA 08"])

    def test_an_entry_older_than_the_ceiling_is_refetched(self):
        c = FitmentCache(self.path, "fp", max_age=100)
        self.addCleanup(c.close)
        c.put(166, "01234", ["ACADIA 08"])
        c.conn.execute("UPDATE applications SET fetched_at=?", (time.time() - 500,))
        self.assertIsNone(c.get(166, "01234"))

    def test_a_zero_ceiling_means_no_expiry(self):
        c = FitmentCache(self.path, "fp", max_age=0)
        self.addCleanup(c.close)
        c.put(166, "01234", ["ACADIA 08"])
        c.conn.execute("UPDATE applications SET fetched_at=?", (0.0,))
        self.assertEqual(c.get(166, "01234"), ["ACADIA 08"])

    def test_a_corrupt_row_is_a_miss_rather_than_a_crash(self):
        c = FitmentCache(self.path, "fp")
        self.addCleanup(c.close)
        c.put(166, "01234", ["ACADIA 08"])
        c.conn.execute("UPDATE applications SET apps='not json'")
        self.assertIsNone(c.get(166, "01234"))


class Fingerprint(unittest.TestCase):
    def test_whitespace_is_not_a_change(self):
        self.assertEqual(fitment_cache.query_fingerprint("SELECT  a\n FROM t"),
                         fitment_cache.query_fingerprint("SELECT a FROM t"))

    def test_a_different_statement_is(self):
        self.assertNotEqual(fitment_cache.query_fingerprint("SELECT a FROM t"),
                            fitment_cache.query_fingerprint("SELECT b FROM t"))

    def test_the_makes_query_counts_too(self):
        self.assertNotEqual(fitment_cache.query_fingerprint("A", "one"),
                            fitment_cache.query_fingerprint("A", "two"))


class SwitchedOff(unittest.TestCase):
    def test_a_site_with_no_application_query_gets_no_cache(self):
        self.assertIsNone(fitment_cache.open_for(Schema(apps="")))


class ThroughTheResolver(unittest.TestCase):
    """The resolver must ask the database only for what the cache does not hold."""

    class FakeCache:
        def __init__(self, seed=None):
            self.rows = dict(seed or {})
            self.writes = []
            self.hits = self.misses = 0

        def get(self, code, key):
            value = self.rows.get((int(code), str(key)))
            if value is None:
                self.misses += 1
            else:
                self.hits += 1
            return value

        def put(self, code, key, apps):
            self.writes.append((int(code), str(key), list(apps)))
            self.rows[(int(code), str(key))] = list(apps)

        def commit(self):
            pass

        def summary(self):
            return "fake"

    def _resolver(self, cache, rows):
        from unittest.mock import patch

        from coreyard.yms.interchange import InterchangeResolver

        schema = Schema(apps="SELECT app FROM t WHERE p={part_type_code} "
                             "AND i='{interchange_code}'", makes="")
        patcher = patch("coreyard.yms.interchange.query", side_effect=rows)
        q = patcher.start()
        self.addCleanup(patcher.stop)
        return InterchangeResolver(object(), source=schema, cache=cache), q

    def test_a_cached_group_is_never_queried(self):
        cache = self.FakeCache({(166, "01234"): ["ACADIA 08"]})
        resolver, q = self._resolver(cache, [])
        self.assertEqual(resolver._applications(166, "01234"), ["ACADIA 08"])
        q.assert_not_called()

    def test_a_missing_group_is_queried_and_then_stored(self):
        cache = self.FakeCache()
        resolver, q = self._resolver(cache, [[{"app": "ENCLAVE 09-11"}]])
        self.assertEqual(resolver._applications(166, "01234"), ["ENCLAVE 09-11"])
        self.assertEqual(cache.writes, [(166, "01234", ["ENCLAVE 09-11"])])

    def test_a_failed_query_is_not_recorded_as_fitting_nothing(self):
        cache = self.FakeCache()
        resolver, _ = self._resolver(cache, RuntimeError("pipe closed"))
        with self.assertRaises(RuntimeError):
            resolver._applications(166, "01234")
        self.assertEqual(cache.writes, [])


if __name__ == "__main__":
    unittest.main()


class NeverFatal(unittest.TestCase):
    """A cache is an optimisation. Losing it must never lose the run.

    A two-hour repair died at 1,000 of 26,660 parts with "database is locked", because a
    scheduled ``sync delta`` wanted the same file. Every job on the host resolves fitment,
    so contention is the normal case, not the exception.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cache = FitmentCache(Path(self.dir.name) / "c.sqlite3", "fp")
        self.addCleanup(self.cache.close)

    class Locked:
        """A connection that behaves the way a contended sqlite file does."""

        def __init__(self, real):
            self.real = real

        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            self.real.close()

    def _break_writes(self):
        self.cache.conn = self.Locked(self.cache.conn)

    def test_a_locked_write_is_dropped_not_raised(self):
        self._break_writes()
        self.cache.put(166, "01234", ["ACADIA 08"])      # must not raise
        self.assertEqual(self.cache.writes_lost, 1)

    def test_a_locked_read_answers_nothing_and_does_not_raise(self):
        self._break_writes()
        self.assertIsNone(self.cache.get(166, "01234"))
        self.assertTrue(self.cache.degraded)

    def test_once_degraded_it_stops_consulting_the_file(self):
        self._break_writes()
        self.cache.get(166, "01234")
        class Forbidden:
            def execute(_self, *a, **k):
                raise AssertionError("degraded cache must not touch the file")

            def commit(_self):
                pass          # closing is allowed to try; reading and writing are not

            def close(_self):
                pass

        self.cache.conn = Forbidden()
        self.assertIsNone(self.cache.get(166, "99999"))
        self.cache.put(166, "99999", [])

    def test_the_summary_says_so_rather_than_looking_healthy(self):
        self._break_writes()
        self.cache.get(1, "a")
        self.assertIn("gave up", self.cache.summary())

    def test_lost_writes_are_reported(self):
        self._break_writes()
        self.cache.put(1, "a", [])
        self.assertIn("not stored", self.cache.summary())

    def test_a_write_is_visible_to_another_connection_immediately(self):
        # Batching writes into one transaction is what held the single WAL write lock
        # across hundreds of network round trips.
        self.cache.put(166, "01234", ["ACADIA 08"])
        other = FitmentCache(self.cache.path, "fp")
        self.addCleanup(other.close)
        self.assertEqual(other.get(166, "01234"), ["ACADIA 08"])

    def test_a_cache_file_that_cannot_be_opened_yields_no_cache(self):
        blocked = Path(self.dir.name) / "nope" / "deeper" / "c.sqlite3"
        self.assertIsNone(fitment_cache.open_for(Schema(), path=blocked))
