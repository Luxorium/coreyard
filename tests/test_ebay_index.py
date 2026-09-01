"""The one place CoreYard reaches outside itself for pricing evidence.

Everything above this module decides *what* to ask and *which* answers count. This module
only fetches and parses, which is what makes the evidence source replaceable: a provider
backed by a marketplace's own API implements these two functions and nothing else moves.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.ebay import index


class Pages(unittest.TestCase):
    def page(self, tmp, body):
        path = Path(tmp) / "page.html"
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_truncated_cached_page_is_treated_as_absent(self):
        """A short page is an error page or a challenge; parsing it prices against nothing."""
        with TemporaryDirectory() as tmp:
            self.assertEqual(index.parse_page(self.page(tmp, "<html>rate limited</html>")), [])
            self.assertEqual(index.parse_page(Path(tmp) / "absent.html"), [])

    def test_items_are_read_as_title_and_price(self):
        body = ('<li id="item-42"><h3 title="2012 Ford F150 Tail Light">x</h3>'
                '<div class="price"><strong>$74.99</strong></div>')
        with TemporaryDirectory() as tmp:
            found = index.parse_page(self.page(tmp, body + " " * index.MIN_PAGE_BYTES))
        self.assertEqual(found, [{"item": "42", "title": "2012 Ford F150 Tail Light",
                                  "price": 74.99}])

    def test_a_price_range_takes_the_last_figure(self):
        """"$50.00 to $80.00" is an asking range; the upper bound is the comparable."""
        body = ('<li id="item-7"><h3 title="Wheel">x</h3>'
                '<div class="price"><strong>$50.00 to $80.00</strong></div>')
        with TemporaryDirectory() as tmp:
            found = index.parse_page(self.page(tmp, body + " " * index.MIN_PAGE_BYTES))
        self.assertEqual(found[0]["price"], 80.0)

    def test_markup_and_entities_are_stripped_from_the_title(self):
        body = ('<li id="item-9"><h3 title="Ford &amp; Lincoln &lt;b&gt;Lamp&lt;/b&gt;">x</h3>'
                '<div class="price"><strong>$20.00</strong></div>')
        with TemporaryDirectory() as tmp:
            found = index.parse_page(self.page(tmp, body + " " * index.MIN_PAGE_BYTES))
        self.assertEqual(found[0]["title"], "Ford & Lincoln <b>Lamp</b>")


class Percentile(unittest.TestCase):
    def test_it_interpolates_and_survives_a_single_value(self):
        self.assertEqual(index.percentile([10.0], 0.3), 10.0)
        self.assertEqual(index.percentile([10.0, 20.0], 0.5), 15.0)

    def test_the_ends_are_the_ends(self):
        self.assertEqual(index.percentile([10.0, 20.0, 30.0], 0.0), 10.0)
        self.assertEqual(index.percentile([10.0, 20.0, 30.0], 1.0), 30.0)


class Fetching(unittest.TestCase):
    """Caching is what makes a priced batch re-runnable at no cost."""

    class Session:
        def __init__(self, body=b""):
            self.body, self.calls = body, 0

        def get(self, url, **kwargs):
            self.calls += 1
            self.url = url
            return type("R", (), {"status_code": 200, "content": self.body})()

    def test_a_cached_page_is_never_requested_again(self):
        with TemporaryDirectory() as tmp:
            page = Path(tmp) / "cached.html"
            page.write_bytes(b"x" * index.MIN_PAGE_BYTES)
            session = self.Session()
            self.assertTrue(index.fetch("anything", page, session, delay=0))
            self.assertEqual(session.calls, 0)

    def test_a_short_response_is_not_cached(self):
        """Caching an error page would poison every later run for that query."""
        with TemporaryDirectory() as tmp:
            page = Path(tmp) / "new.html"
            session = self.Session(b"too short")
            self.assertFalse(index.fetch("anything", page, session, delay=0))
            self.assertFalse(page.exists())

    def test_a_full_response_is_cached(self):
        with TemporaryDirectory() as tmp:
            page = Path(tmp) / "new.html"
            session = self.Session(b"y" * index.MIN_PAGE_BYTES)
            self.assertTrue(index.fetch("tail light", page, session, delay=0))
            self.assertEqual(page.read_bytes(), b"y" * index.MIN_PAGE_BYTES)

    def test_the_query_is_escaped_into_the_url(self):
        with TemporaryDirectory() as tmp:
            session = self.Session(b"y" * index.MIN_PAGE_BYTES)
            index.fetch("2012 Ford F150", Path(tmp) / "q.html", session, delay=0)
        self.assertIn("2012+Ford+F150", session.url)
