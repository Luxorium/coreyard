"""Optional catalogue enrichment: aliases, donor-vehicle detail, and staying off by default.

The load-bearing test here is :class:`FingerprintStability`. Enrichment feeds the rendered
product, so if it were ever accidentally on, the next sync would re-publish the whole
catalogue. Every other behaviour in this module is subordinate to that staying deliberate.

The assertions run against the canonical renderer, which is the one both sinks publish
through: enrichment that only reached the CSV builder was enrichment nobody could see.
"""

import unittest
from decimal import Decimal

from coreyard.config import StoreProfile
from coreyard.models import VEHICLE_FIELDS, Part
from coreyard.state import product_fingerprint
from coreyard.transform.seo import build_body_html, build_tags
from coreyard.yms.enrich import CatalogEnricher
from coreyard.yms.schema import SourceSchema

STORE = StoreProfile(vendor="Yard", city="Springfield", handle_prefix="yard")

MAPPING = SourceSchema(
    select={"r_number": "i.PartId", "part_type": "pt.Name", "price": "i.Price"},
    source="dbo.Parts i",
    scope="i.Price > 0",
    order_by="i.PartId",
    part_type_aliases="SELECT TypeId AS part_type_code, Alias AS alias FROM dbo.TypeAlias",
    vehicle_details="SELECT Stock AS stock_number, Eng AS engine FROM dbo.Vin",
)

BARE = SourceSchema(
    select={"r_number": "i.PartId", "part_type": "pt.Name", "price": "i.Price"},
    source="dbo.Parts i",
    scope="i.Price > 0",
    order_by="i.PartId",
)


def a_part(**kw) -> Part:
    base = dict(r_number="51", part_type="Tail Light", price=Decimal("80.00"),
                part_type_code=7, stock_number="251026", year=2019,
                make="Ford", model="F-150")
    base.update(kw)
    return Part(**base)


class FakeConn:
    """Returns canned rows per SQL fragment, so nothing here needs a database."""

    def __init__(self, alias_rows=None, vehicle_rows=None):
        self.alias_rows = alias_rows or []
        self.vehicle_rows = vehicle_rows or []
        self.queries = []

    def rows_for(self, sql):
        self.queries.append(sql)
        return self.alias_rows if "TypeAlias" in sql else self.vehicle_rows


def patched_query(conn, sql):
    return conn.rows_for(sql)


class EnricherBase(unittest.TestCase):
    def setUp(self):
        import coreyard.yms.enrich as enrich_mod

        self._real = enrich_mod.query
        enrich_mod.query = patched_query
        self.addCleanup(lambda: setattr(enrich_mod, "query", self._real))


class Aliases(EnricherBase):
    def test_attaches_alternate_names(self):
        conn = FakeConn(alias_rows=[
            {"part_type_code": "7", "alias": "Taillamp"},
            {"part_type_code": "7", "alias": "Rear Lamp"},
            {"part_type_code": "9", "alias": "Elsewhere"},
        ])
        part = a_part()
        CatalogEnricher(conn, MAPPING).attach([part])
        self.assertEqual(part.aliases, ["Taillamp", "Rear Lamp"])

    def test_drops_an_alias_that_only_repeats_the_part_type(self):
        """A duplicate tag adds nothing but still moves the fingerprint."""
        conn = FakeConn(alias_rows=[
            {"part_type_code": "7", "alias": "tail light"},
            {"part_type_code": "7", "alias": "Taillamp"},
        ])
        part = a_part()
        CatalogEnricher(conn, MAPPING).attach([part])
        self.assertEqual(part.aliases, ["Taillamp"])

    def test_deduplicates_case_insensitively_and_skips_blanks(self):
        conn = FakeConn(alias_rows=[
            {"part_type_code": "7", "alias": "Taillamp"},
            {"part_type_code": "7", "alias": "TAILLAMP"},
            {"part_type_code": "7", "alias": "  "},
            {"part_type_code": "7", "alias": "NULL"},
        ])
        part = a_part()
        CatalogEnricher(conn, MAPPING).attach([part])
        self.assertEqual(part.aliases, ["Taillamp"])

    def test_tags_include_aliases_only_when_present(self):
        self.assertNotIn("Taillamp", build_tags(a_part()))
        self.assertIn("Taillamp", build_tags(a_part(aliases=["Taillamp"])))

    def test_drops_internal_shorthand_codes(self):
        """These tables mix real alternate names with operator codes; only the first are
        shopper vocabulary, and a tag reading 'HULK' is visible nonsense."""
        conn = FakeConn(alias_rows=[
            {"part_type_code": "7", "alias": "ABK"},
            {"part_type_code": "7", "alias": "HULK"},
            {"part_type_code": "7", "alias": "VIS"},
            {"part_type_code": "7", "alias": "ABS PARTS"},
            {"part_type_code": "7", "alias": "HEADLAMP ASSY"},
        ])
        part = a_part()
        CatalogEnricher(conn, MAPPING).attach([part])
        self.assertEqual(part.aliases, ["ABS PARTS", "HEADLAMP ASSY"])


class VehicleDetail(EnricherBase):
    def test_attaches_donor_specifics_by_stock_number(self):
        conn = FakeConn(vehicle_rows=[
            {"stock_number": "251026", "engine": "5.0L V8"},
            {"stock_number": "999999", "engine": "Other"},
        ])
        part = a_part()
        CatalogEnricher(conn, MAPPING).attach([part])
        self.assertEqual(part.vehicle, {"engine": "5.0L V8"})

    def test_drops_raw_numeric_codes_but_keeps_a_real_door_count(self):
        """'Drivetrain: 2' is a lookup key leaking onto a product page; 'Doors: 4' is a fact."""
        conn = FakeConn(vehicle_rows=[{
            "stock_number": "251026", "drivetrain": "2", "doors": "4",
            "body": "Wagon", "trim": "7",
        }])
        part = a_part()
        CatalogEnricher(conn, MAPPING).attach([part])
        self.assertEqual(part.vehicle, {"body": "Wagon", "doors": "4"})

    def test_a_donor_with_no_decoded_row_stays_empty(self):
        conn = FakeConn(vehicle_rows=[{"stock_number": "999999", "engine": "Other"}])
        part = a_part()
        CatalogEnricher(conn, MAPPING).attach([part])
        self.assertEqual(part.vehicle, {})

    def test_body_lists_detail_next_to_the_vehicle(self):
        html = build_body_html(a_part(vehicle={"engine": "5.0L V8"}), STORE)
        self.assertIn("<strong>Engine:</strong> 5.0L V8", html)
        self.assertLess(html.index("Engine:"), html.index("Condition:"))

    def test_every_declared_field_can_render(self):
        part = a_part(vehicle={f: f.upper() for f in VEHICLE_FIELDS})
        html = build_body_html(part, STORE)
        for field in VEHICLE_FIELDS:
            self.assertIn(field.upper(), html)

    def test_detail_is_html_escaped(self):
        html = build_body_html(a_part(vehicle={"trim": "<script>x</script>"}), STORE)
        self.assertNotIn("<script>", html)


class UnmappedSite(EnricherBase):
    def test_a_mapping_without_the_queries_is_a_no_op(self):
        conn = FakeConn(alias_rows=[{"part_type_code": "7", "alias": "Taillamp"}])
        part = a_part()
        CatalogEnricher(conn, BARE).attach([part])
        self.assertEqual(part.aliases, [])
        self.assertEqual(part.vehicle, {})
        self.assertEqual(conn.queries, [])


class FingerprintStability(unittest.TestCase):
    """Enrichment must be invisible until a site opts in, or every part looks changed."""

    def test_an_unenriched_part_fingerprints_exactly_as_before(self):
        part = a_part()
        baseline = product_fingerprint(part, ["51_01.jpg"], STORE)
        part.aliases = []
        part.vehicle = {}
        self.assertEqual(product_fingerprint(part, ["51_01.jpg"], STORE), baseline)

    def test_enrichment_deliberately_does_move_the_fingerprint(self):
        baseline = product_fingerprint(a_part(), ["51_01.jpg"], STORE)
        enriched = a_part(aliases=["Taillamp"], vehicle={"engine": "5.0L V8"})
        self.assertNotEqual(product_fingerprint(enriched, ["51_01.jpg"], STORE), baseline)

    def test_default_part_carries_no_enrichment(self):
        part = Part(r_number="1", part_type="Door")
        self.assertEqual(part.aliases, [])
        self.assertEqual(part.vehicle, {})


class Gate(unittest.TestCase):
    def test_checking_the_gate_does_not_pull_in_dotenv(self):
        """A render-path gate that loaded `.env` would leak the site's real settings into
        every other test in the process — which is how a pricing setting once turned itself on
        mid-suite and failed an unrelated price assertion."""
        import os

        import coreyard.yms.enrich as enrich_mod

        before = dict(os.environ)
        enrich_mod.enabled()
        self.assertEqual(dict(os.environ), before)

    def test_enabled_only_for_explicit_opt_in(self):
        import os

        import coreyard.yms.enrich as enrich_mod

        original = os.environ.get("COREYARD_ENRICH")
        self.addCleanup(
            lambda: os.environ.__setitem__("COREYARD_ENRICH", original)
            if original is not None else os.environ.pop("COREYARD_ENRICH", None)
        )
        for value, expected in [("1", True), ("true", True), ("on", True), ("yes", True),
                                ("0", False), ("false", False), ("", False)]:
            os.environ["COREYARD_ENRICH"] = value
            self.assertIs(enrich_mod.enabled(), expected, msg=value)


if __name__ == "__main__":
    unittest.main()
