"""Order status sync: generic mechanism, site-supplied shipping policy.

The dangerous decision here is fulfillment. Shopify has no "picked but not shipped" state —
creating a fulfillment closes the fulfillment order — so fulfilling under a shipping app
that has not bought the label yet costs the buyer their tracking number, and *not*
fulfilling an order nothing else will ever touch leaves it unfulfilled forever. Which is
which depends on the site's shipping arrangements, so it is configuration.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from coreyard.orders import lifecycle
from coreyard.orders.lifecycle import SourceOrder, decide
from coreyard.orders.policy import (
    DEFAULT_POLICY,
    FulfillmentPolicy,
    OrderPolicy,
    OrderPolicyError,
    from_dict,
    load,
)

# A site whose parcels are shipped by an outside app and whose freight and pickup lines are
# not. Expressed only in that site's own tag vocabulary; CoreYard knows none of these words.
POLICY = OrderPolicy(
    tags=("invoiced",),
    reference_tag="wo-{reference}",
    note="Source work order {reference} invoiced",
    fulfillment=FulfillmentPolicy(
        groups=("ship:pickup-only", "ship:freight-299", "ship:freight-199", "ship:parcel"),
        default_group="ship:parcel",
        defer_groups=("ship:parcel",),
    ),
)

SOURCE = SourceOrder(order_reference="#1042", external_reference="55123", status="invoiced")


def order(tags=(), note="", lines=(), fulfillment_status="UNFULFILLED",
          open_fulfillment=True) -> dict:
    return {
        "id": "gid://shopify/Order/1",
        "name": "#1042",
        "tags": list(tags),
        "note": note,
        "displayFulfillmentStatus": fulfillment_status,
        "lineItems": {"nodes": [{"id": f"li{i}", "product": {"tags": list(t)}}
                                for i, t in enumerate(lines)]},
        "fulfillmentOrders": {"nodes": ([{"id": "gid://shopify/FulfillmentOrder/1",
                                          "status": "OPEN"}] if open_fulfillment else [])},
    }


class TagsAndNote(unittest.TestCase):
    def test_tags_and_the_source_reference_are_added(self):
        decision = decide(order(lines=[("ship:freight-299",)]), SOURCE, POLICY)
        self.assertEqual(decision.add_tags, ["invoiced", "wo-55123"])

    def test_tags_already_present_are_not_re_added(self):
        decision = decide(order(tags=["invoiced", "wo-55123"],
                                lines=[("ship:freight-299",)]), SOURCE, POLICY)
        self.assertEqual(decision.add_tags, [])

    def test_the_buyers_own_note_survives(self):
        """"Leave at the side door" is the part somebody actually has to read."""
        decision = decide(order(note="leave at the side door",
                                lines=[("ship:freight-299",)]), SOURCE, POLICY)
        self.assertTrue(decision.note.startswith("leave at the side door"))
        self.assertIn("Source work order 55123 invoiced", decision.note)

    def test_a_note_already_carrying_the_marker_is_left_alone(self):
        decision = decide(order(note="Source work order 55123 invoiced",
                                lines=[("ship:freight-299",)]), SOURCE, POLICY)
        self.assertEqual(decision.note, "")

    def test_an_order_needing_nothing_is_empty(self):
        decision = decide(order(tags=["invoiced", "wo-55123"],
                                note="Source work order 55123 invoiced",
                                lines=[("ship:parcel",)]), SOURCE, POLICY)
        self.assertTrue(decision.empty)


class Booked(unittest.TestCase):
    """A work order that exists but has not been invoiced yet."""

    BOOKED = SourceOrder(order_reference="#1042", external_reference="2078", status="booked")

    def test_only_the_reference_tag_is_added(self):
        decision = decide(order(lines=[("ship:freight-299",)]), self.BOOKED, POLICY)
        self.assertEqual(decision.add_tags, ["wo-2078"])

    def test_the_invoiced_tag_the_note_and_fulfillment_all_wait(self):
        decision = decide(order(lines=[("ship:freight-299",)]), self.BOOKED, POLICY)
        self.assertEqual(decision.note, "")
        self.assertEqual(decision.fulfill, [])

    def test_the_reference_tag_is_not_re_added_once_present(self):
        decision = decide(order(tags=["wo-2078"], lines=[("ship:freight-299",)]),
                          self.BOOKED, POLICY)
        self.assertTrue(decision.empty)

    def test_a_later_invoiced_row_still_adds_the_invoiced_tag_and_note(self):
        """The wo- tag is already on from the booked pass; the invoice adds the rest."""
        invoiced = SourceOrder(order_reference="#1042", external_reference="2078",
                               status="invoiced")
        decision = decide(order(tags=["wo-2078"], lines=[("ship:freight-299",)]),
                          invoiced, POLICY)
        self.assertEqual(decision.add_tags, ["invoiced"])
        self.assertIn("Source work order 2078 invoiced", decision.note)

    def test_status_is_matched_case_and_space_insensitively(self):
        spaced = SourceOrder(order_reference="#1042", external_reference="2078",
                             status="  Booked ")
        self.assertEqual(decide(order(), spaced, POLICY).add_tags, ["wo-2078"])

    def test_a_policy_with_no_reference_tag_writes_nothing_for_a_booked_order(self):
        decision = decide(order(), self.BOOKED, DEFAULT_POLICY)
        self.assertTrue(decision.empty)


class Fulfillment(unittest.TestCase):
    def test_an_order_nothing_else_will_close_is_fulfilled(self):
        decision = decide(order(lines=[("ship:freight-299",)]), SOURCE, POLICY)
        self.assertEqual(decision.fulfill, ["gid://shopify/FulfillmentOrder/1"])

    def test_an_order_an_outside_shipper_owns_is_left_open(self):
        decision = decide(order(lines=[("ship:parcel",)]), SOURCE, POLICY)
        self.assertEqual(decision.fulfill, [])
        self.assertIn("another system", decision.reason)

    def test_a_line_with_no_group_tag_falls_back_to_the_default(self):
        """Untagged parts are the common case; guessing wrong here costs a buyer tracking."""
        decision = decide(order(lines=[()]), SOURCE, POLICY)
        self.assertEqual(decision.fulfill, [])

    def test_a_mixed_order_is_left_open(self):
        decision = decide(order(lines=[("ship:freight-299",), ("ship:parcel",)]),
                          SOURCE, POLICY)
        self.assertEqual(decision.fulfill, [])

    def test_an_already_fulfilled_order_is_not_fulfilled_again(self):
        decision = decide(order(lines=[("ship:pickup-only",)],
                                fulfillment_status="FULFILLED"), SOURCE, POLICY)
        self.assertEqual(decision.fulfill, [])
        self.assertIn("already fulfilled", decision.reason)

    def test_no_open_fulfillment_order_means_nothing_to_do(self):
        decision = decide(order(lines=[("ship:pickup-only",)], open_fulfillment=False),
                          SOURCE, POLICY)
        self.assertEqual(decision.fulfill, [])

    def test_without_configured_groups_nothing_is_ever_fulfilled(self):
        """The safe default: tag and note only, until the site describes its shipping."""
        decision = decide(order(lines=[("ship:freight-299",)]), SOURCE, DEFAULT_POLICY)
        self.assertEqual(decision.fulfill, [])
        self.assertIn("not configured", decision.reason)
        self.assertEqual(decision.add_tags, ["invoiced"])

    def test_group_matching_is_case_insensitive_and_ordered(self):
        policy = FulfillmentPolicy(groups=("ship:pickup-only", "ship:parcel"),
                                   default_group="ship:parcel")
        self.assertEqual(policy.group_of(["SHIP:PICKUP-ONLY"]), "ship:pickup-only")
        self.assertEqual(policy.group_of(["ship:parcel", "ship:pickup-only"]),
                         "ship:pickup-only")
        self.assertEqual(policy.group_of([]), "ship:parcel")


class Apply(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def mutate(self, document, variables, root):
            self.calls.append((root, variables))
            return {}

    def test_each_step_is_its_own_mutation(self):
        client = self.FakeClient()
        decision = decide(order(lines=[("ship:freight-299",)]), SOURCE, POLICY)
        done = lifecycle.apply(client, decision)
        self.assertEqual(done, ["tags", "note", "fulfillment"])
        self.assertEqual([root for root, _ in client.calls],
                         ["tagsAdd", "orderUpdate", "fulfillmentCreate"])

    def test_the_customer_is_not_notified_unless_the_site_asks(self):
        client = self.FakeClient()
        decision = decide(order(lines=[("ship:freight-299",)]), SOURCE, POLICY)
        lifecycle.apply(client, decision, notify_customer=False)
        payload = client.calls[-1][1]["fulfillment"]
        self.assertFalse(payload["notifyCustomer"])


class Matching(unittest.TestCase):
    def test_the_exact_order_name_is_required(self):
        """Shopify's order search is fuzzy: "#104" also returns "#1042"."""
        class FakeClient:
            def graphql(self, query, variables=None):
                return {"orders": {"nodes": [{"id": "1", "name": "#1042"},
                                             {"id": "2", "name": "#104"}]}}

        self.assertEqual(lifecycle.find_order(FakeClient(), "#104")["id"], "2")

    def test_no_match_is_none(self):
        class FakeClient:
            def graphql(self, query, variables=None):
                return {"orders": {"nodes": []}}

        self.assertIsNone(lifecycle.find_order(FakeClient(), "#9999"))


class SourceRows(unittest.TestCase):
    def test_a_row_is_read_by_its_documented_column_names(self):
        record = SourceOrder.from_row({"order_reference": " #1042 ",
                                       "external_reference": 55123, "status": None})
        self.assertEqual(record.order_reference, "#1042")
        self.assertEqual(record.external_reference, "55123")
        self.assertEqual(record.status, "")


class PolicyLoading(unittest.TestCase):
    def _write(self, data) -> Path:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "order-sync.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_no_path_means_tag_and_note_only(self):
        self.assertIs(load(None), DEFAULT_POLICY)
        self.assertFalse(DEFAULT_POLICY.fulfillment.enabled)

    def test_the_documented_shape_loads(self):
        policy = load(self._write({
            "tags": ["invoiced"],
            "reference_tag": "wo-{reference}",
            "note": "Source work order {reference} invoiced",
            "fulfillment": {"groups": ["ship:freight-299", "ship:parcel"],
                            "default_group": "ship:parcel",
                            "defer_groups": ["ship:parcel"],
                            "notify_customer": False},
        }))
        self.assertEqual(policy.tags_for("55123"), ["invoiced", "wo-55123"])
        self.assertEqual(policy.reference_tag_for("55123"), ["wo-55123"])
        self.assertTrue(policy.fulfillment.enabled)
        self.assertTrue(policy.fulfillment.defers(["ship:parcel"]))

    def test_reference_tag_for_is_empty_without_a_reference_tag_or_a_reference(self):
        self.assertEqual(DEFAULT_POLICY.reference_tag_for("55123"), [])
        self.assertEqual(OrderPolicy(reference_tag="wo-{reference}").reference_tag_for(""), [])

    def test_an_unknown_key_is_an_error(self):
        with self.assertRaises(OrderPolicyError):
            from_dict({"fulfilment": {}})

    def test_an_unknown_fulfillment_key_is_an_error(self):
        with self.assertRaises(OrderPolicyError):
            from_dict({"fulfillment": {"group": ["x"]}})

    def test_a_configured_but_missing_file_is_an_error(self):
        with self.assertRaises(OrderPolicyError):
            load("/nonexistent/order-sync.json")


if __name__ == "__main__":
    unittest.main()
