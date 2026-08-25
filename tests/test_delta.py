"""Delta (incremental catch-up) query building, row classification and state handling."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from coreyard.models import Part
from coreyard.state import SyncState
from coreyard.yms import delta
from coreyard.yms.schema import DELTA_SCOPE_FIELD, SchemaError, SourceSchema, sql_timestamp

MAPPING = SourceSchema(
    select={"r_number": "i.PartId", "part_type": "pt.Name", "price": "i.Price"},
    source="dbo.Parts i LEFT JOIN dbo.Types pt ON i.TypeId = pt.TypeId",
    scope="i.Price > 0 AND i.Qty > 0",
    order_by="i.PartId",
    modified_at="i.ChangedOn",
    image_changes="SELECT PartId AS r_number FROM dbo.Photos WHERE ChangedOn > {since}",
)

NO_DELTA = SourceSchema(
    select={"r_number": "i.PartId", "part_type": "pt.Name", "price": "i.Price"},
    source="dbo.Parts i",
    scope="i.Price > 0",
    order_by="i.PartId",
)

WHEN = datetime(2026, 8, 24, 14, 5, 0)


class Timestamps(unittest.TestCase):
    def test_renders_an_unambiguous_literal(self):
        self.assertEqual(sql_timestamp(WHEN), "'2026-08-24T14:05:00'")

    def test_accepts_an_iso_string_round_trip(self):
        self.assertEqual(sql_timestamp(WHEN.isoformat()), sql_timestamp(WHEN))

    def test_rejects_anything_that_is_not_a_timestamp(self):
        """The cursor is interpolated into SQL, so it is re-derived, never passed through."""
        for hostile in ["'; DROP TABLE Parts--", "2026-13-45", "", "yesterday", None, 17]:
            with self.assertRaises(SchemaError, msg=repr(hostile)):
                sql_timestamp(hostile)


class DeltaQuery(unittest.TestCase):
    def test_filters_on_the_cursor_and_carries_the_scope_verdict(self):
        sql = MAPPING.build_delta_query(WHEN, 250)
        self.assertIn("WHERE i.ChangedOn > '2026-08-24T14:05:00'", sql)
        self.assertIn(f"THEN 1 ELSE 0 END AS {DELTA_SCOPE_FIELD}", sql)

    def test_does_not_filter_by_scope(self):
        """The whole point: a sold part must still come back, flagged out-of-scope."""
        sql = MAPPING.build_delta_query(WHEN, 250)
        self.assertNotIn("WHERE i.Price > 0 AND i.Qty > 0", sql)
        self.assertIn("SELECT TOP 250", sql)

    def test_pages_on_the_identity_cursor(self):
        sql = MAPPING.build_delta_query(WHEN, 10, after="A'2000")
        self.assertIn("AND i.PartId > N'A''2000'", sql)
        self.assertIn("ORDER BY i.PartId", sql)

    def test_rejects_bad_bounds_and_missing_support(self):
        with self.assertRaises(ValueError):
            MAPPING.build_delta_query(WHEN, 0)
        with self.assertRaises(SchemaError):
            NO_DELTA.build_delta_query(WHEN, 10)

    def test_supports_delta_reflects_the_mapping(self):
        self.assertTrue(MAPPING.supports_delta)
        self.assertFalse(NO_DELTA.supports_delta)

    def test_image_changes_substitutes_the_cursor(self):
        sql = MAPPING.build_image_changes_query(WHEN)
        self.assertIn("ChangedOn > '2026-08-24T14:05:00'", sql)
        with self.assertRaises(SchemaError):
            NO_DELTA.build_image_changes_query(WHEN)

    def test_lookup_can_carry_the_scope_verdict(self):
        plain = MAPPING.build_lookup_query(["51"])
        scoped = MAPPING.build_lookup_query(["51"], with_scope=True)
        self.assertNotIn(DELTA_SCOPE_FIELD, plain)
        self.assertIn(f"THEN 1 ELSE 0 END AS {DELTA_SCOPE_FIELD}", scoped)

    def test_mapping_may_not_shadow_the_computed_column(self):
        with self.assertRaises(SchemaError):
            SourceSchema.from_dict({
                "select": {"r_number": "a", "part_type": "b", "price": "c",
                           DELTA_SCOPE_FIELD: "d"},
                "source": "t", "scope": "1=1",
            })


class ScopeVerdict(unittest.TestCase):
    def test_coerces_every_shape_the_transport_returns(self):
        for truthy in (1, True, "1", "true", "True", "yes"):
            self.assertTrue(delta._truthy(truthy), msg=repr(truthy))
        for falsy in (0, False, "0", "false", "False", "", None, "NULL"):
            self.assertFalse(delta._truthy(falsy), msg=repr(falsy))

    def test_the_string_False_is_not_treated_as_true(self):
        """Plain truthiness would publish every sold part; this is the trap being guarded."""
        self.assertFalse(delta._truthy("False"))


class SubsetState(unittest.TestCase):
    """A delta run holds a slice of the yard; the snapshot must survive that."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = SyncState(Path(self.dir.name) / "state.sqlite3")
        self.state.commit({"1": "fp1", "2": "fp2", "3": "fp3"})

    def tearDown(self):
        self.state.close()
        self.dir.cleanup()

    def test_subset_diff_reports_no_removals(self):
        diff = self.state.diff({"2": "fp2-new"}, detect_removals=False)
        self.assertEqual(diff.changed, ["2"])
        self.assertEqual(diff.removed, [])

    def test_full_diff_still_reports_removals(self):
        self.assertEqual(self.state.diff({"2": "fp2"}).removed, ["1", "3"])

    def test_update_merges_without_erasing_the_rest(self):
        self.state.update({"2": "fp2-new", "9": "fp9"})
        self.assertEqual(
            self.state.load(),
            {"1": "fp1", "2": "fp2-new", "3": "fp3", "9": "fp9"},
        )

    def test_commit_would_have_erased_the_rest(self):
        """Contrast: why the delta path must never call commit with a subset."""
        self.state.commit({"2": "fp2"})
        self.assertEqual(sorted(self.state.load()), ["2"])

    def test_forget_drops_only_the_named_parts(self):
        self.state.forget(["1", "missing"])
        self.assertEqual(sorted(self.state.load()), ["2", "3"])

    def test_cursor_round_trips_and_starts_empty(self):
        self.assertIsNone(self.state.get_cursor(delta.CURSOR_NAME))
        self.state.set_cursor(delta.CURSOR_NAME, WHEN.isoformat())
        self.assertEqual(self.state.get_cursor(delta.CURSOR_NAME), WHEN.isoformat())
        self.state.set_cursor(delta.CURSOR_NAME, "2026-08-25T00:00:00")
        self.assertEqual(self.state.get_cursor(delta.CURSOR_NAME), "2026-08-25T00:00:00")


class Overlap(unittest.TestCase):
    def test_cursor_is_rewound_so_in_flight_writes_are_not_skipped(self):
        self.assertGreater(delta.DEFAULT_OVERLAP, timedelta(0))


class TransientRetry(unittest.TestCase):
    """The named pipe is occasionally busy; that must not look like a broken sync."""

    def test_busy_pipe_is_transient(self):
        for status in ("STATUS_PIPE_NOT_AVAILABLE", "STATUS_PIPE_BUSY",
                       "STATUS_INSUFF_SERVER_RESOURCES", "STATUS_CONNECTION_DISCONNECTED"):
            exc = Exception(f"SMB SessionError: {status}(some detail)")
            self.assertTrue(delta._is_transient(exc), msg=status)

    def test_real_failures_are_not_retried(self):
        """Retrying bad credentials just relearns they are bad, three times, every run."""
        for message in ("SMB SessionError: STATUS_LOGON_FAILURE",
                        "SMB SessionError: STATUS_ACCESS_DENIED",
                        "invalid column name 'Nope'", ""):
            self.assertFalse(delta._is_transient(Exception(message)), msg=message)

    def test_retries_then_succeeds(self):
        calls = []

        def flaky(since, overlap):
            calls.append(since)
            if len(calls) < 3:
                raise Exception("SMB SessionError: STATUS_PIPE_NOT_AVAILABLE(busy)")
            return delta.DeltaResult(left_scope=["7"])

        with mock.patch.object(delta, "_fetch_changes_once", flaky), \
                mock.patch.object(delta.time, "sleep"):
            result = delta.fetch_changes(WHEN)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.left_scope, ["7"])

    def test_gives_up_after_the_limit(self):
        def always_busy(since, overlap):
            raise Exception("SMB SessionError: STATUS_PIPE_NOT_AVAILABLE(busy)")

        with mock.patch.object(delta, "_fetch_changes_once", always_busy), \
                mock.patch.object(delta.time, "sleep") as slept:
            with self.assertRaises(Exception):
                delta.fetch_changes(WHEN)
        self.assertEqual(slept.call_count, delta.CONNECT_RETRIES - 1)

    def test_a_real_error_raises_immediately(self):
        calls = []

        def broken(since, overlap):
            calls.append(1)
            raise RuntimeError("invalid column name 'Nope'")

        with mock.patch.object(delta, "_fetch_changes_once", broken):
            with self.assertRaises(RuntimeError):
                delta.fetch_changes(WHEN)
        self.assertEqual(len(calls), 1)


class ResultShape(unittest.TestCase):
    def test_summary_counts_each_bucket(self):
        result = delta.DeltaResult(
            listable=[Part(r_number="1", part_type="Door")],
            left_scope=["2", "3"],
            photo_changed={"4"},
        )
        self.assertEqual(result.summary(), "changed=1 left_scope=2 photos=1")


if __name__ == "__main__":
    unittest.main()
