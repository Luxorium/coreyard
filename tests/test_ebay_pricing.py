"""Marketplace markup applies to the part; included shipping is added afterwards."""

import unittest
from dataclasses import replace
from decimal import Decimal

from coreyard.ebay import auto_prices, daily
from coreyard.ebay.pricing import PricePolicy, quote
from coreyard.transform.shipping import ShippingPolicy, ShippingGroup
from tests.test_ebay_daily import STORE, part, row

POLICY = PricePolicy(Decimal('15'), True)


def store(price='14.99', group='GROUND'):
    return replace(STORE, shipping=ShippingPolicy((
        ShippingGroup(group, 'ship:test', price=price, default=True),)))


class Prices(unittest.TestCase):
    def test_pickup_markup_does_not_include_shipping(self):
        self.assertEqual(quote(part(price='100'), store(None), 'pickup', POLICY)['new_price'], '115.00')

    def test_free_shipping_is_added_after_markup(self):
        result = quote(part(price='100'), store(), 'free', POLICY)
        self.assertEqual(result['new_price'], '129.99')
        self.assertEqual(result['shipping_included'], '14.99')

    def test_zero_shipping_rate_is_valid(self):
        self.assertEqual(quote(part(price='75'), store('0.00'), 'free', POLICY)['new_price'], '86.25')

    def test_cents_round_half_up(self):
        self.assertEqual(quote(part(price='0.10'), store(), 'pickup', POLICY)['new_price'], '0.12')

    def test_large_and_medium_freight_are_charged_by_policy(self):
        for group, target, rate in [('A', 'large', '299.99'), ('B', 'medium', '199.99')]:
            result = quote(part(price='1000'), store(rate, group), 'free', POLICY,
                           {'A': 'large', 'B': 'medium'})
            self.assertEqual(result['new_price'], '1150.00')
            self.assertEqual(result['shipping_included'], '0.00')
            self.assertEqual(result['shipping_policy'], target)

    def test_unknown_shipping_and_unshippable_free_listing_are_held(self):
        for mode, settings in [('unknown', store()), ('free', store(None))]:
            with self.assertRaises(ValueError):
                quote(part(), settings, mode, POLICY)

    def test_missing_or_nonpositive_source_price_is_refused(self):
        for price in (None, '0', '-10'):
            with self.assertRaises(ValueError):
                quote(part(price=price), store(), 'pickup', POLICY)

    def test_default_policy_preserves_source_price(self):
        self.assertEqual(quote(part(price='67.50'), STORE, None, PricePolicy())['new_price'], '67.50')

    def test_legacy_daily_cannot_restore_unmarked_prices_under_shipping_policy(self):
        ready, held = daily.source_price_decisions([row()], {'L1': part()},
                                                   store=store(), policy=POLICY)
        self.assertEqual(ready, [])
        self.assertIn('shipping policy is unknown', held[0]['reason'])

    def test_a_matching_price_but_wrong_freight_policy_still_changes(self):
        record = {**row(price='$92.00'), 'shipping_mode': 'paid', 'shipping_policy': 'medium'}
        ready, held = auto_prices.plan([record], [part()], store('299.99', 'A'),
                                       POLICY, {'A': 'large'})
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]['shipping_policy'], 'large')

    def test_filter_values_are_structured_not_interpolated(self):
        result = auto_prices.filter_query('{"field":["{part_type}","{policy_id}"]}',
                                          part_type=123, policy_id='a"b')
        self.assertEqual(result, {'field': [123, 'a"b']})


class PriceWorker(unittest.TestCase):
    def test_freight_policy_and_price_are_verified_before_existing_listing_revision(self):
        import json
        import tempfile
        from copy import deepcopy
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as directory:
            current = [{**row(price='$80.00'), 'shipping_mode': 'paid',
                        'shipping_policy': 'medium'}]
            client = Mock()
            client.portal.filters = {'freight_policy_by_group': '{"A":"large"}'}

            def update(changes, ids, tab):
                self.assertEqual(tab, 'listed')
                self.assertEqual(ids, ['L1'])
                for change in changes:
                    if change['field'] == 'fixed_price':
                        current[0]['price'] = change['value']
                    elif change['field'] == 'shipping_policy':
                        current[0]['shipping_policy'] = change['value']
                    else:
                        self.fail('unexpected write surface')
                return ''

            client.bulk_update.side_effect = update
            args = SimpleNamespace(batch_size=1, cap=1, types_per_run=1,
                state=str(Path(directory)/'state.json'), out=str(Path(directory)/'report.json'),
                portal=None, part_type='166', apply=True, revise_listed=True)
            def submit(options):
                self.assertEqual(options.tab, 'listed')
                self.assertFalse(options.list_as_new)
                self.assertEqual(current[0]['price'], '92.00')
                self.assertEqual(current[0]['shipping_policy'], 'large')
                pending = json.loads(Path(args.state).read_text())['pending']
                self.assertIn('L1', pending)
                return 0

            with patch.object(auto_prices, 'load_env'), \
                 patch.object(auto_prices, 'load_store', return_value=store('299.99','A')), \
                 patch.object(auto_prices, 'load_portal'), \
                 patch.object(auto_prices, 'PortalClient', return_value=client), \
                 patch.object(auto_prices.PricePolicy, 'configured', return_value=POLICY), \
                 patch('coreyard.yms.inventory.fetch_parts', return_value=[part()]), \
                 patch.object(auto_prices, 'collect', side_effect=lambda *a: deepcopy(current)), \
                 patch('coreyard.ebay.cli.push', side_effect=submit) as push:
                self.assertEqual(auto_prices.run(args), 0)
                self.assertEqual(push.call_count, 1)
            report = json.loads(Path(args.out).read_text())
            self.assertEqual(report['saved'][0]['new_price'], '92.00')
            self.assertEqual(json.loads(Path(args.state).read_text())['pending'], {})
