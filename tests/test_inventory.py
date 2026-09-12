"""Extract mapping: identifiers, value coercion, and schema-driven query building.

These tests never read the site's real ``schema.json`` — the repository ships no vendor
schema, so the fixture below stands in for one.
"""

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from coreyard.yms import schema
from coreyard.yms.inventory import (
    FETCH_PAGE_SIZE,
    _clean_note,
    fetch_parts,
    row_to_part,
    source_inventory_count,
)

FIXTURE = {
    "select": {
        "r_number": "i.PartId",
        "part_type": "RTRIM(CAST(pt.Name AS varchar(20)))",
        "price": "i.Price",
        "stock_number": "RTRIM(CAST(i.DonorId AS varchar(10)))",
    },
    "source": "dbo.Parts i LEFT JOIN dbo.PartTypes pt ON i.TypeId = pt.TypeId",
    "scope": "i.Price > 0 AND i.Qty > 0",
    "images_filter": "i.HasPhotos = 1",
    "order_by": "i.PartId",
}


class SchemaDrivenQuery(unittest.TestCase):
    def test_query_is_built_from_the_supplied_mapping(self):
        sql = schema.SourceSchema.from_dict(FIXTURE).build_query()
        self.assertIn("i.PartId AS r_number", sql)
        self.assertIn("FROM dbo.Parts i LEFT JOIN dbo.PartTypes pt", sql)
        self.assertIn("WHERE i.Price > 0 AND i.Qty > 0", sql)
        self.assertIn("ORDER BY i.PartId", sql)

    def test_limit_and_images_filter_are_optional(self):
        s = schema.SourceSchema.from_dict(FIXTURE)
        self.assertIn("TOP 25", s.build_query(limit=25))
        self.assertNotIn("TOP", s.build_query())
        self.assertIn("i.HasPhotos = 1", s.build_query(images_only=True))
        self.assertNotIn("HasPhotos", s.build_query(images_only=False))

    def test_missing_required_field_is_reported_clearly(self):
        broken = {**FIXTURE, "select": {"r_number": "i.PartId"}}
        with self.assertRaises(schema.SchemaError) as ctx:
            schema.SourceSchema.from_dict(broken)
        self.assertIn("part_type", str(ctx.exception))

    def test_missing_section_is_reported_clearly(self):
        with self.assertRaises(schema.SchemaError):
            schema.SourceSchema.from_dict({"select": FIXTURE["select"]})



class IdentifierMapping(unittest.TestCase):
    def test_identifiers_map_to_their_actual_meanings(self):
        part = row_to_part({
            "r_number": "51",
            "stock_number": "251026",
            "interchange_number": "545-01883",
            "interchange_code": "01883",
            "part_type_code": "545",
            "part_type": "Anti-lock Brake Pts",
            "price": "125.00",
            "quantity": "1",
        })
        self.assertEqual(part.r_number, "51")
        self.assertEqual(part.stock_number, "251026")
        self.assertEqual(part.interchange_number, "545-01883")
        self.assertEqual(part.interchange_code, "01883")
        self.assertEqual(part.price, Decimal("125.00"))

    def test_side_is_kept_off_the_part_type(self):
        part = row_to_part({"r_number": "9", "part_type": "Side View Mirror",
                            "left_right": "L", "price": "20.00"})
        self.assertEqual(part.side, "Left")
        self.assertEqual(part.part_type, "Side View Mirror")

    def test_null_arrives_as_a_string_and_is_coerced_away(self):
        part = row_to_part({"r_number": "9", "part_type": "Alternator",
                            "price": "10.00", "mileage": "NULL", "grade": "NULL"})
        self.assertIsNone(part.mileage)
        self.assertIsNone(part.grade)


class NoteCleaning(unittest.TestCase):
    def test_legacy_header_is_stripped(self):
        self.assertEqual(
            _clean_note("Old ID 6565||ENG||2060||10000.001,,,(3.0L, VIN C, 4th digit)"),
            "3.0L, VIN C, 4th digit",
        )

    def test_plain_text_passes_through(self):
        self.assertEqual(_clean_note("Good condition, tested"), "Good condition, tested")

    def test_empty_and_null_become_none(self):
        self.assertIsNone(_clean_note(""))
        self.assertIsNone(_clean_note("NULL"))


def _row(r_number: int) -> dict:
    return {"r_number": str(r_number), "part_type": "Engine", "price": "10.00"}


class PagedFetch(unittest.TestCase):
    @patch("coreyard.yms.inventory.query")
    @patch("coreyard.yms.inventory.connect")
    @patch("coreyard.yms.inventory.schema.load")
    def test_full_extract_reads_bounded_pages(self, load, connect, run_query):
        mapping = load.return_value
        mapping.build_page_query.side_effect = lambda size, after, **_: f"page:{after}:{size}"
        connect.return_value.__enter__.return_value = MagicMock()
        # Distinct R#s, because that is what keyset paging reads: the next page starts
        # *after* the last row's identifier, so a page of repeats could not occur — and a
        # repeated R# is now held back as ambiguous, which would make this test about
        # something else entirely.
        run_query.side_effect = [
            [_row(i) for i in range(1, FETCH_PAGE_SIZE + 1)],
            [_row(FETCH_PAGE_SIZE + 1)],
        ]

        parts = fetch_parts()

        self.assertEqual(len(parts), FETCH_PAGE_SIZE + 1)
        self.assertEqual(
            [call.args[1] for call in run_query.call_args_list],
            [f"page:None:{FETCH_PAGE_SIZE}", f"page:{FETCH_PAGE_SIZE}:{FETCH_PAGE_SIZE}"],
        )
        self.assertEqual(connect.call_count, 2)
    @patch("coreyard.yms.inventory.query")
    @patch("coreyard.yms.inventory.connect")
    @patch("coreyard.yms.inventory.schema.load")
    def test_limit_caps_the_last_page(self, load, connect, run_query):
        mapping = load.return_value
        mapping.build_page_query.side_effect = lambda size, after, **_: f"page:{after}:{size}"
        connect.return_value.__enter__.return_value = MagicMock()
        run_query.side_effect = [
            [_row(i) for i in range(1, FETCH_PAGE_SIZE + 1)],
            [_row(i) for i in range(FETCH_PAGE_SIZE + 1, FETCH_PAGE_SIZE + 251)],
        ]

        limit = FETCH_PAGE_SIZE + 250
        self.assertEqual(len(fetch_parts(limit=limit)), limit)
        self.assertEqual(
            [call.args[1] for call in run_query.call_args_list],
            [f"page:None:{FETCH_PAGE_SIZE}", f"page:{FETCH_PAGE_SIZE}:250"],
        )
        self.assertEqual(connect.call_count, 2)


class InventoryCount(unittest.TestCase):
    @patch("coreyard.yms.inventory.query")
    @patch("coreyard.yms.inventory.connect")
    @patch("coreyard.yms.inventory.schema.load")
    def test_exact_count_uses_one_aggregate_without_the_photo_gate(
        self, load, connect, run_query
    ):
        mapping = load.return_value
        mapping.build_inventory_count_query.return_value = "count sql"
        connect.return_value.__enter__.return_value = MagicMock()
        run_query.return_value = [{"part_count": "27163"}]

        self.assertEqual(source_inventory_count(), 27163)

        mapping.build_inventory_count_query.assert_called_once_with(images_only=False)
        run_query.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class DependenciesAreDeclaredOnce(unittest.TestCase):
    """The two direct dependencies are written down twice, for two different installers.

    `pyproject.toml` is what a wheel installs; `requirements.txt` is what `install.sh` reads.
    Two lists of the same thing drift in one direction — the one nobody installs from gets
    forgotten — and the failure lands on whoever installs the way the maintainer does not.
    """

    def test_pyproject_and_requirements_declare_the_same_set(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, "scripts/dependencies.py", "--check"],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_qualification_set_is_recorded(self):
        """A release qualified against "whatever the index served" is not qualified."""
        from pathlib import Path

        lock = (Path(__file__).resolve().parent.parent / "requirements-lock.txt")
        recorded = [line for line in lock.read_text(encoding="utf-8").splitlines()
                    if line and not line.startswith("#")]
        self.assertGreater(len(recorded), 2, "a closure, not just the direct dependencies")
        self.assertTrue(all("==" in line for line in recorded))
        for direct in ("impacket==", "requests=="):
            with self.subTest(dependency=direct):
                self.assertTrue(any(line.startswith(direct) for line in recorded))


class SettingsAreDocumentedFromTheCode(unittest.TestCase):
    """UX-02: the type, default and precedence of every setting, generated rather than kept.

    A configuration reference written by hand is wrong within two releases — a key is
    renamed, a default moves, an option is added for one installation and never written down
    — and a reference that is wrong is worse than none, because it is believed.
    """

    def test_the_document_matches_the_code(self):
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, "scripts/settings.py", "--check"],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_it_covers_the_scripts_as_well_as_the_package(self):
        """`healthcheck.py` reads its own notifier command, and a reference that stopped at
        the package would omit a key `.env.example` documents."""
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "coreyard_settings_script", root / "scripts" / "settings.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn("COREYARD_ALERT_CMD", module.collect())

    def test_a_typed_accessor_gives_the_setting_its_type(self):
        """A key read through `_duration` is a duration whether or not anyone wrote that
        down, which is the whole reason this is generated."""
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "coreyard_settings_script2", root / "scripts" / "settings.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        settings = module.collect()
        self.assertIn("duration", settings["COREYARD_ALERT_REPEAT"].kind)
        self.assertEqual(settings["STORE_REQUIRE_IMAGES"].kind, "switch")


class TheScriptsRunOnEveryAdvertisedPython(unittest.TestCase):
    """The helper scripts are part of the supported surface, and CI runs them.

    Both of these failed in CI while passing here, for the two reasons a script written in a
    developer's environment usually does: it imported the package, which the hygiene job does
    not install, and it imported `tomllib`, which 3.10 does not have. The badge says 3.10.
    """

    def script(self, name: str):
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(f"coreyard_script_{name}",
                                                      root / "scripts" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_dependency_check_reads_pyproject_without_tomllib(self):
        module = self.script("dependencies")
        with_parser = module.declared_in_pyproject()
        module.tomllib = None
        self.assertEqual(module.declared_in_pyproject(), with_parser)

    def test_every_script_ci_runs_works_without_the_package_installed(self):
        """`python scripts/x.py` puts `scripts/` on the path, not the repository root."""
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for command in (["scripts/inventory.py", "--check"],
                        ["scripts/settings.py", "--check"],
                        ["scripts/dependencies.py", "--check"],
                        ["scripts/ledger.py", "--summary"],
                        ["scripts/check_neutrality.py"]):
            with self.subTest(script=command[0]):
                result = subprocess.run(
                    [sys.executable, *command], cwd=root, capture_output=True, text=True,
                    env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
