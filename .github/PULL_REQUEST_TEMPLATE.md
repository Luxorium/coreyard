## What this changes

<!-- One or two sentences. What behaviour is different afterwards? -->

## How it was verified

<!-- Commands you actually ran, e.g. python -m unittest discover -t . -s tests -->

- [ ] `python -m unittest discover -t . -s tests`
- [ ] `python scripts/check_neutrality.py`

## Impact checklist

- [ ] No vendor product names or real table/column names added to the repo
- [ ] No source write outside `coreyard/yms/orders.py`; its transaction guards remain intact
- [ ] Webhook/customer PII is not logged, committed, or retained without a bounded reason
- [ ] Tests still run offline (no DB, no network, no `.env`)
- [ ] If rendered output changed (titles/descriptions/handles), noted below

<!-- Changing shopify_product.py or the image resolver invalidates every sync fingerprint,
     making the next incremental run rewrite the whole catalogue. Say so if that applies. -->
