"""Webhook signature checking and pull-ticket rendering, offline."""

import base64
import hashlib
import hmac
import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.transform import pull_ticket
from coreyard.webhook import EventQueue, verify

SECRET = "shpss_testsecret"
STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="90-day warranty")


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()


ORDER = {
    "name": "#1042",
    "order_number": 1042,
    "created_at": "2026-08-15T14:03:22-05:00",
    "email": "buyer@example.com",
    "financial_status": "paid",
    "note": "Leave at side door",
    "customer": {"first_name": "Dana", "last_name": "Ruiz"},
    "shipping_address": {
        "first_name": "Dana", "last_name": "Ruiz", "address1": "88 Elm St",
        "city": "Austin", "province_code": "TX", "zip": "78701",
        "country_code": "US", "phone": "512-555-0134",
    },
    "shipping_lines": [{"title": "Standard Freight"}],
    "line_items": [
        {"sku": "51", "title": "2008 Ford F150 Driver Side Left Mirror",
         "quantity": 1, "price": "89.00"},
        {"sku": "99999", "title": "Unknown Widget", "quantity": 2, "price": "10.00"},
    ],
}

PART = Part(r_number="51", part_type="Mirror", stock_number="251026", side="Left",
            interchange_number="545-01883", price=Decimal("89.00"), year=2008,
            make="Ford", model="F150", location="A-12-3", grade="B")


class Signature(unittest.TestCase):
    def test_accepts_a_genuine_signature(self):
        body = json.dumps(ORDER).encode()
        self.assertTrue(verify(body, sign(body), SECRET))

    def test_rejects_a_tampered_body(self):
        body = json.dumps(ORDER).encode()
        header = sign(body)
        self.assertFalse(verify(body + b" ", header, SECRET))

    def test_rejects_the_wrong_secret(self):
        body = b'{"a":1}'
        self.assertFalse(verify(body, sign(body, "other-secret"), SECRET))

    def test_rejects_a_missing_header(self):
        """An unsigned POST must never be treated as an order."""
        self.assertFalse(verify(b'{"a":1}', "", SECRET))

    def test_rejects_garbage_header(self):
        self.assertFalse(verify(b'{"a":1}', "not-base64!!", SECRET))


class Queue(unittest.TestCase):
    def test_duplicate_delivery_is_dropped(self):
        """Shopify delivers at least once; a resend must not reprint the order."""
        with TemporaryDirectory() as d:
            q = EventQueue(Path(d) / "q.sqlite3")
            self.assertTrue(q.add("wh-1", "orders/create", b'{"name":"#1"}'))
            self.assertFalse(q.add("wh-1", "orders/create", b'{"name":"#1"}'))
            self.assertEqual(len(q.pending()), 1)

    def test_payload_is_erased_once_handled(self):
        """An order body is a customer's name, phone and street address."""
        with TemporaryDirectory() as d:
            q = EventQueue(Path(d) / "q.sqlite3")
            q.add("wh-1", "orders/create", json.dumps(ORDER).encode())
            q.finish("wh-1", "#1042", ["51"], "/tmp/t.html")
            self.assertEqual(q.pending(), [])
            stored = q.conn.execute("SELECT payload FROM events").fetchone()[0]
            self.assertIsNone(stored)


class Ticket(unittest.TestCase):
    def setUp(self):
        self.ticket = pull_ticket.from_order(ORDER, {"51": PART}, STORE)
        self.html = pull_ticket.render(self.ticket, STORE)

    def test_carries_both_halves_of_the_job(self):
        """Bin location comes from the yard, the address from Shopify; both must appear."""
        self.assertIn("A-12-3", self.html)      # where to find it
        self.assertIn("88 Elm St", self.html)   # where it goes
        self.assertIn("#1042", self.html)

    def test_unmatched_sku_is_flagged_not_blank(self):
        """A silent empty bin column reads as 'no location', which is a different fact."""
        self.assertIn("NOT FOUND", self.html)
        self.assertIn("had no matching part", self.html)

    def test_shows_the_identifiers_a_puller_needs(self):
        self.assertIn("Stock #251026", self.html)
        self.assertIn("545-01883", self.html)

    def test_escapes_order_content(self):
        """Order fields are attacker-supplied text going into HTML."""
        hostile = dict(ORDER, note="<script>alert(1)</script>")
        html = pull_ticket.render(pull_ticket.from_order(hostile, {}, STORE), STORE)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_renders_without_a_store_profile(self):
        self.assertIn("#1042", pull_ticket.render(self.ticket))

    def test_line_quantities_and_prices_survive(self):
        self.assertEqual([l.quantity for l in self.ticket.lines], [1, 2])
        self.assertEqual(self.ticket.lines[0].sku, "51")


if __name__ == "__main__":
    unittest.main()
