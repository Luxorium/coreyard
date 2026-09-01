import unittest
from decimal import Decimal
from pathlib import Path

from coreyard.ebay.client import PortalClient, PortalError, SessionExpired
from coreyard.ebay.engines import (ApplyGuard, build_overrides, plan_prices,
                                   plan_titles, title_decisions)
from coreyard.ebay.portal import PortalConfigError, load
from coreyard.config import REPO_ROOT


class Response:
    def __init__(self, value=None, status=200, headers=None, text=""):
        self.value = value
        self.status_code = status
        self.headers = headers or {}
        self.text = text

    def json(self):
        if isinstance(self.value, ValueError):
            raise self.value
        return self.value


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cookies = {}
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class ProtocolMap(unittest.TestCase):
    def setUp(self):
        self.portal = load(REPO_ROOT / "portal.example.json")

    def test_example_map_is_complete_and_normalizes_rows(self):
        row = self.portal.normalize_row({"listing_id": 7, "title": "Engine",
                                         "interchange_number": 123,
                                         "price": "$10.00", "part_type": 300})
        self.assertEqual(row["listing_id"], "7")
        self.assertEqual(row["interchange_number"], "123")
        self.assertEqual(row["part_type"], "300")

    def test_unknown_tab_is_rejected(self):
        with self.assertRaises(PortalConfigError):
            self.portal.status("everything")

    def test_grid_uses_mapped_protocol_and_returns_neutral_rows(self):
        session = Session([Response({"pages": 1, "page": 1, "records": 1,
                                     "session": "token", "rows": [{
                                         "listing_id": 7, "interchange_number": "300-A",
                                         "title": "Engine", "price": "$10.00",
                                         "part_type": 300,
                                     }]})])
        client = PortalClient(self.portal, cookies={"session": "x"}, session=session,
                              delay=0)
        page = client.grid(tab="unlisted")
        self.assertEqual(page["rows"][0]["listing_id"], "7")
        self.assertEqual(client.session_id, "token")
        self.assertEqual(session.calls[0][2]["params"]["status"], "unlisted")

    def test_expired_session_is_a_specific_error(self):
        session = Session([Response(status=403)])
        client = PortalClient(self.portal, cookies={"session": "x"}, session=session,
                              delay=0)
        with self.assertRaises(SessionExpired):
            client.grid()

    def test_bulk_update_never_accepts_inverted_update_all(self):
        client = PortalClient(self.portal, cookies={"session": "x"},
                              session=Session([]), delay=0)
        with self.assertRaises(PortalError):
            client.bulk_update([], ["7"], session_id="token", update_all=True)

    def test_bulk_update_requires_explicit_ids(self):
        client = PortalClient(self.portal, cookies={"session": "x"},
                              session=Session([]), delay=0)
        with self.assertRaises(PortalError):
            client.bulk_update([], [], session_id="token")


LISTINGS = [
    {"listing_id": "1", "interchange_number": "300-A", "title": "Old",
     "price": "$100.00"},
    {"listing_id": "2", "interchange_number": "300-A", "title": "Old",
     "price": "$100.00"},
]


class EnginePlanning(unittest.TestCase):
    def test_title_decision_retargets_the_whole_current_interchange_group(self):
        proposals = [{"interchange": "300-A", "new_title": "A Better Engine Title"}]
        decisions, held = title_decisions(LISTINGS, proposals)
        self.assertFalse(held)
        self.assertEqual(decisions[0]["listing_ids"], ["1", "2"])

    def test_unchanged_titles_remain_shopify_decisions_but_not_portal_writes(self):
        rows = [dict(LISTINGS[0], title="A Better Engine Title")]
        proposals = [{"interchange": "300-A", "new_title": "A Better Engine Title"}]
        self.assertEqual(len(title_decisions(rows, proposals)[0]), 1)
        self.assertEqual(plan_titles(rows, proposals)[0], [])

    def test_price_planner_holds_condition_disclosures(self):
        research = [{"listing_id": "1", "suggested_price": 249.99,
                     "confidence": "high", "comp_count": 5,
                     "needs_disclosure": True}]
        ready, held = plan_prices(LISTINGS[:1], research)
        self.assertFalse(ready)
        self.assertIn("disclosed", held[0]["reason"])

    def test_price_planner_keeps_safe_researched_price(self):
        research = [{"listing_id": "1", "suggested_price": 249.99,
                     "confidence": "medium", "comp_count": 3}]
        ready, held = plan_prices(LISTINGS[:1], research)
        self.assertFalse(held)
        self.assertEqual(ready[0]["new_price"], "249.99")

    def test_shopify_override_is_keyed_by_r_number_not_listing_id(self):
        value = build_overrides(
            {"1": {"r_number": "42"}},
            [{"listing_ids": ["1"], "new_title": "SEO Engine"}],
            [{"listing_id": "1", "new_price": "249.99"}],
        )
        self.assertEqual(value["parts"]["42"],
                         {"title": "SEO Engine", "price": "249.99"})

    def test_missing_r_number_refuses_shopify_override(self):
        with self.assertRaises(ApplyGuard):
            build_overrides({}, [{"listing_ids": ["1"], "new_title": "SEO"}], [])


if __name__ == "__main__":
    unittest.main()

