"""DATA-02: an extract is authoritative, or it is an error. Never quietly both.

Retirement is the one destructive thing CoreYard does, and it is driven by absence: a part
that was in the catalogue and is not in this extract has been sold. That inference is only
safe while "not in the extract" means "not in the yard". Every way an extract can come back
short *without* saying so is therefore a way to archive a shelf full of parts that are still
on it.

The guards on the retirement side are tested in `test_retire.py` — partial views, scopes,
the fraction limit, the empty extract. This file covers the other half, which is that the
extract layer never manufactures an empty or short result in the first place: an error is
raised, not swallowed; a policy that narrows the extract reaches every source; and a row
CoreYard cannot understand is dropped rather than turned into a half-built part.
"""

import unittest
from decimal import Decimal
from unittest import mock

from coreyard.models import Part
from coreyard.yms import inventory


class Fake:
    """A source that records what the dispatch handed it."""

    TRAITS = None

    def __init__(self, parts=()):
        self._parts, self.calls = list(parts), []

    def parts(self, limit=None, images_only=None):
        self.calls.append({"limit": limit, "images_only": images_only})
        return list(self._parts)

    def listable_r_numbers(self, images_only=None):
        self.calls.append({"images_only": images_only})
        return {p.r_number for p in self._parts}


class ATransportErrorIsNotAnEmptyYard(unittest.TestCase):
    """The failure that would be indistinguishable from a sold-out yard.

    `fetch_parts` deliberately has no `except` around its paging loop. That is the property
    under test: an exception must reach the caller, because a caller that receives `[]`
    cannot tell a broken pipe from a yard with nothing in it, and one of those two readings
    archives the catalogue.
    """

    def test_a_failed_connection_raises_rather_than_returning_nothing(self):
        with mock.patch("coreyard.yms.inventory.connect",
                        side_effect=OSError("named pipe refused")):
            with mock.patch("coreyard.yms.inventory.schema.load"):
                with mock.patch("coreyard.yms.inventory._elsewhere", return_value=None):
                    with self.assertRaises(OSError):
                        inventory.fetch_parts()

    def test_a_failed_query_raises_rather_than_returning_nothing(self):
        with mock.patch("coreyard.yms.inventory.connect"):
            with mock.patch("coreyard.yms.inventory.schema.load"):
                with mock.patch("coreyard.yms.inventory._elsewhere", return_value=None):
                    with mock.patch("coreyard.yms.inventory.query",
                                    side_effect=RuntimeError("TDS parse error")):
                        with self.assertRaises(RuntimeError):
                            inventory.fetch_parts()

    def test_the_identity_view_fails_the_same_way(self):
        """Reconciliation reads this one, and it is the cheaper path to the same damage."""
        with mock.patch("coreyard.yms.inventory.connect",
                        side_effect=OSError("named pipe refused")):
            with mock.patch("coreyard.yms.inventory.schema.load"):
                with mock.patch("coreyard.yms.inventory._elsewhere", return_value=None):
                    with self.assertRaises(OSError):
                        inventory.listable_r_numbers()


class ThePhotoPolicyReachesEverySource(unittest.TestCase):
    """The narrowing policy has to be resolved before the hand-off, not after it.

    `fetch_parts` defaults `images_only` from this installation's photo policy — but it did
    so on the database path, *below* the branch that hands off to any other source. A yard
    with `STORE_REQUIRE_IMAGES=true` and a CSV export therefore published every part with no
    photographs at all, and reconciliation agreed with it, so nothing ever looked wrong.
    """

    def test_the_policy_is_resolved_before_the_hand_off(self):
        fake = Fake()
        with mock.patch("coreyard.yms.inventory._elsewhere", return_value=fake):
            with mock.patch("coreyard.yms.inventory.photos_required", return_value=True):
                inventory.fetch_parts()
        self.assertEqual(fake.calls[0]["images_only"], True,
                         "the source was handed None and never learned the policy")

    def test_the_identity_view_resolves_it_too(self):
        fake = Fake()
        with mock.patch("coreyard.yms.inventory._elsewhere", return_value=fake):
            with mock.patch("coreyard.yms.inventory.photos_required", return_value=True):
                inventory.listable_r_numbers()
        self.assertEqual(fake.calls[0]["images_only"], True)

    def test_an_explicit_argument_still_wins(self):
        """`--no-image-scan` and the audit pass one deliberately; policy must not override."""
        fake = Fake()
        with mock.patch("coreyard.yms.inventory._elsewhere", return_value=fake):
            with mock.patch("coreyard.yms.inventory.photos_required", return_value=True):
                inventory.fetch_parts(images_only=False)
        self.assertEqual(fake.calls[0]["images_only"], False)

    def test_a_source_with_no_mapping_does_not_have_one_demanded_of_it(self):
        """`photos_required` used to load `schema.json` to answer, and raise without one."""
        from coreyard.source import SourceTraits

        traits = SourceTraits(kind="tabular", needs_schema_mapping=False,
                              needs_database_config=False, needs_smb=False,
                              carries_own_photos=True, supports_delta=False,
                              supports_fitment=False, supports_order_booking=False)
        with mock.patch("coreyard.config.source_traits", return_value=traits):
            with mock.patch("coreyard.yms.inventory.require_images", return_value=True):
                with mock.patch("coreyard.yms.inventory.schema.load",
                                side_effect=AssertionError("loaded a mapping it should not")):
                    self.assertTrue(inventory.photos_required())


class AMalformedRowIsDroppedNotGuessed(unittest.TestCase):
    """Coercion happens once, at the boundary, and never invents a value.

    impacket returns everything as a string and renders SQL NULL as the literal `'NULL'`.
    A row that cannot be read has to become nothing, rather than a part with a zero price or
    an empty identity, because both of those are publishable-looking.
    """

    def test_null_arrives_as_a_string_and_is_treated_as_absent(self):
        part = inventory.row_to_part(
            {"r_number": "51", "part_type": "Engine", "price": "100",
             "grade": "NULL", "mileage": "NULL", "model": "NULL"})
        self.assertIsNone(part.grade)
        self.assertIsNone(part.mileage)
        self.assertIsNone(part.model)

    def test_an_unreadable_price_is_absent_rather_than_zero(self):
        """Zero would be a real price. Absent is refused by `is_listable`; zero is a sale."""
        part = inventory.row_to_part(
            {"r_number": "51", "part_type": "Engine", "price": "not a number"})
        self.assertIsNone(part.price)
        self.assertFalse(part.is_listable())

    def test_an_unreadable_integer_is_absent_rather_than_zero(self):
        part = inventory.row_to_part(
            {"r_number": "51", "part_type": "Engine", "price": "1", "year": "n/a"})
        self.assertIsNone(part.year)

    def test_a_row_with_no_identity_cannot_be_listed(self):
        for identity in ("", "   ", "NULL"):
            with self.subTest(identity=identity):
                part = inventory.row_to_part(
                    {"r_number": identity, "part_type": "Engine", "price": "100"})
                self.assertFalse(part.is_listable())

    def test_a_negative_or_zero_price_cannot_be_listed(self):
        for price in ("0", "-1", "-0.01"):
            with self.subTest(price=price):
                part = inventory.row_to_part(
                    {"r_number": "51", "part_type": "Engine", "price": price})
                self.assertEqual(part.price, Decimal(price))
                self.assertFalse(part.is_listable(), f"{price} reached the storefront")

    def test_a_part_type_is_never_left_blank(self):
        """It reaches the title. An empty one renders a listing that describes nothing."""
        part = inventory.row_to_part({"r_number": "51", "price": "100"})
        self.assertTrue(part.part_type.strip())


class TheListablePolicyIsOneDefinition(unittest.TestCase):
    """`Part.is_listable` is the final guard, and it is the same object for every source."""

    def test_it_requires_an_identity_and_a_positive_price(self):
        self.assertTrue(Part(r_number="51", part_type="Engine",
                             price=Decimal("0.01")).is_listable())
        self.assertFalse(Part(r_number="", part_type="Engine",
                              price=Decimal("100")).is_listable())
        self.assertFalse(Part(r_number="51", part_type="Engine", price=None).is_listable())
