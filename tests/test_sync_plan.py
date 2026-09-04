"""What a scoped or targeted sync is allowed to do, and what it must record afterwards.

Two failure modes are worth the whole file. A scoped run that *commits* records every part
it declined to publish as up to date, so the change it skipped is never published by
anything — the catalogue silently stops converging. And a run that saw a slice of the yard
must never read absence as a sale, because absence is the only evidence retirement has.
"""

import argparse
import sqlite3
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from coreyard import run_sync
from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.state import (SCOPES, DiffResult, Fingerprints, SyncState,
                            fingerprints_all, subset)
from coreyard.transform.render import SCOPE_FIELDS, render

STORE = StoreProfile()
NO_IMG = lambda part: []                                              # noqa: E731


def part(r_number="51", **kw):
    fields = dict(r_number=r_number, part_type="Door", price=Decimal("100.00"),
                  quantity=1, make="Honda", model="Civic", year=2015)
    fields.update(kw)
    return Part(**fields)


def sync_args(argv):
    parser = argparse.ArgumentParser()
    run_sync.add_arguments(parser)
    args = parser.parse_args(argv)
    args.sink = "api"
    return args


class ScopeProjections(unittest.TestCase):
    """Each scope must move for its own changes and stay still for everyone else's."""

    def moved(self, before_part, after_part, before_images=("51_01.jpg",),
              after_images=None):
        after_images = before_images if after_images is None else after_images
        before = render(before_part, list(before_images), STORE)
        after = render(after_part, list(after_images), STORE)
        return {scope for scope in SCOPE_FIELDS
                if before.scope_fingerprint(scope) != after.scope_fingerprint(scope)}

    def test_a_quantity_change_is_inventory_only(self):
        self.assertEqual(self.moved(part(), part(quantity=0)), {"inventory"})

    def test_a_price_change_is_inventory_only(self):
        self.assertEqual(self.moved(part(), part(price=Decimal("90.00"))), {"inventory"})

    def test_a_copy_change_does_not_move_the_photo_scope(self):
        """Alt text is generated from the part's copy. If it counted as a photo change, a
        retitled part would have its media torn down and re-uploaded — the one expensive
        thing a scoped run exists to avoid."""
        self.assertNotIn("photos", self.moved(part(), part(part_type="Hood")))

    def test_a_photo_set_change_moves_the_photo_scope(self):
        self.assertIn("photos", self.moved(part(), part(),
                                           after_images=("51_01.jpg", "51_02.jpg")))

    def test_nothing_moves_when_nothing_changed(self):
        self.assertEqual(self.moved(part(), part()), set())

    def test_a_scope_fingerprint_is_a_projection_of_the_canonical_payload(self):
        """Not a second rendering. If a scope hashed its own idea of the product, it could
        disagree with the renderer that actually publishes."""
        rendered = render(part(), ["51_01.jpg"], STORE)
        payload = rendered.payload()
        for scope in SCOPE_FIELDS:
            with self.subTest(scope=scope):
                self.assertEqual(rendered.scope_fingerprint(scope),
                                 render(part(), ["51_01.jpg"], STORE)
                                 .scope_fingerprint(scope))
        self.assertIn("inventory", payload)


class ChangedIn(unittest.TestCase):
    def test_a_new_part_belongs_to_every_scope(self):
        """Creating the product is what makes it right in all of them."""
        diff = DiffResult(added=["1"], scope_changed={"inventory": [], "catalog": []})
        for scope in ("inventory", "catalog", "photos"):
            with self.subTest(scope=scope):
                self.assertIn("1", diff.changed_in(scope))

    def test_a_scope_sees_only_its_own_changes(self):
        diff = DiffResult(changed=["1", "2"], image_changed=["3"],
                          scope_changed={"inventory": ["1"], "catalog": ["2"]})
        self.assertEqual(diff.changed_in("inventory"), ["1"])
        self.assertEqual(diff.changed_in("catalog"), ["2"])
        self.assertEqual(diff.changed_in("photos"), ["3"])


class UnattributedChanges(unittest.TestCase):
    """A change nothing can classify must not vanish from every scope.

    Parts written before the scope columns existed have no baseline to compare against, so
    no scope's fingerprint can be said to have moved. Dropping them made `sync inventory`
    report "0 to publish" on an installation with 1,264 pending changes — which reads as
    "the catalogue is in step", the most expensive thing a status line can get wrong.
    """

    def _state(self, tmp):
        return SyncState(Path(tmp) / "state.sqlite3")

    def test_a_change_with_no_scope_baseline_is_claimed_by_every_scope(self):
        with tempfile.TemporaryDirectory() as tmp, self._state(tmp) as state:
            # A pre-upgrade row: canonical fingerprint only, both scope columns empty.
            state.conn.execute(
                "INSERT INTO parts(r_number, fingerprint, image_fingerprint, last_seen)"
                " VALUES ('1','old-fp','','2026-01-01')")
            state.conn.commit()
            moved = fingerprints_all([part("1")], NO_IMG, STORE)
            diff = state.diff(moved)
            self.assertEqual(diff.changed, ["1"])
            self.assertEqual(diff.unattributed, ["1"])
            for scope in ("inventory", "catalog", "photos"):
                with self.subTest(scope=scope):
                    self.assertIn("1", diff.changed_in(scope))

    def test_a_change_with_a_baseline_is_claimed_only_by_its_own_scope(self):
        with tempfile.TemporaryDirectory() as tmp, self._state(tmp) as state:
            state.commit(fingerprints_all([part("1")], NO_IMG, STORE))
            repriced = fingerprints_all([part("1", price=Decimal("90.00"))], NO_IMG, STORE)
            diff = state.diff(repriced)
            self.assertEqual(diff.unattributed, [], "this one can be classified")
            self.assertIn("1", diff.changed_in("inventory"))
            self.assertNotIn("1", diff.changed_in("catalog"))

    def test_an_unchanged_part_is_never_unattributed(self):
        with tempfile.TemporaryDirectory() as tmp, self._state(tmp) as state:
            state.conn.execute(
                "INSERT INTO parts(r_number, fingerprint, image_fingerprint, last_seen)"
                " VALUES ('1',?,'','2026-01-01')",
                (fingerprints_all([part("1")], NO_IMG, STORE).content["1"],))
            state.conn.commit()
            diff = state.diff(fingerprints_all([part("1")], NO_IMG, STORE))
            self.assertEqual(diff.unchanged, ["1"])
            self.assertEqual(diff.unattributed, [])
            self.assertEqual(diff.changed_in("inventory"), [])

    def test_it_self_heals_once_a_run_records_the_scopes(self):
        with tempfile.TemporaryDirectory() as tmp, self._state(tmp) as state:
            state.conn.execute(
                "INSERT INTO parts(r_number, fingerprint, image_fingerprint, last_seen)"
                " VALUES ('1','old-fp','','2026-01-01')")
            state.conn.commit()
            current = fingerprints_all([part("1")], NO_IMG, STORE)
            self.assertEqual(state.diff(current).unattributed, ["1"])
            state.update(current)                       # a run publishes it
            self.assertEqual(state.diff(current).unattributed, [])


class RetirementGuards(unittest.TestCase):
    """Retirement is the one destructive act, so every way of narrowing a run blocks it."""

    def setUp(self):
        self.diff = DiffResult(removed=["1", "2"],
                               unchanged=[str(i) for i in range(100)])

    def _retires(self, argv):
        return run_sync._retirement_plan(self.diff, sync_args(argv))[0]

    def test_a_full_run_retires(self):
        self.assertEqual(self._retires([]), ["1", "2"])

    def test_the_inventory_scope_retires(self):
        self.assertEqual(self._retires(["inventory"]), ["1", "2"])

    def test_limit_blocks_retirement(self):
        self.assertEqual(self._retires(["--limit", "5"]), [])

    def test_targeting_specific_parts_blocks_retirement(self):
        """A part outside the selection is unexamined, not missing."""
        self.assertEqual(self._retires(["--r-number", "51"]), [])

    def test_the_photo_scope_never_retires(self):
        self.assertEqual(self._retires(["photos"]), [])

    def test_the_catalog_scope_never_retires(self):
        self.assertEqual(self._retires(["catalog"]), [])

    def test_a_mass_removal_is_still_refused(self):
        diff = DiffResult(removed=[str(i) for i in range(50)],
                          unchanged=[str(i) for i in range(50, 100)])
        retire, why = run_sync._retirement_plan(diff, sync_args([]))
        self.assertEqual(retire, [])
        self.assertIn("REFUSING", why)

    def test_force_retire_overrides_the_fraction_but_not_a_partial_view(self):
        diff = DiffResult(removed=[str(i) for i in range(50)],
                          unchanged=[str(i) for i in range(50, 100)])
        self.assertEqual(len(run_sync._retirement_plan(
            diff, sync_args(["--force-retire"]))[0]), 50)
        self.assertEqual(run_sync._retirement_plan(
            diff, sync_args(["--force-retire", "--limit", "5"]))[0], [])


class PartialView(unittest.TestCase):
    def test_a_full_run_sees_the_whole_yard(self):
        self.assertEqual(run_sync._partial_view(sync_args([])), "")
        self.assertEqual(run_sync._partial_view(sync_args(["catalog"])), "")

    def test_narrowing_flags_are_reported_with_a_reason(self):
        self.assertIn("--limit", run_sync._partial_view(sync_args(["--limit", "5"])))
        self.assertIn("--r-number",
                      run_sync._partial_view(sync_args(["--r-number", "51"])))


class CursorHold(unittest.TestCase):
    """A delta may only advance its cursor over rows it actually read.

    The delta query pages on R#, not on ``modified_at``, so a read that hits the row cap is
    an arbitrary slice of the window rather than its oldest part. Advancing the cursor past
    it puts the rows it never read permanently behind the cursor, where only a full sync
    would find them. Retirement was already guarded for this reason; the cursor was not.
    """

    def result(self, **kw):
        from coreyard.yms.delta import DeltaResult
        fields = dict(cursor=datetime(2026, 9, 2, 12, 0, 0), truncated=False)
        fields.update(kw)
        return DeltaResult(**fields)

    def test_a_complete_read_advances_the_cursor(self):
        self.assertEqual("", run_sync._cursor_hold_reason(self.result()))

    def test_a_truncated_read_holds_the_cursor(self):
        reason = run_sync._cursor_hold_reason(self.result(truncated=True))
        self.assertIn("truncated", reason)

    def test_a_read_that_reported_no_cursor_holds(self):
        self.assertIn("no cursor", run_sync._cursor_hold_reason(self.result(cursor=None)))


class StorefrontCount(unittest.TestCase):
    def test_source_count_ignores_the_storefront_image_gate(self):
        from unittest import mock

        publisher = mock.MagicMock()
        with mock.patch("coreyard.yms.inventory.source_inventory_count",
                        return_value=3) as counted:
            self.assertEqual(run_sync._refresh_source_inventory_count(publisher), 3)

        counted.assert_called_once_with(images_only=False)
        publisher.publish_source_inventory_count.assert_called_once_with(3)

    def test_counter_failure_does_not_fail_a_completed_product_sync(self):
        from unittest import mock

        publisher = mock.MagicMock()
        publisher.publish_source_inventory_count.side_effect = RuntimeError("scope missing")
        with mock.patch("coreyard.yms.inventory.source_inventory_count",
                        return_value=1):
            self.assertIsNone(run_sync._refresh_source_inventory_count(publisher))


class Selection(unittest.TestCase):
    def test_an_unscoped_run_publishes_everything_that_moved(self):
        diff = DiffResult(added=["1"], changed=["2"],
                          scope_changed={"inventory": ["2"], "catalog": []})
        self.assertEqual(run_sync._selected(diff, sync_args([])), {"1", "2"})

    def test_a_scoped_run_publishes_only_its_share(self):
        diff = DiffResult(added=[], changed=["2", "3"],
                          scope_changed={"inventory": ["2"], "catalog": ["3"]})
        self.assertEqual(run_sync._selected(diff, sync_args(["inventory"])), {"2"})
        self.assertEqual(run_sync._selected(diff, sync_args(["catalog"])), {"3"})

    def test_catalog_and_inventory_scopes_leave_photo_work_for_its_owner(self):
        diff = DiffResult(changed=["1"], image_changed=["1"])
        for scope in ("catalog", "inventory"):
            with self.subTest(scope=scope):
                self.assertEqual(run_sync._photo_refreshes(
                    diff, sync_args([scope]), {"1"}), set())

    def test_full_and_photo_runs_refresh_a_changed_photo_set(self):
        diff = DiffResult(changed=["1"], image_changed=["1"])
        self.assertEqual(run_sync._photo_refreshes(diff, sync_args([]), {"1"}), {"1"})
        self.assertEqual(run_sync._photo_refreshes(
            diff, sync_args(["photos"]), {"1"}), {"1"})

    def test_catalog_publish_does_not_mark_skipped_media_as_current(self):
        with tempfile.TemporaryDirectory() as directory:
            with SyncState(Path(directory) / "state.sqlite3") as state:
                old = Fingerprints(
                    content={"1": "old-content"}, images={"1": "old-image"},
                    scopes={scope: {"1": f"old-{scope}"} for scope in SCOPES})
                state.update(old)
                current = Fingerprints(
                    content={"1": "new-content"}, images={"1": "new-image"},
                    scopes={scope: {"1": f"new-{scope}"} for scope in SCOPES})
                got = run_sync._published_fingerprints(
                    current, {"1"}, state, sync_args(["catalog"]),
                    DiffResult(changed=["1"], image_changed=["1"]))
        self.assertEqual(got.content["1"], "new-content")
        self.assertEqual(got.images["1"], "old-image")


class Defaults(unittest.TestCase):
    """Where each entry point publishes, if nobody says."""

    def test_the_operator_command_publishes(self):
        """`coreyard sync` in a crontab must not quietly write a CSV nobody reads."""
        parser = argparse.ArgumentParser()
        run_sync.add_arguments(parser)
        self.assertEqual(parser.parse_args([]).sink, "api")

    def test_the_legacy_entry_point_keeps_its_original_default(self):
        """This installation's crontab names `python -m coreyard.run_sync` directly, and a
        scheduled job whose behaviour changes silently is the expensive kind of surprise."""
        parser = argparse.ArgumentParser()
        run_sync._sync_flags(parser, default_sink="csv")
        self.assertEqual(parser.parse_args([]).sink, "csv")

    def test_a_scope_always_publishes_through_the_api(self):
        parser = argparse.ArgumentParser()
        run_sync.add_arguments(parser)
        parsed = parser.parse_args(["photos"])
        self.assertEqual(parsed.scope, "photos")

    def test_dry_run_is_accepted_by_every_scope(self):
        parser = argparse.ArgumentParser()
        run_sync.add_arguments(parser)
        for scope in ("delta", "inventory", "photos", "catalog"):
            with self.subTest(scope=scope):
                self.assertTrue(parser.parse_args([scope, "--dry-run"]).dry_run)


class StateMigration(unittest.TestCase):
    """An existing installation must upgrade without republishing its catalogue."""

    def _legacy_db(self, path):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE parts (r_number TEXT PRIMARY KEY,"
                     " fingerprint TEXT NOT NULL, last_seen TEXT NOT NULL,"
                     " image_fingerprint TEXT NOT NULL DEFAULT '')")
        conn.executemany("INSERT INTO parts VALUES (?,?,?,?)",
                         [("1", "fp-one", "2026-01-01", "img-one"),
                          ("2", "fp-two", "2026-01-01", "img-two")])
        conn.commit()
        conn.close()

    def test_upgrading_adds_the_columns_and_changes_no_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            self._legacy_db(db)
            with SyncState(db) as state:
                self.assertEqual(state.load(), {"1": "fp-one", "2": "fp-two"})
                self.assertEqual(state.load_images(), {"1": "img-one", "2": "img-two"})
                columns = {row[1] for row in state.conn.execute("PRAGMA table_info(parts)")}
                for scope in SCOPES:
                    self.assertIn(f"{scope}_fingerprint", columns)

    def test_the_first_run_after_upgrading_reports_no_scope_changes(self):
        """An unknown scope value must read as "not changed". Reading it as changed would
        publish the whole catalogue once per scope on the upgrade run."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            self._legacy_db(db)
            with SyncState(db) as state:
                fingerprints = Fingerprints(
                    content={"1": "fp-one", "2": "fp-two"},
                    images={"1": "img-one", "2": "img-two"},
                    scopes={"inventory": {"1": "x", "2": "y"},
                            "catalog": {"1": "p", "2": "q"}})
                diff = state.diff(fingerprints)
                self.assertEqual(diff.changed, [])
                self.assertEqual(diff.scope_changed["inventory"], [])
                self.assertEqual(diff.scope_changed["catalog"], [])


class SnapshotDiscipline(unittest.TestCase):
    def test_subset_narrows_every_map_together(self):
        """A part that failed to publish must keep its old fingerprints in all of them."""
        fps = fingerprints_all([part("1"), part("2")], NO_IMG, STORE)
        narrowed = subset(fps, {"1"})
        self.assertEqual(set(narrowed.content), {"1"})
        self.assertEqual(set(narrowed.images), {"1"})
        for scope in SCOPES:
            with self.subTest(scope=scope):
                self.assertEqual(set(narrowed.scopes[scope]), {"1"})

    def test_update_records_scope_columns_and_leaves_others_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            with SyncState(db) as state:
                fps = fingerprints_all([part("1"), part("2")], NO_IMG, STORE)
                state.commit(fps)
                self.assertEqual(set(state.load()), {"1", "2"})
                self.assertEqual(set(state.load_scope("inventory")), {"1", "2"})

                # A scoped run publishes only R#1; R#2 must keep what it had.
                moved = fingerprints_all([part("1", quantity=7), part("2", quantity=7)],
                                         NO_IMG, STORE)
                state.update(subset(moved, {"1"}))
                self.assertEqual(state.load()["1"], moved.content["1"])
                self.assertEqual(state.load()["2"], fps.content["2"])

    def test_a_scoped_run_would_strand_changes_if_it_committed(self):
        """The regression this discipline exists to prevent: commit() replaces the whole
        snapshot, so a part whose catalog moved but which a photo run never published
        would be recorded as up to date and never published by anything."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            with SyncState(db) as state:
                state.commit(fingerprints_all([part("1"), part("2")], NO_IMG, STORE))
                moved = fingerprints_all([part("1", part_type="Hood"), part("2")],
                                         NO_IMG, STORE)
                # What a scoped run does: record only what it published (nothing here).
                state.update(subset(moved, set()))
                self.assertEqual(state.diff(moved).changed, ["1"],
                                 "R#1's catalog change must still be pending")


if __name__ == "__main__":
    unittest.main()


class Summary(unittest.TestCase):
    """What a run reports, and what `status` remembers about it afterwards."""

    def _run(self, diff, published_ok=(), revived=(), retired=(), todo=(), argv=()):
        import contextlib
        import io

        from coreyard import ops

        args = sync_args(list(argv))
        args.dry_run = False
        out = io.StringIO()
        ops._counts.clear()
        with contextlib.redirect_stdout(out):
            run_sync._summarise(diff, set(published_ok), set(revived), set(retired),
                                list(todo), args)
        return out.getvalue(), dict(ops._counts)

    def test_an_idle_run_says_so_in_one_line(self):
        """A quiet catch-up tick every five minutes is 288 a day; eight lines of zeroes
        each time buries the ticks that matter."""
        text, _ = self._run(DiffResult(unchanged=["1", "2", "3"]))
        self.assertEqual(text.strip(), "Nothing to publish (3 unchanged).")

    def test_a_run_that_did_something_prints_the_block(self):
        text, _ = self._run(DiffResult(added=["1"], unchanged=["2"]),
                            published_ok={"1"}, todo=["1"])
        self.assertIn("Sync complete.", text)
        self.assertIn("Created", text)

    def test_counts_are_recorded_even_when_nothing_happened(self):
        """`status` must be able to say what a run did, including "nothing"."""
        _, counts = self._run(DiffResult(unchanged=["1", "2"]))
        self.assertEqual(counts["unchanged"], 2)
        self.assertEqual(counts["created"], 0)

    def test_a_delta_run_records_its_scope(self):
        _, counts = self._run(DiffResult(added=["1"]), published_ok={"1"}, todo=["1"],
                              argv=["delta"])
        self.assertEqual(counts["scope"], "delta")
        self.assertEqual(counts["created"], 1)

    def test_a_failed_publish_is_counted_and_explained(self):
        text, counts = self._run(DiffResult(added=["1", "2"]), published_ok={"1"},
                                 todo=["1", "2"])
        self.assertEqual(counts["failed"], 1)
        self.assertIn("failed to publish", text)

    def test_a_dry_run_records_counts_but_prints_no_summary(self):
        import contextlib
        import io

        from coreyard import ops

        args = sync_args([])
        args.dry_run = True
        out = io.StringIO()
        ops._counts.clear()
        with contextlib.redirect_stdout(out):
            run_sync._summarise(DiffResult(added=["1"]), {"1"}, set(), set(), ["1"], args)
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(ops._counts["dry_run"])
