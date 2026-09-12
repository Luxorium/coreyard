"""Checking a vendor upgrade against the schema surface CoreYard actually depends on.

CoreYard reads the source system through a hand-written map naming tables and columns
nobody promised to keep. Release notes list features, not column definitions, so "will this
upgrade break it?" cannot be answered by reading them — the change that breaks a mapping is
exactly the one too dull to announce. Snapshot before, compare after.

The classification is the whole value: an upgrade that widens a column is news, and one
that drops a column CoreYard selects, or adds a required column to a table it inserts into,
is a broken pipeline the next time cron fires.

Every table and column here is invented. Which of them a real installation writes to comes
from that site's own map, never from a list in this project — see `write_tables`.
"""

import tempfile
import unittest
from pathlib import Path

import scripts.schema_snapshot as snap

WRITES = {"WIDGET_LINE", "STOCK"}


def table(**columns):
    return {name: {"type": spec[0], "maxlen": spec[1],
                   "nullable": spec[2], "has_default": spec[3]}
            for name, spec in columns.items()}


def snapshot(tables, counters=(), server="a database server"):
    return {"tables": tables, "counters": list(counters), "server": server}


class WhatIsMapped(unittest.TestCase):
    def write(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "schema.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_cross_database_reference_keeps_its_database(self):
        """A catalogue can live in its own database on the same server, and
        INFORMATION_SCHEMA is per-database — so a snapshot that dropped the prefix would
        ask the wrong catalogue, find nothing, and then report every one of those tables as
        unchanged after the upgrade, having never looked at them."""
        path = self.write('{"q": "SELECT x FROM Catalogue.dbo.PartLookup pl"}')
        every, certain = snap.mapped_tables(path)
        self.assertIn("Catalogue.PartLookup", every)
        self.assertIn("Catalogue.PartLookup", certain)

    def test_a_plain_table_has_no_prefix(self):
        path = self.write('{"q": "SELECT a FROM dbo.STOCK"}')
        every, _ = snap.mapped_tables(path)
        self.assertIn("STOCK", every)

    def test_prose_in_a_comment_is_not_mistaken_for_a_table(self):
        """The map carries commentary, and "from the source database" reads exactly like a
        table reference to a regex. Reporting `dbo` and the word "closed" as missing tables
        is a check lit for a non-problem, which teaches people to skim past it."""
        path = self.write('{"_comment": "rows are read from the source database when '
                          'the work order is closed", "q": "SELECT a FROM dbo.STOCK"}')
        _, certain = snap.mapped_tables(path)
        self.assertEqual(certain, ["STOCK"])


class WhichTablesAreWritten(unittest.TestCase):
    """Derived from the site's own mapping, because the names belong to an installation.

    A hardcoded list is wrong twice: it names one vendor's schema in a vendor-neutral tree,
    and it silently gives every other installation the wrong answer.
    """

    def test_inserts_and_updates_in_the_order_write_are_found(self):
        schema = {"order_write": {"header_insert": "INSERT dbo.WIDGET (a) VALUES (1)",
                                  "line_insert": "INSERT INTO dbo.WIDGET_LINE (b) VALUES (2)",
                                  "stock_take": "UPDATE dbo.STOCK SET qty = qty - 1"}}
        self.assertEqual(snap.write_tables(schema), {"WIDGET", "WIDGET_LINE", "STOCK"})

    def test_a_site_that_does_not_book_orders_writes_nothing(self):
        self.assertEqual(snap.write_tables({"select": {"a": "b"}}), set())

    def test_every_write_path_counts_not_only_the_first(self):
        """A write path missing from this set is checked as though CoreYard only read it."""
        schema = {"order_write": {"header_insert": "INSERT dbo.WIDGET (a) VALUES (1)"},
                  "invoice_write": {"header_insert": "INSERT dbo.DOCKET (a) VALUES (1)",
                                    "order_close": "UPDATE dbo.WIDGET SET s = 'C'"},
                  "backlink_write": {"clear": "UPDATE dbo.STOCK SET link = NULL"}}
        self.assertEqual(snap.write_tables(schema), {"WIDGET", "DOCKET", "STOCK"})


class WhereTheCountersAre(unittest.TestCase):
    """Which table hands out ids is the site's to say, like every other name here."""

    def test_the_counter_tables_come_from_the_mapping(self):
        schema = {"order_write": {
            "counter_order_id": "UPDATE dbo.TALLY SET CounterValue = CounterValue + 1",
            "header_insert": "INSERT dbo.WIDGET (a) VALUES (1)"}}
        self.assertEqual(snap.counter_tables(schema), ["TALLY"])

    def test_a_second_write_path_may_use_a_second_counter(self):
        schema = {"order_write": {"counter_order_id": "UPDATE dbo.TALLY SET x = 1"},
                  "invoice_write": {"counter_invoice_id": "UPDATE dbo.TALLY SET x = 1",
                                    "counter_invoice_number": "UPDATE dbo.PER_STORE SET x = 1"}}
        self.assertEqual(snap.counter_tables(schema), ["TALLY", "PER_STORE"])

    def test_a_site_with_no_write_mapping_has_no_counters(self):
        self.assertEqual(snap.counter_tables({"select": {"a": "b"}}), [])

    def test_only_the_counter_statements_are_read(self):
        """The insert names the table the ids are *for*, which is not where they come from."""
        schema = {"order_write": {"header_insert": "INSERT dbo.WIDGET (a) VALUES (1)"}}
        self.assertEqual(snap.counter_tables(schema), [])

    def test_the_shipped_example_names_its_counters(self):
        import json

        from coreyard.config import bundled

        example = json.loads(bundled("schema.example.json").read_text(encoding="utf-8"))
        self.assertEqual(snap.counter_tables(example),
                         ["COUNTER_TABLE", "PER_STORE_COUNTER_TABLE"])


class WhatChanged(unittest.TestCase):
    BEFORE = snapshot({
        "STOCK": table(StockKey=("int", -1, False, False),
                       Remark=("varchar", 255, True, False)),
        "WIDGET_LINE": table(LineKey=("int", -1, False, False)),
    }, counters=[{"table": "AUDIT", "counter": "AuditKey", "increment": 1}])

    def diff(self, after):
        return snap.compare(self.BEFORE, after, WRITES)

    def test_an_identical_schema_says_nothing(self):
        self.assertEqual(self.diff(self.BEFORE), ([], []))

    def test_a_dropped_column_is_breaking(self):
        after = snapshot({"STOCK": table(StockKey=("int", -1, False, False)),
                          "WIDGET_LINE": self.BEFORE["tables"]["WIDGET_LINE"]},
                         counters=self.BEFORE["counters"])
        breaking, _ = self.diff(after)
        self.assertTrue(any("Remark" in b and "gone" in b for b in breaking), breaking)

    def test_a_widened_column_is_only_news(self):
        """The change advertised in the release notes that prompted this was a widening.
        Widening a column CoreYard reads cannot break a read."""
        after = snapshot({"STOCK": table(StockKey=("int", -1, False, False),
                                         Remark=("varchar", 1024, True, False)),
                          "WIDGET_LINE": self.BEFORE["tables"]["WIDGET_LINE"]},
                         counters=self.BEFORE["counters"])
        breaking, notes = self.diff(after)
        self.assertEqual(breaking, [])
        self.assertTrue(any("widened" in n for n in notes), notes)

    def test_a_narrowed_column_is_breaking(self):
        after = snapshot({"STOCK": table(StockKey=("int", -1, False, False),
                                         Remark=("varchar", 32, True, False)),
                          "WIDGET_LINE": self.BEFORE["tables"]["WIDGET_LINE"]},
                         counters=self.BEFORE["counters"])
        breaking, _ = self.diff(after)
        self.assertTrue(any("narrowed" in b for b in breaking), breaking)

    def test_a_new_required_column_breaks_where_we_insert(self):
        """An insert names its columns, so it cannot fill one it has never heard of."""
        after = snapshot({
            "STOCK": self.BEFORE["tables"]["STOCK"],
            "WIDGET_LINE": table(LineKey=("int", -1, False, False),
                                 Mandatory=("int", -1, False, False)),
        }, counters=self.BEFORE["counters"])
        breaking, _ = self.diff(after)
        self.assertTrue(any("WIDGET_LINE.Mandatory" in b for b in breaking), breaking)

    def test_the_same_addition_to_a_read_only_table_is_harmless(self):
        after = snapshot({
            "STOCK": self.BEFORE["tables"]["STOCK"],
            "WIDGET_LINE": self.BEFORE["tables"]["WIDGET_LINE"],
            "REFERENCE": table(Code=("int", -1, False, False)),
        }, counters=self.BEFORE["counters"])
        breaking, notes = self.diff(after)
        self.assertEqual(breaking, [])
        self.assertTrue(any("REFERENCE" in n for n in notes))

    def test_a_new_optional_column_is_only_news(self):
        after = snapshot({
            "STOCK": table(StockKey=("int", -1, False, False),
                           Remark=("varchar", 255, True, False),
                           Extra=("varchar", 64, True, False)),
            "WIDGET_LINE": self.BEFORE["tables"]["WIDGET_LINE"],
        }, counters=self.BEFORE["counters"])
        breaking, notes = self.diff(after)
        self.assertEqual(breaking, [])
        self.assertTrue(any("Extra" in n for n in notes))

    def test_a_lost_counter_is_breaking(self):
        """The order write allocates ids from these rows. Losing one does not fail loudly —
        it books under an id the source application also intends to issue."""
        after = snapshot(self.BEFORE["tables"], counters=[])
        breaking, _ = self.diff(after)
        self.assertTrue(any("counter" in b for b in breaking), breaking)

    def test_a_changed_counter_increment_is_breaking(self):
        after = snapshot(self.BEFORE["tables"],
                         counters=[{"table": "AUDIT", "counter": "AuditKey",
                                    "increment": 5}])
        breaking, _ = self.diff(after)
        self.assertTrue(any("increment 1 -> 5" in b for b in breaking), breaking)

    def test_the_server_version_is_reported_but_is_not_a_failure(self):
        after = snapshot(self.BEFORE["tables"], counters=self.BEFORE["counters"],
                         server="a newer database server")
        breaking, notes = self.diff(after)
        self.assertEqual(breaking, [])
        self.assertTrue(any("server version" in n for n in notes))
