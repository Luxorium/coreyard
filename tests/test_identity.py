"""DATA-01: whose catalogue the sync state describes.

A fingerprint is a claim of the form "the storefront holds this version of R#51". It is only
meaningful about one store, under one handle prefix, and neither of those is recorded in the
rows themselves — so an installation re-pointed at a different store, or renamed, inherits a
set of claims that are all false. Quietly, and in opposite directions:

* **A different store.** Every fingerprint still says the part is published, so the next sync
  finds nothing to do. The new store stays empty and the run reports success.
* **A different handle prefix.** The handle is part of the rendered product, so every
  fingerprint moves at once: the next sync republishes the entire catalogue under new
  handles, beside the old products, which stay live, on sale and now unmanaged.

The snapshot therefore says whose it is, a command that writes to the store refuses when
that disagrees with the configuration, and `coreyard state adopt` is how someone says they
meant it.
"""

import contextlib
import io
import tempfile
import unittest
import unittest.mock as mock
from argparse import Namespace
from decimal import Decimal
from pathlib import Path

from coreyard import cli, state_cli
from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.state import SyncState, fingerprints_all

STORE = "example.myshopify.com"
OTHER = "somewhere-else.myshopify.com"
NO_IMG = lambda part: []                                              # noqa: E731


def part(r_number="51"):
    return Part(r_number=r_number, part_type="Door", price=Decimal("100.00"),
                quantity=1, make="Honda", model="Civic", year=2015)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.sqlite3"
        with SyncState(self.db) as state:
            state.update(fingerprints_all([part()], NO_IMG, StoreProfile()))

    @contextlib.contextmanager
    def installation(self, store=STORE, prefix="coreyard"):
        with mock.patch.object(cli, "_installation", return_value=(store, prefix)), \
                mock.patch("coreyard.state.DEFAULT_STATE_DB", self.db), \
                mock.patch("coreyard.cli.SUPPORT", dict(cli.SUPPORT)):
            yield


class TheSnapshotSaysWhoseItIs(Fixture):
    def test_a_new_snapshot_carries_no_claim(self):
        with SyncState(self.db) as state:
            self.assertEqual(state.identity(), {})

    def test_an_unlabelled_snapshot_is_never_a_mismatch(self):
        """An installation that has been publishing all along must not stop at an upgrade."""
        with SyncState(self.db) as state:
            self.assertEqual(state.mismatch(STORE, "coreyard"), "")

    def test_it_remembers_what_it_was_claimed_for(self):
        with SyncState(self.db) as state:
            state.claim(STORE, "coreyard")
            self.assertEqual(state.identity(),
                             {"store": STORE, "handle_prefix": "coreyard"})
            self.assertEqual(state.mismatch(STORE, "coreyard"), "")

    def test_a_different_store_is_a_mismatch(self):
        with SyncState(self.db) as state:
            state.claim(STORE, "coreyard")
            self.assertIn(OTHER, state.mismatch(OTHER, "coreyard"))

    def test_a_different_handle_prefix_is_a_mismatch(self):
        with SyncState(self.db) as state:
            state.claim(STORE, "coreyard")
            self.assertIn("handle prefix", state.mismatch(STORE, "yard"))

    def test_both_are_reported_together(self):
        with SyncState(self.db) as state:
            state.claim(STORE, "coreyard")
            moved = state.mismatch(OTHER, "yard")
            self.assertIn("store", moved)
            self.assertIn("handle prefix", moved)

    def test_the_claim_survives_reopening(self):
        with SyncState(self.db) as state:
            state.claim(STORE, "coreyard")
        with SyncState(self.db) as state:
            self.assertEqual(state.identity()["store"], STORE)

    def test_an_older_snapshot_gains_the_table_on_open(self):
        import sqlite3

        legacy = Path(self.tmp.name) / "legacy.sqlite3"
        conn = sqlite3.connect(legacy)
        with conn:
            conn.execute("CREATE TABLE parts (r_number TEXT PRIMARY KEY,"
                         " fingerprint TEXT NOT NULL, last_seen TEXT NOT NULL DEFAULT '')")
            conn.execute("INSERT INTO parts VALUES ('51', 'abc', '')")
        conn.close()
        with SyncState(legacy) as state:
            self.assertEqual(state.identity(), {})
            self.assertEqual(state.load(), {"51": "abc"})


class AWriteIsRefusedAfterAMove(Fixture):
    def setUp(self):
        super().setUp()
        with SyncState(self.db) as state:
            state.claim(STORE, "coreyard")

    def refuse(self, path="sync", **kw):
        args = Namespace(dry_run=False, **kw)
        err = io.StringIO()
        with self.installation(store=OTHER), contextlib.redirect_stderr(err):
            code = cli._snapshot_moved(path, args)
        return code, err.getvalue()

    def test_a_publishing_command_stops_before_it_writes(self):
        code, text = self.refuse()
        self.assertEqual(code, 2)
        self.assertIn(OTHER, text)
        self.assertIn("Nothing was changed", text)

    def test_the_refusal_says_how_to_proceed(self):
        _, text = self.refuse()
        self.assertIn("state adopt", text)
        self.assertIn("COREYARD_HOME", text)

    def test_a_dry_run_is_told_and_allowed_through(self):
        """It changes nothing, and the diff is exactly what the operator needs to see."""
        args = Namespace(dry_run=True)
        err = io.StringIO()
        with self.installation(store=OTHER), contextlib.redirect_stderr(err):
            code = cli._snapshot_moved("sync", args)
        self.assertIsNone(code)
        self.assertIn("dry run changes nothing", err.getvalue())

    def test_a_read_only_command_is_not_affected(self):
        """`status` and `doctor` are what someone runs *because* something is wrong."""
        args = Namespace(dry_run=False)
        with self.installation(store=OTHER):
            self.assertIsNone(cli._snapshot_moved("status", args))
            self.assertIsNone(cli._snapshot_moved("doctor", args))

    def test_the_matching_installation_runs(self):
        args = Namespace(dry_run=False)
        with self.installation(store=STORE):
            self.assertIsNone(cli._snapshot_moved("sync", args))

    def test_an_unconfigured_installation_is_left_to_the_other_checks(self):
        args = Namespace(dry_run=False)
        with mock.patch.object(cli, "_installation", return_value=None), \
                mock.patch("coreyard.state.DEFAULT_STATE_DB", self.db):
            self.assertIsNone(cli._snapshot_moved("sync", args))


class AnUnlabelledSnapshotIsAdoptedByUse(Fixture):
    def test_a_successful_run_labels_it(self):
        with self.installation():
            cli._claim_snapshot("sync", 0)
        with SyncState(self.db) as state:
            self.assertEqual(state.identity()["store"], STORE)

    def test_a_failed_run_does_not(self):
        with self.installation():
            cli._claim_snapshot("sync", 2)
        with SyncState(self.db) as state:
            self.assertEqual(state.identity(), {})

    def test_it_never_overwrites_a_claim_that_is_already_there(self):
        """Otherwise the refusal would erase its own reason on the next run."""
        with SyncState(self.db) as state:
            state.claim(STORE, "coreyard")
        with self.installation(store=OTHER, prefix="yard"):
            cli._claim_snapshot("sync", 0)
        with SyncState(self.db) as state:
            self.assertEqual(state.identity()["store"], STORE)

    def test_a_read_only_command_labels_nothing(self):
        with self.installation():
            cli._claim_snapshot("status", 0)
        with SyncState(self.db) as state:
            self.assertEqual(state.identity(), {})


class Adopting(Fixture):
    def adopt(self, apply=False, store=OTHER, prefix="coreyard"):
        out = io.StringIO()
        with mock.patch.object(state_cli, "_identity", return_value=(store, prefix)), \
                mock.patch("coreyard.state_cli.DEFAULT_STATE_DB", self.db), \
                contextlib.redirect_stdout(out):
            code = state_cli.cmd_adopt(Namespace(apply=apply))
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        with SyncState(self.db) as state:
            state.claim(STORE, "coreyard")

    def test_it_says_what_would_change_and_writes_nothing(self):
        code, text = self.adopt()
        self.assertEqual(code, 0)
        self.assertIn("different installation", text)
        self.assertIn("Plan only", text)
        with SyncState(self.db) as state:
            self.assertEqual(state.identity()["store"], STORE)

    def test_it_explains_what_the_new_store_would_find(self):
        _, text = self.adopt()
        self.assertIn("stay empty", text)

    def test_a_prefix_change_warns_about_the_products_left_behind(self):
        _, text = self.adopt(store=STORE, prefix="yard")
        self.assertIn("unmanaged", text)

    def test_applying_rewrites_the_identity(self):
        code, text = self.adopt(apply=True)
        self.assertEqual(code, 0)
        self.assertIn("Adopted", text)
        with SyncState(self.db) as state:
            self.assertEqual(state.identity()["store"], OTHER)

    def test_adopting_changes_no_fingerprint(self):
        """It relabels the snapshot. Deciding what to publish is still the sync's job."""
        with SyncState(self.db) as state:
            before = state.load()
        self.adopt(apply=True)
        with SyncState(self.db) as state:
            self.assertEqual(state.load(), before)

    def test_the_matching_installation_is_a_no_op(self):
        code, text = self.adopt(store=STORE, prefix="coreyard")
        self.assertEqual(code, 0)
        self.assertIn("already belongs", text)


class AmbiguousIdentifiers(unittest.TestCase):
    """An R# that matches more than one source row is a mapping fault, not a yard fact.

    The R# is the source's own key for a physical part, so two rows carrying one means the
    configured `source` joins something one-to-many. What must not happen is either of the
    two easy answers: publishing whichever row the paging read last, or dropping the part —
    a full run reads absence as a sale, so holding it out of the extract would archive a
    part sitting in the yard.
    """

    def collapse(self, parts):
        from coreyard.yms.inventory import unambiguous

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kept = unambiguous(parts)
        return kept, out.getvalue()

    def test_an_unambiguous_extract_is_untouched(self):
        kept, said = self.collapse([part("1"), part("2")])
        self.assertEqual([p.uid() for p in kept], ["1", "2"])
        self.assertEqual(said, "")

    def test_rows_that_merely_repeat_collapse_quietly(self):
        """What a join against a lookup table produces. There is nothing to decide."""
        kept, said = self.collapse([part("1"), part("1"), part("2")])
        self.assertEqual([p.uid() for p in kept], ["1", "2"])
        self.assertEqual(said, "")

    def test_rows_that_disagree_are_named(self):
        cheap, dear = part("7"), part("7")
        dear.price = Decimal("999.00")
        _, said = self.collapse([cheap, dear])
        self.assertIn("R#7", said)
        self.assertIn("AMBIGUOUS", said)
        self.assertIn("schema mapping", said)

    def test_the_first_row_read_wins_deterministically(self):
        """Ordered by R#, so "the first" is a fact about the extract rather than about
        which page happened to be read last."""
        cheap, dear = part("7"), part("7")
        dear.price = Decimal("999.00")
        kept, _ = self.collapse([cheap, dear])
        self.assertEqual([p.price for p in kept], [Decimal("100.00")])

    def test_the_part_stays_in_the_extract_so_nothing_retires_it(self):
        """The one answer that would be worse than guessing: absence is how a full run
        recognises a sale, and this part is sitting in the yard."""
        cheap, dear = part("7"), part("7")
        dear.quantity = 4
        kept, _ = self.collapse([cheap, dear])
        self.assertEqual([p.uid() for p in kept], ["7"])

    def test_a_long_list_is_summarised(self):
        parts = []
        for i in range(10):
            a, b = part(str(i)), part(str(i))
            b.quantity = 9
            parts += [a, b]
        _, said = self.collapse(parts)
        self.assertIn("and 5 more", said)


if __name__ == "__main__":
    unittest.main()
