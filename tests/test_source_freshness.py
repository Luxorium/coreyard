"""DATA-07: a read that worked is not a read that is current.

Where the source is a live database, those are the same thing — the query happened, so the
answer is now. Where it is an export, they are not: a nightly job that stopped writing leaves
a file that parses perfectly, answers every probe, and describes a yard that has moved on.
Publishing from it raises availability for stock that was sold days ago and, worse, retires
every part the stale file no longer lists, because absence is how a full run recognises a
sale.

So a source declares whether its clock dates its data, and only the kind that does can be
stale. The refusal is a refusal rather than a warning, and the exposure it cannot fix —
parts already listed stay on sale — is documented rather than implied.
"""

import contextlib
import io
import os
import tempfile
import time
import unittest
import unittest.mock as mock
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

from coreyard import alerts, cli
from coreyard import source as source_mod


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = Path(self.tmp.name) / "parts.csv"
        self.csv.write_text("r_number,part_type,price\n51,DOOR,10.00\n", encoding="utf-8")
        self.spec = f"tabular:{self.csv}"

    def age(self, hours: float) -> None:
        when = time.time() - hours * 3600
        os.utime(self.csv, (when, when))

    def freshness(self, **env):
        with mock.patch.dict(os.environ, env, clear=False):
            return source_mod.freshness(spec=self.spec)


class OnlyADatedSourceCanBeStale(Fixture):
    def test_a_live_database_has_no_age(self):
        """Its clock is the current time, so a maximum age would be a limit on nothing."""
        state = source_mod.freshness(spec="database")
        self.assertIsNone(state.age)
        self.assertFalse(state.stale)
        self.assertIn("live", state.detail)

    def test_an_export_is_aged_by_its_modification_time(self):
        self.age(3)
        state = self.freshness()
        self.assertAlmostEqual(state.age.total_seconds() / 3600, 3, places=1)
        self.assertFalse(state.stale)

    def test_past_the_limit_it_is_stale(self):
        self.age(40)
        self.assertTrue(self.freshness().stale)

    def test_the_limit_is_configurable(self):
        self.age(40)
        self.assertFalse(self.freshness(SOURCE_MAX_AGE_HOURS="72").stale)

    def test_zero_turns_the_check_off(self):
        """For a site whose export is deliberately occasional and would rather publish."""
        self.age(500)
        state = self.freshness(SOURCE_MAX_AGE_HOURS="0")
        self.assertFalse(state.stale)
        self.assertIsNotNone(state.age)

    def test_an_unreadable_modification_time_is_stale_not_fine(self):
        """Unknown is not fresh. A source that cannot say when it was written is exactly
        the case this guards, and treating silence as freshness is how it would be missed."""
        self.csv.unlink()
        self.assertTrue(self.freshness().stale)

    def test_a_clock_ahead_of_ours_is_not_staleness(self):
        self.age(-5)
        state = self.freshness()
        self.assertEqual(state.age, timedelta(0))
        self.assertFalse(state.stale)

    def test_a_nonsense_limit_falls_back_to_the_default(self):
        self.age(40)
        self.assertTrue(self.freshness(SOURCE_MAX_AGE_HOURS="soon").stale)


class AWriteIsRefused(Fixture):
    def refuse(self, path="sync", dry_run=False):
        err = io.StringIO()
        with mock.patch.object(source_mod, "freshness",
                               return_value=source_mod.Freshness(
                                   timedelta(hours=40), timedelta(hours=24), True,
                                   "tabular: 40.0h old, limit 24h")), \
                contextlib.redirect_stderr(err):
            code = cli._source_too_old(path, Namespace(dry_run=dry_run))
        return code, err.getvalue()

    def test_a_publishing_command_stops_before_it_writes(self):
        code, text = self.refuse()
        self.assertEqual(code, 2)
        self.assertIn("stale", text)
        self.assertIn("Nothing was changed", text)

    def test_the_refusal_says_what_it_is_protecting_against(self):
        _, text = self.refuse()
        self.assertIn("retire", text)
        self.assertIn("SOURCE_MAX_AGE_HOURS", text)

    def test_it_says_that_listed_parts_stay_on_sale(self):
        """The exposure the refusal cannot fix, named where someone will read it."""
        _, text = self.refuse()
        self.assertIn("already on the storefront", text)

    def test_a_dry_run_is_told_and_allowed_through(self):
        code, text = self.refuse(dry_run=True)
        self.assertIsNone(code)
        self.assertIn("dry run changes nothing", text)

    def test_a_read_only_command_is_not_affected(self):
        """`status` and `doctor` are what an operator runs to find out about this."""
        for path in ("status", "doctor"):
            with self.subTest(path=path):
                self.assertIsNone(self.refuse(path=path)[0])

    def test_a_fresh_source_is_not_refused(self):
        self.age(1)
        with mock.patch.object(source_mod, "freshness",
                               return_value=source_mod.freshness(spec=self.spec)):
            self.assertIsNone(cli._source_too_old("sync", Namespace(dry_run=False)))


class TheOperatorIsTold(Fixture):
    def caps(self):
        return mock.Mock(source_spec=self.spec)

    def test_a_stale_export_raises_an_alert(self):
        self.age(40)
        found = alerts.check_source_age(self.caps())
        self.assertEqual([a.key for a in found], ["source.stale"])
        self.assertIn("stay on sale", found[0].detail)

    def test_a_fresh_one_raises_nothing(self):
        self.age(1)
        self.assertEqual(alerts.check_source_age(self.caps()), [])

    def test_a_live_database_raises_nothing(self):
        self.assertEqual(alerts.check_source_age(mock.Mock(source_spec="database")), [])

    def test_doctor_reports_the_age_either_way(self):
        from coreyard import doctor

        self.age(1)
        caps = mock.Mock(source_spec=self.spec, source_kind="tabular")
        caps.get.return_value = mock.Mock(state=doctor.OK, detail="")
        caps.enabled.return_value = False
        results = doctor.check_liveness(caps=caps)
        ages = [r for r in results if r[1] == "source age"]
        self.assertEqual(len(ages), 1)
        self.assertEqual(ages[0][0], doctor.OK)

    def test_doctor_fails_on_a_stale_one(self):
        from coreyard import doctor

        self.age(40)
        caps = mock.Mock(source_spec=self.spec, source_kind="tabular")
        caps.get.return_value = mock.Mock(state=doctor.OK, detail="")
        caps.enabled.return_value = False
        ages = [r for r in doctor.check_liveness(caps=caps) if r[1] == "source age"]
        self.assertEqual(ages[0][0], doctor.FAIL)


if __name__ == "__main__":
    unittest.main()
