"""DATA-04: what the storefront is allowed to say, and what it must never invent.

The renderer turns a row of yard data into copy a shopper reads and a search engine indexes.
Two things have to hold at once: the copy must carry every material qualifier the data
supports, and it must never carry one the data does not — no invented fitment, no condition
nobody recorded, no promise the yard never made. A catalogue is thousands of small claims
made automatically, and the expensive ones are the claims nobody noticed being made.

Untrusted text is the other half. The yard's free-text fields are typed by people at a
counter, and they reach a shopper in three shapes: escaped inside the description HTML,
reduced to words in a title, and verbatim as a metadata value — a tag, a product type, a
meta description. Only the first two protected themselves.
"""

import re
import unittest
from decimal import Decimal

from coreyard.config import StoreProfile
from coreyard.models import Part
from coreyard.transform import seo
from coreyard.transform.render import render

STORE = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="90-day warranty")

HOSTILE = {
    "markup": "<script>alert(1)</script>",
    "attribute break": 'Hon"da',
    "angle brackets": "a < b > c",
    "entity": "Tom &amp; Jerry",
    "control characters": "line" + chr(0) + "one" + chr(10) + "line two",
    "unicode": "Citroën Berlingo — 1.6 HDi",
    "emoji": "Door \U0001F6AA Handle",
}


def part(**kw) -> Part:
    fields = dict(r_number="51", part_type="Alternator", price=Decimal("120.00"),
                  quantity=1, year=2010, make="Honda", model="Civic")
    fields.update(kw)
    return Part(**fields)


class UntrustedTextIsNeverMarkup(unittest.TestCase):
    """A value is not markup, so it is made to stop being markup rather than escaped —
    escaping a tag would publish ``&amp;`` as part of its name."""

    def rendered(self, **kw):
        return render(part(**kw), [], STORE)

    def test_the_description_escapes_what_the_yard_typed(self):
        # Prose, because a counter note that does not read like a sentence is dropped from
        # the body rather than published — a separate guard, and not the one under test.
        typed = "Fits several trims <script>alert(1)</script> see photos for detail"
        body = self.rendered(description=typed).description_html
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)

    def test_no_metadata_value_carries_markup(self):
        for name, text in HOSTILE.items():
            with self.subTest(case=name):
                product = self.rendered(part_type=text, model=text, description=text)
                values = [product.product_type, product.seo_description, product.seo_title,
                          product.title, *product.tags]
                for value in values:
                    self.assertNotIn("<", value)
                    self.assertNotIn(">", value)

    def test_no_metadata_value_can_break_out_of_an_attribute(self):
        """A meta description lands inside ``content="..."`` in the storefront's own head."""
        product = self.rendered(model=HOSTILE["attribute break"])
        self.assertNotIn('"', product.seo_description)
        for tag in product.tags:
            self.assertNotIn('"', tag)

    def test_no_value_carries_a_control_character_or_a_newline(self):
        product = self.rendered(part_type=HOSTILE["control characters"])
        for value in (product.product_type, product.seo_description, *product.tags):
            self.assertIsNone(re.search(r"[\x00-\x1f\x7f]", value), value)

    def test_unicode_survives_intact(self):
        """Sanitizing is not transliterating: a French model name is data, not a threat."""
        product = self.rendered(model="Citroën Berlingo")
        self.assertIn("Citroën", " ".join(product.tags))

    def test_ordinary_text_is_left_exactly_as_it_was(self):
        for text in ("Alternator", "Bumper Reinf, Front", "Door & Glass", "4x4 Transfer Case"):
            with self.subTest(text=text):
                self.assertEqual(seo.plain_text(text), text)

    def test_the_spacing_of_an_existing_tag_is_not_quietly_tidied(self):
        """"Bumper Reinf, Front" has published as a tag with two spaces since the beginning.
        Tidying it here would move the fingerprint of every part whose type carries a comma —
        7,518 of this catalogue's 27,202 — which is a decision someone makes on purpose with
        the repair commands, not a side effect of hardening a value."""
        tags = render(part(part_type="Bumper Reinf, Front"), [], STORE).tags
        self.assertIn("Bumper Reinf  Front", tags)

    def test_an_ampersand_stays_an_ampersand_in_a_value(self):
        """Escaping it here would publish "Door &amp;amp; Glass" as a product type."""
        self.assertEqual(seo.plain_text("Door & Glass"), "Door & Glass")

    def test_a_tag_is_one_line_of_text(self):
        for text in HOSTILE.values():
            with self.subTest(text=text):
                for tag in render(part(model=text), [], STORE).tags:
                    self.assertEqual(tag, tag.strip())
                    self.assertNotIn("\n", tag)


class NothingIsInvented(unittest.TestCase):
    """Everything the copy claims has to come from the row or from the site's own profile."""

    def test_an_unknown_part_type_is_published_as_it_was_typed(self):
        """No guess about what a code means. The wording gap is reported by `part-types`."""
        self.assertIn("ZZQ", render(part(part_type="ZZQ"), [], STORE).title.upper())

    def test_a_part_with_no_year_makes_no_year_claim(self):
        product = render(part(year=None), [], STORE)
        self.assertNotIn("None", product.title)
        self.assertNotIn("None", product.seo_description)

    def test_a_part_with_no_side_makes_no_side_claim(self):
        product = render(part(side=None), [], STORE)
        for word in ("Driver Side", "Passenger Side"):
            self.assertNotIn(word, product.title)

    def test_a_side_that_is_recorded_is_carried_into_the_title(self):
        """The other half of the same rule: a material qualifier must not be dropped."""
        self.assertIn("Left", render(part(side="LEFT"), [], STORE).title)

    def test_no_condition_is_claimed_beyond_used_oem(self):
        """The built-in profile states only what the yard data supports; "tested" and
        "inspected" are promises about a business, not facts about a row."""
        body = render(part(), [], STORE).description_html.lower()
        for claim in ("tested", "inspected", "refurbished", "guaranteed to work"):
            self.assertNotIn(claim, body)

    def test_a_site_with_no_warranty_promises_none(self):
        bare = StoreProfile(vendor="Test Yard", city="Testville, TX", warranty="")
        self.assertNotIn("Warranty", render(part(), [], bare).description_html)

    def test_a_grade_that_is_absent_is_not_described(self):
        self.assertNotIn("Grade", render(part(grade=None), [], STORE).description_html)


class NoInferenceService(unittest.TestCase):
    """CoreYard uses no LLM and no inference service. It is a stated property of the
    product, so it is checked rather than remembered: the copy is a template over a row."""

    PROVIDERS = ("openai", "anthropic", "cohere", "mistralai", "google.generativeai",
                 "transformers", "torch", "langchain", "llama_cpp", "ollama")

    def test_no_module_imports_an_inference_client(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "coreyard"
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for provider in self.PROVIDERS:
                with self.subTest(module=path.name, provider=provider):
                    self.assertNotIn(f"import {provider}", source)
                    self.assertNotIn(f"from {provider}", source)

    def test_the_dependency_list_carries_none_either(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        requirements = (root / "requirements.txt").read_text(encoding="utf-8").lower()
        for provider in self.PROVIDERS:
            with self.subTest(provider=provider):
                self.assertNotIn(provider, requirements)

    def test_the_same_row_renders_the_same_copy_every_time(self):
        """Determinism is the observable difference. A generated description would not be
        identical on the second call, and the fingerprint would move on its own."""
        first = render(part(), [], STORE)
        second = render(part(), [], STORE)
        self.assertEqual(first.fingerprint(), second.fingerprint())
        self.assertEqual(first.description_html, second.description_html)


if __name__ == "__main__":
    unittest.main()
