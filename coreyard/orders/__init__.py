"""Storefront orders: the pipeline, its transports, and lifecycle synchronization.

    pipeline.py    what happens to a paid order — work order, delisting, queue
    poll.py        pull transport, for installations that cannot accept inbound webhooks
    lifecycle.py   push the source system's order status back onto the Shopify order
    policy.py      the site's order-handling policy, supplied as configuration

The webhook receiver in :mod:`coreyard.webhook` is the push transport for the same pipeline.
"""

from coreyard.orders.pipeline import (
    EventQueue,
    Handled,
    OrderWorker,
    QueuedEvent,
    drain,
    order_is_paid,
)

__all__ = [
    "EventQueue",
    "Handled",
    "OrderWorker",
    "QueuedEvent",
    "drain",
    "order_is_paid",
]
