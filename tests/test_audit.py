"""Catalog audit: generic checks, site-configurable thresholds, no site-specific rules."""

import unittest

from coreyard.audit.catalog import evaluate, scan
from coreyard.config import StoreProfile
from coreyard.profile import AuditPolicy


def product(**kw) -> dict:
    base = dict(handle="coreyard-51", title="2010 Honda Civic Alternator Generator",
                status="ACTIVE", product_type="Alternator Generator", vendor="Test Yard",
                tags=["Honda", "2010 Honda Civic", "Used OEM"],
                description_html="<p>" + "x" * 200 + "</p>",
                seo_title="2010 Honda Civic Alternator", seo_description="In stock.",
                inventory=1, media_count=2, first_media_alt="A photo", sku="51",
                price="120.00", weight=18.0,
                metafields={"fitment": '[{"make":"Honda"}]', "condition": "Used"})
    base.update(kw)
    return base


class Checks(unittest.TestCase):
    def test_a_healthy_product_produces_nothing(self):
        self.assertTrue(evaluate([product()]).clean)

    def test_missing_sku(self):
        self.assertEqual(evaluate([product(sku="")]).count("missing sku"), 1)

    def test_duplicate_sku(self):
        """Two listings for one physical part means one gets sold and never pulled."""
        report = evaluate([product(), product(handle="coreyard-52")])
        self.assertEqual(report.count("duplicate sku"), 1)

    def test_zero_price(self):
        self.assertEqual(evaluate([product(price="0")]).count("price is zero"), 1)

    def test_price_floor_is_configurable(self):
        cheap = [product(price="3.00")]
        self.assertEqual(evaluate(cheap).count("price below the floor"), 0)
        strict = AuditPolicy(min_price=5)
        self.assertEqual(evaluate(cheap, strict).count("price below the floor"), 1)

    def test_no_photos(self):
        self.assertEqual(evaluate([product(media_count=0)]).count("no photos"), 1)

    def test_missing_alt_text_is_only_reported_when_there_is_a_photo(self):
        self.assertEqual(evaluate([product(first_media_alt="")])
                         .count("photo missing alt text"), 1)
        report = evaluate([product(media_count=0, first_media_alt="")])
        self.assertEqual(report.count("photo missing alt text"), 0)

    def test_thin_description(self):
        self.assertEqual(evaluate([product(description_html="<p>x</p>")])
                         .count("description thin or empty"), 1)

    def test_missing_seo_metadata(self):
        report = evaluate([product(seo_title="", seo_description="")])
        self.assertEqual(report.count("no SEO title"), 1)
        self.assertEqual(report.count("no SEO description"), 1)

    def test_missing_product_type_and_vendor(self):
        report = evaluate([product(product_type="", vendor="")])
        self.assertEqual(report.count("no product type"), 1)
        self.assertEqual(report.count("no vendor"), 1)

    def test_too_few_tags(self):
        self.assertEqual(evaluate([product(tags=["Honda"])]).count("too few tags"), 1)

    def test_missing_shipping_weight(self):
        self.assertEqual(evaluate([product(weight=0)]).count("no shipping weight"), 1)

    def test_active_with_no_stock(self):
        self.assertEqual(evaluate([product(inventory=0)])
                         .count("active with zero inventory"), 1)
        self.assertEqual(evaluate([product(inventory=0, status="DRAFT")])
                         .count("active with zero inventory"), 0)

    def test_suspicious_titles(self):
        self.assertEqual(evaluate([product(title="Example product")])
                         .count("suspicious title"), 1)
        self.assertEqual(evaluate([product(title="Short")]).count("suspicious title"), 1)

    def test_title_over_the_limit(self):
        self.assertEqual(evaluate([product(title="x" * 300)])
                         .count("title over the limit"), 1)


class ConfiguredChecks(unittest.TestCase):
    """Two checks that only mean something once the site configured the thing."""

    TAGS = {"ship:free", "ship:freight-299", "ship:pickup-only"}

    def test_a_product_with_no_shipping_tag_is_reported(self):
        report = evaluate([product(tags=["Honda"])], shipping_tags=self.TAGS)
        self.assertEqual(report.count("no shipping classification"), 1)

    def test_a_classified_product_is_not(self):
        report = evaluate([product(tags=["Honda", "ship:free"])], shipping_tags=self.TAGS)
        self.assertEqual(report.count("no shipping classification"), 0)

    def test_without_a_configured_policy_the_check_stays_silent(self):
        """A site that classifies nothing must not have its whole catalogue flagged."""
        report = evaluate([product(tags=["Honda"])])
        self.assertEqual(report.count("no shipping classification"), 0)

    def test_a_product_with_no_structured_fitment_is_reported(self):
        report = evaluate([product(metafields={})], namespace="abm")
        self.assertEqual(report.count("no structured fitment"), 1)

    def test_a_product_with_fitment_is_not(self):
        report = evaluate([product(metafields={"fitment": "[]"})], namespace="abm")
        self.assertEqual(report.count("no structured fitment"), 0)

    def test_without_a_namespace_the_fitment_check_stays_silent(self):
        self.assertEqual(evaluate([product(metafields={})])
                         .count("no structured fitment"), 0)


class Policy(unittest.TestCase):
    def test_every_requirement_can_be_switched_off(self):
        relaxed = AuditPolicy(require_weight=False, require_seo=False, require_photos=False,
                              require_alt_text=False, require_vendor=False,
                              require_product_type=False, require_sku=False,
                              min_description_chars=0, min_tags=0, min_title_chars=0,
                              flag_active_zero_inventory=False)
        bare = product(sku="", weight=0, seo_title="", seo_description="", media_count=0,
                       first_media_alt="", vendor="", product_type="", tags=[],
                       description_html="", inventory=0, title="A part")
        self.assertTrue(evaluate([bare], relaxed).clean)

    def test_the_suspicious_word_list_is_the_sites_own(self):
        policy = AuditPolicy(suspicious_title_words=("clearance",), min_title_chars=1)
        self.assertEqual(evaluate([product(title="Clearance alternator")], policy)
                         .count("suspicious title"), 1)
        self.assertEqual(evaluate([product(title="Example product")], policy)
                         .count("suspicious title"), 0)

    def test_findings_are_ordered_by_how_many_products_they_hit(self):
        report = evaluate([product(weight=0, sku=""), product(handle="coreyard-52", weight=0)])
        self.assertEqual(report.ordered()[0][0], "no shipping weight")


class Scan(unittest.TestCase):
    class FakeClient:
        def __init__(self, nodes):
            self._nodes = nodes

        def paginate(self, query, connection, variables=None, page_size=250, max_pages=None):
            yield from self._nodes

    def _node(self, handle):
        return {
            "id": "gid://shopify/Product/1", "handle": handle, "title": "A part",
            "status": "ACTIVE", "productType": "Alternator", "vendor": "Test Yard",
            "tags": ["Honda"], "descriptionHtml": "<p>x</p>",
            "seo": {"title": "t", "description": "d"}, "totalInventory": 1,
            "metafields": {"nodes": [{"key": "fitment", "value": "[]"}]},
            "mediaCount": {"count": 1}, "media": {"nodes": [{"alt": "a"}]},
            "variants": {"nodes": [{"id": "v", "sku": "51", "price": "120.00",
                                    "inventoryItem": {"measurement": {
                                        "weight": {"value": 18.0}}}}]},
        }

    STORE = StoreProfile(vendor="Test Yard", handle_prefix="coreyard")

    def test_by_default_only_our_own_products_are_audited(self):
        client = self.FakeClient([self._node("coreyard-51"), self._node("t-shirt")])
        self.assertEqual(len(scan(client, self.STORE)), 1)

    def test_the_whole_store_can_be_audited_when_asked(self):
        client = self.FakeClient([self._node("coreyard-51"), self._node("t-shirt")])
        self.assertEqual(len(scan(client, self.STORE, ours_only=False)), 2)

    def test_the_flat_shape_is_what_evaluate_expects(self):
        client = self.FakeClient([self._node("coreyard-51")])
        rows = scan(client, self.STORE)
        self.assertEqual(rows[0]["sku"], "51")
        self.assertEqual(rows[0]["weight"], 18.0)
        self.assertEqual(rows[0]["first_media_alt"], "a")
        self.assertEqual(rows[0]["metafields"], {"fitment": "[]"})
        evaluate(rows)          # must not raise on a real-shaped row


if __name__ == "__main__":
    unittest.main()
