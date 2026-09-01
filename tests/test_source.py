"""A yard's inventory, from something other than the yard.

The point of the seam is that nothing downstream can tell the implementations apart, so
these tests assert on :class:`~coreyard.models.Part` and never on a transport. If they ever
need a database, a share or a ``schema.json``, the seam has leaked.
"""

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard import source
from coreyard.source.tabular import TabularSource, _canonical

HEADER = ("r_number,part_type,part_type_code,make,model,year,price,quantity,"
          "mileage,description\n")
ROWS = ("51,TAIL LAMP,166,Ford,F150,2012,89.00,1,,Right side\n"
        "77,ENGINE ASSEMBLY,300,Ford,F150,2013,1450.00,1,88000,3.5L VIN 8\n")


def write(tmp, text, name="parts.csv"):
    path = Path(tmp) / name
    path.write_text(text, encoding="utf-8")
    return path


class Headers(unittest.TestCase):
    """An export from another system should work without being rewritten first."""

    def test_punctuation_and_case_are_ignored(self):
        for spelling in ("r_number", "R Number", "RNumber", "R-NUMBER", "r number"):
            self.assertEqual(_canonical(spelling), "r_number")

    def test_common_synonyms_reach_the_right_field(self):
        self.assertEqual(_canonical("SKU"), "r_number")
        self.assertEqual(_canonical("qty"), "quantity")
        self.assertEqual(_canonical("Odometer"), "mileage")
        self.assertEqual(_canonical("Vehicle Make"), "make")

    def test_an_unknown_column_keeps_its_name_and_is_then_ignored(self):
        self.assertEqual(_canonical("warehouse_bay"), "warehousebay")


class Reading(unittest.TestCase):
    def parts(self, text=HEADER + ROWS, **kwargs):
        with TemporaryDirectory() as tmp:
            return TabularSource(write(tmp, text), **kwargs).parts()

    def test_rows_become_parts(self):
        parts = self.parts()
        self.assertEqual([p.r_number for p in parts], ["51", "77"])
        self.assertEqual(parts[0].part_type, "TAIL LAMP")
        self.assertEqual(parts[1].price, Decimal("1450.00"))
        self.assertEqual(parts[1].mileage, 88000)
        self.assertEqual(parts[0].part_type_code, 166)

    def test_money_survives_currency_formatting(self):
        text = HEADER + '51,TAIL LAMP,166,Ford,F150,2012,"$1,089.00",1,,x\n'
        self.assertEqual(self.parts(text)[0].price, Decimal("1089.00"))

    def test_a_row_without_an_identity_is_skipped_not_guessed(self):
        text = HEADER + ",TAIL LAMP,166,Ford,F150,2012,89.00,1,,x\n"
        self.assertEqual(self.parts(text), [])

    def test_an_unpriced_part_is_not_listable(self):
        """The same guard the database source applies: no price, no listing."""
        text = HEADER + "51,TAIL LAMP,166,Ford,F150,2012,,1,,x\n"
        self.assertEqual(self.parts(text), [])

    def test_the_literal_string_null_reads_as_absent(self):
        text = HEADER + "51,TAIL LAMP,166,Ford,F150,NULL,89.00,1,NULL,x\n"
        part = self.parts(text)[0]
        self.assertIsNone(part.year)
        self.assertIsNone(part.mileage)

    def test_unknown_columns_are_ignored_rather_than_fatal(self):
        text = ("r_number,part_type,price,warehouse_bay\n"
                "51,TAIL LAMP,89.00,B12\n")
        self.assertEqual(self.parts(text)[0].r_number, "51")


class Photographs(unittest.TestCase):
    def test_photos_are_matched_on_the_anchored_r_number(self):
        """R# 51 must not collect R# 510's photographs."""
        with TemporaryDirectory() as tmp:
            images = Path(tmp) / "images"
            images.mkdir()
            for name in ("51_1.jpg", "51_2.jpg", "510_1.jpg"):
                (images / name).write_bytes(b"x")
            parts = TabularSource(write(tmp, HEADER + ROWS),
                                  images_dir=images).parts()
        self.assertEqual([Path(p).name for p in parts[0].images], ["51_1.jpg", "51_2.jpg"])

    def test_an_images_column_wins_over_the_directory(self):
        text = ("r_number,part_type,price,images\n"
                "51,TAIL LAMP,89.00,a.jpg;b.jpg\n")
        with TemporaryDirectory() as tmp:
            parts = TabularSource(write(tmp, text)).parts()
        self.assertEqual(parts[0].images, ["a.jpg", "b.jpg"])

    def test_no_directory_means_no_photos_not_an_error(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(TabularSource(write(tmp, HEADER + ROWS)).parts()[0].images, [])


class Protocol(unittest.TestCase):
    def source(self, tmp):
        return TabularSource(write(tmp, HEADER + ROWS))

    def test_lookup_by_r_number(self):
        with TemporaryDirectory() as tmp:
            found = self.source(tmp).parts_by_r_number(["77"])
        self.assertEqual(list(found), ["77"])

    def test_listable_identities(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(self.source(tmp).listable_r_numbers(), {"51", "77"})

    def test_a_limit_stops_early(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(len(self.source(tmp).parts(limit=1)), 1)

    def test_images_only_narrows_to_photographed_parts(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(self.source(tmp).parts(images_only=True), [])

    def test_it_satisfies_the_source_protocol(self):
        with TemporaryDirectory() as tmp:
            self.assertIsInstance(self.source(tmp), source.Source)


class Selection(unittest.TestCase):
    def test_the_default_is_the_database_so_nothing_changes_for_an_existing_site(self):
        from coreyard.source.database import DatabaseSource
        self.assertIsInstance(source.load("database"), DatabaseSource)

    def test_a_tabular_source_needs_a_path(self):
        with self.assertRaises(ValueError):
            source.load("tabular")

    def test_an_unknown_source_is_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            source.load("carrier-pigeon")
        self.assertIn("carrier-pigeon", str(caught.exception))


class SqliteSource(unittest.TestCase):
    def test_a_sqlite_table_reads_the_same_as_a_csv(self):
        import sqlite3
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "yard.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE parts (r_number TEXT, part_type TEXT, price TEXT)")
            connection.execute("INSERT INTO parts VALUES ('51', 'TAIL LAMP', '89.00')")
            connection.commit()
            connection.close()
            parts = TabularSource(path).parts()
        self.assertEqual(parts[0].r_number, "51")
        self.assertEqual(parts[0].price, Decimal("89.00"))


class ShippedExample(unittest.TestCase):
    def test_the_bundled_export_reads(self):
        """The file a newcomer runs first must never be broken."""
        example = Path(__file__).resolve().parent.parent / "examples" / "parts.csv"
        parts = TabularSource(example).parts()
        self.assertGreaterEqual(len(parts), 5)
        self.assertTrue(all(p.r_number and p.price for p in parts))
