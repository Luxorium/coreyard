"""The contract every source adapter must satisfy, and a battery that proves it.

`Source` is a `runtime_checkable` Protocol, which sounds stronger than it is: `isinstance`
against one checks that the *names* exist and nothing else. An adapter with the wrong
signature, the wrong return type, or an inconsistent idea of "listable" passes that check
and then fails somewhere downstream, in a sync, on a customer's yard.

So the contract lives here instead, as behaviour. `SourceContract` is written against no
particular adapter: subclass it, return one from `source()` built over the bundled
seven-part example, and the whole battery runs. A second yard system gets its coverage from
a subclass with a factory in it, which is the point — the seam is only real if conforming to
it is cheaper than forking.

`StaticContract` runs against *every* registered adapter, including ones that cannot be
constructed offline, and covers what can be checked without a yard: registration, declared
traits, signatures, and the offline reachability probe.
"""

import csv
import inspect
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from coreyard.config import bundled
from coreyard.models import Part
from coreyard.source import _ADAPTERS, Source, SourceTraits, kinds
from coreyard.source.database import DatabaseSource
from coreyard.source.tabular import TabularSource

EXAMPLE = bundled("parts.csv")


def example_r_numbers() -> set:
    with EXAMPLE.open(encoding="utf-8") as handle:
        return {row["r_number"].strip() for row in csv.DictReader(handle)
                if row.get("r_number", "").strip()}


class SourceContract:
    """Behaviour required of every source. Subclass and implement :meth:`source`.

    Deliberately not a `TestCase` itself: unittest would discover and run the abstract
    version, fail on the missing factory, and teach everyone to ignore a red suite.
    """

    def source(self):
        raise NotImplementedError("return an adapter over the bundled example yard")

    def expected_r_numbers(self) -> set:
        return example_r_numbers()

    # -- what it returns ---------------------------------------------------------

    def test_parts_returns_parts(self):
        parts = self.source().parts()
        self.assertTrue(parts, "the example yard is not empty")
        for part in parts:
            self.assertIsInstance(part, Part)

    def test_every_part_has_an_identity(self):
        """R# is the SKU, the handle key, the photo stem and the state key at once."""
        for part in self.source().parts():
            self.assertTrue(str(part.r_number).strip(),
                            f"a part came back with no r_number: {part!r}")

    def test_identities_are_unique(self):
        parts = self.source().parts()
        seen = [p.r_number for p in parts]
        self.assertEqual(len(seen), len(set(seen)), "two parts share an R#")

    def test_limit_is_a_ceiling(self):
        self.assertLessEqual(len(self.source().parts(limit=3)), 3)

    def test_limit_of_zero_or_none_is_not_a_limit(self):
        """`limit=None` means everything; a caller passing it must not get an empty yard."""
        self.assertEqual(len(self.source().parts(limit=None)),
                         len(self.source().parts()))

    # -- the two views must agree -------------------------------------------------

    def test_listable_identities_match_the_parts(self):
        """The cheap half of reconciliation must answer the same question as the full one.

        When these disagree, sync publishes a part and reconciliation archives it on the
        next tick, forever. It is the single most expensive way for an adapter to be subtly
        wrong, and it is invisible until a catalogue starts flapping.
        """
        source = self.source()
        self.assertEqual(source.listable_r_numbers(),
                         {p.r_number for p in source.parts()})

    def test_lookup_returns_what_was_asked_for(self):
        source = self.source()
        wanted = sorted(self.expected_r_numbers())[:2]
        found = source.parts_by_r_number(wanted)
        self.assertEqual(set(found), set(wanted))
        for r_number, part in found.items():
            self.assertEqual(part.r_number, r_number, "keyed by a different R# than it holds")

    def test_lookup_of_nothing_is_empty_not_everything(self):
        self.assertEqual(self.source().parts_by_r_number([]), {})

    def test_lookup_of_an_unknown_identity_omits_it(self):
        """Absent, not an exception and not a blank Part: a sold part may simply be gone."""
        self.assertEqual(self.source().parts_by_r_number(["no-such-r-number-9999"]), {})

    # -- clock and liveness -------------------------------------------------------

    def test_the_clock_survives_being_stored_as_a_cursor(self):
        """Exactly what the delta path does to it, which is the only thing it is for.

        `run_sync` stores `cursor.isoformat()` and reads it back with `fromisoformat`, and
        subtracts an overlap before storing. Awareness is deliberately not required: SQL
        Server's `GETDATE()` has no zone and a file's mtime is local, so both shipped
        adapters return naive datetimes and `doctor._age` handles either. What must hold is
        that the value round-trips.
        """
        from datetime import timedelta

        now = self.source().server_now()
        self.assertIsInstance(now, datetime)
        self.assertIsInstance(now - timedelta(minutes=5), datetime)
        self.assertEqual(datetime.fromisoformat(now.isoformat()), now)

    def test_the_clock_does_not_change_flavour_between_calls(self):
        """Aware once and naive the next time raises `TypeError` on the comparison only."""
        source = self.source()
        first, second = source.server_now(), source.server_now()
        self.assertEqual(first.tzinfo is None, second.tzinfo is None)
        self.assertIsInstance(second - first, __import__("datetime").timedelta)

    def test_ping_says_something_short(self):
        line = self.source().ping()
        self.assertIsInstance(line, str)
        self.assertTrue(line.strip(), "ping returned nothing for `doctor` to show")


class ListablePolicyContract:
    """DATA-02: every source applies the *same* documented listable policy.

    A source decides what reaches the storefront at all, so two sources that disagree about
    one row build two different catalogues from the same yard. Both divergences this pins
    down were real, and both were found by writing it:

    * `STORE_REQUIRE_IMAGES=true` was ignored by any source that was not the database,
      because `fetch_parts` defaulted `images_only` from the site's photo policy *after*
      handing off — so a CSV yard published every part with no photographs.
    * A part with quantity 0 was published, because `Part.is_listable` checks price and
      identity but not availability: its docstring says availability is enforced upstream in
      the SQL WHERE clause, and an export has no WHERE clause.

    Subclass alongside :class:`SourceContract` and implement :meth:`source_from_rows` for any
    adapter that can be built over arbitrary rows. An adapter that cannot — the database one
    needs a yard — simply does not mix this in.
    """

    def source_from_rows(self, rows: "list[dict]"):
        raise NotImplementedError("build an adapter over these rows, or omit this mixin")

    def listed(self, rows, **kwargs) -> set:
        return {p.r_number for p in self.source_from_rows(rows).parts(**kwargs)}

    def test_a_positively_priced_available_part_is_listed(self):
        self.assertEqual(
            self.listed([{"r_number": "1", "part_type": "Engine", "price": "100"}]), {"1"})

    def test_price_must_be_positive(self):
        for price in ("0", "0.00", "-5", "", "not a number"):
            with self.subTest(price=price):
                self.assertEqual(
                    self.listed([{"r_number": "1", "part_type": "Engine", "price": price}]),
                    set(), f"a part priced {price!r} reached the storefront")

    def test_a_part_with_no_identity_is_dropped(self):
        """R# is the SKU, the handle and the state key. A row without one cannot be managed."""
        self.assertEqual(
            self.listed([{"r_number": "", "part_type": "Engine", "price": "100"}]), set())

    def test_a_sold_part_is_not_listed(self):
        self.assertEqual(
            self.listed([{"r_number": "1", "part_type": "Engine", "price": "100",
                          "quantity": "0"}]), set())

    def test_a_missing_quantity_means_unknown_not_zero(self):
        """A two-column export naming no quantity is a list of parts the yard has."""
        self.assertEqual(
            self.listed([{"r_number": "1", "part_type": "Engine", "price": "100"}]), {"1"})

    def test_the_photo_requirement_excludes_unphotographed_parts(self):
        rows = [{"r_number": "1", "part_type": "Engine", "price": "100"}]
        self.assertEqual(self.listed(rows, images_only=True), set())
        self.assertEqual(self.listed(rows, images_only=False), {"1"})

    def test_the_two_views_agree_on_every_edge_case(self):
        """Whatever the policy decides, `listable_r_numbers` must decide it identically."""
        rows = [
            {"r_number": "1", "part_type": "Engine", "price": "100", "quantity": "1"},
            {"r_number": "2", "part_type": "Door", "price": "0", "quantity": "1"},
            {"r_number": "3", "part_type": "Hood", "price": "50", "quantity": "0"},
            {"r_number": "", "part_type": "Wheel", "price": "50", "quantity": "1"},
        ]
        source = self.source_from_rows(rows)
        self.assertEqual(source.listable_r_numbers(),
                         {p.r_number for p in source.parts()})
        self.assertEqual(source.listable_r_numbers(), {"1"})


class TabularSourceContract(SourceContract, ListablePolicyContract, unittest.TestCase):
    """The bundled example yard, read as a CSV export."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def source(self):
        return TabularSource(EXAMPLE)

    def source_from_rows(self, rows):
        columns = sorted({key for row in rows for key in row})
        path = Path(self.tmp.name) / f"rows-{len(list(Path(self.tmp.name).iterdir()))}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({column: row.get(column, "") for column in columns})
        return TabularSource(path)


class TabularSourceFromSqlite(SourceContract, unittest.TestCase):
    """The same contract, over the same rows in SQLite.

    Two transports behind one adapter is exactly where a contract earns its keep: the CSV
    path and the SQLite path are different code, and nothing else compares them.
    """

    def setUp(self):
        import sqlite3

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "parts.sqlite3"
        with EXAMPLE.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        columns = list(rows[0])
        # `with sqlite3.connect(...)` commits but does not close, which leaks the handle
        # and makes the suite print a ResourceWarning that nobody should learn to ignore.
        db = sqlite3.connect(self.path)
        try:
            with db:
                db.execute(
                    f"CREATE TABLE parts ({', '.join(f'{c} TEXT' for c in columns)})")
                db.executemany(
                    f"INSERT INTO parts VALUES ({', '.join('?' for _ in columns)})",
                    [[row[c] for c in columns] for row in rows])
        finally:
            db.close()

    def source(self):
        return TabularSource(self.path)


class StaticContract(unittest.TestCase):
    """What can be checked for every registered adapter without a yard behind it."""

    def adapters(self):
        import importlib

        for kind, (module_name, class_name) in _ADAPTERS.items():
            yield kind, getattr(importlib.import_module(module_name), class_name)

    def test_every_registered_adapter_imports(self):
        self.assertEqual({kind for kind, _ in self.adapters()}, set(kinds()))

    def test_every_adapter_satisfies_the_protocol(self):
        for kind, adapter in self.adapters():
            with self.subTest(kind=kind):
                self.assertTrue(isinstance(adapter.__new__(adapter), Source))

    def test_every_adapter_declares_its_traits(self):
        for kind, adapter in self.adapters():
            with self.subTest(kind=kind):
                traits = getattr(adapter, "TRAITS", None)
                self.assertIsInstance(traits, SourceTraits,
                                      f"{adapter.__name__} declares no TRAITS")
                self.assertEqual(traits.kind, kind,
                                 "the declared kind must be the registered one")

    def test_traits_are_internally_consistent(self):
        """A change cursor needs a clock behind it, and a mapping needs a thing to map."""
        for kind, adapter in self.adapters():
            with self.subTest(kind=kind):
                traits = adapter.TRAITS
                if traits.supports_order_booking:
                    self.assertTrue(
                        traits.needs_schema_mapping,
                        "booking writes through the site's own mapping, so one is required")
                if traits.needs_smb or traits.needs_database_config:
                    self.assertFalse(
                        traits.carries_own_photos,
                        "a server-backed source keeps photographs on its share, not in a row")

    def test_signatures_match_the_protocol(self):
        """`isinstance` against a Protocol checks names only. Names are not a contract."""
        for name in ("parts", "parts_by_r_number", "listable_r_numbers", "server_now",
                     "ping"):
            expected = inspect.signature(getattr(Source, name))
            for kind, adapter in self.adapters():
                with self.subTest(kind=kind, method=name):
                    actual = inspect.signature(getattr(adapter, name))
                    self.assertEqual(
                        list(actual.parameters), list(expected.parameters),
                        f"{adapter.__name__}.{name} takes different parameters than the "
                        f"contract; a caller passing them by keyword would fail here only")

    def test_every_adapter_probes_offline(self):
        """The probe runs before every command, so it must never open a connection."""
        from coreyard.capabilities import MISSING, OFF, ON

        for kind, adapter in self.adapters():
            with self.subTest(kind=kind):
                state, detail, keys = adapter.probe("")
                self.assertIn(state, (ON, OFF, MISSING))
                self.assertTrue(str(detail).strip(), "a probe must explain its verdict")
                self.assertTrue(keys, "a probe must name the settings that decide it")

    def test_an_unregistered_kind_is_refused_by_name(self):
        from coreyard import source

        with self.assertRaises(ValueError) as caught:
            source.traits("nosuchyardsystem")
        message = str(caught.exception)
        self.assertIn("nosuchyardsystem", message)
        for kind in kinds():
            self.assertIn(kind, message, "the error should list what is registered")


class TheDatabaseAdapterForwards(unittest.TestCase):
    """The one adapter that cannot be exercised offline, checked where it can be.

    It holds no logic of its own — every query still comes from `coreyard.yms.inventory` —
    so what is worth pinning is that it passes the arguments through unchanged. Dropping
    `images_only` here would quietly publish parts with no photographs on a site that had
    turned that off.
    """

    def test_it_passes_every_argument_through(self):
        from unittest import mock

        with mock.patch("coreyard.yms.inventory.fetch_parts") as fetch:
            fetch.return_value = []
            DatabaseSource().parts(limit=7, images_only=True)
        fetch.assert_called_once_with(limit=7, images_only=True)

        with mock.patch("coreyard.yms.inventory.listable_r_numbers") as listable:
            listable.return_value = set()
            DatabaseSource().listable_r_numbers(images_only=False)
        listable.assert_called_once_with(images_only=False)

        with mock.patch("coreyard.yms.inventory.fetch_parts_by_r_number") as lookup:
            lookup.return_value = {}
            DatabaseSource().parts_by_r_number(["51", "77"])
        lookup.assert_called_once_with(["51", "77"])

    def test_its_probe_reads_credentials_and_opens_nothing(self):
        from unittest import mock

        from coreyard.capabilities import MISSING

        with mock.patch("coreyard.capabilities._text", return_value=""):
            state, detail, keys = DatabaseSource.probe("")
        self.assertEqual(state, MISSING)
        self.assertIn("SMB_HOST", detail)
        self.assertIn("SMB_HOST", keys)
