# Repository Guidelines

## Project Structure & Module Organization

`coreyard/` implements the source-system-to-Shopify pipeline. `models.py` and
`config.py` define the neutral data/config contracts. `yms/` owns schema-driven SQL
extraction, SMB/TDS transport, photos, fitment, and the narrowly scoped order write;
`transform/` renders products, SEO, pricing, CSV rows, and pull tickets; `sink/`
contains CSV and Shopify publishers. `run_sync.py` orchestrates syncs, `state.py`
tracks fingerprints, `webhook.py` handles orders, and `schedule.py` installs timers.
Tests are in `tests/`; operational checks belong in `scripts/`. Photos are external;
`out/`, `*.sqlite3`, `.env`, `schema.json`, and generated `bin/` content are private
or generated.

## Build, Test, and Development Commands

Python 3.10+ is required; there is no build step.

```bash
./install.sh --no-deps --yes
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_neutrality.py
.venv/bin/python scripts/demo_offline.py
bin/coreyard --check
bin/coreyard --sink csv --limit 25 --dry-run
bin/coreyard schema
```

The installer creates the venv and launcher. The demo is database-free but fetches
SMB photos. `--check` performs configured live checks; the limited CSV dry run
previews output without committing sync state.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `PascalCase` classes,
and `UPPER_CASE` constants. Group imports as standard library, third-party, then
local. Preserve type hints and add short docstrings where behavior is non-obvious.
Prefer `pathlib.Path`, dataclasses, and explicit mappings. No formatter or linter is
configured; match nearby code and keep lines near 88 characters. Read new settings
through `config._get`, not `os.environ`, so `_LEGACY_KEYS` remains effective.

## Testing Guidelines

Tests use `unittest` and must run without a database, network, or `.env`. Name files
`test_<area>.py`, classes for the behavior, and methods `test_<expected_behavior>`.
Add regression coverage for identifiers, mappings, fingerprints/state diffs,
retirement, webhooks, order guards, and rendered Shopify output. Run one test with,
for example, `.venv/bin/python -m unittest tests.test_state.Diff.test_summary`.

## Commit & Pull Request Guidelines

History is short and uses concise, outcome-focused subjects such as `Reconcile
retirement against the live store`; an area prefix like `Order webhook:` is also
established. Keep commits focused and imperative. Follow `.github/PULL_REQUEST_TEMPLATE.md`:
describe behavior, list exact verification commands, and disclose schema/config or
rendered-output impact. Link issues and include redacted CSV samples or screenshots
for storefront-visible changes.

## Security, Schema, and Database Rules

Never commit secrets, customer data, generated output, or site-specific names. Source
table/column names belong only in gitignored `schema.json`; keep
`schema.example.json` generic. CI enforces vendor neutrality with
`scripts/check_neutrality.py`.

Every database path is `SELECT`-only except `coreyard/yms/orders.py`. That opt-in
work-order path requires an `order_write` mapping plus explicit enablement through
`YMS_WRITE_ORDERS=1`, `--write-orders`, or the manual `--execute` command. Keep its
single `SET XACT_ABORT ON` transaction, atomic ID allocation, unused-ID checks,
inventory floor/exactly-one-row guard, idempotence, and no-`DELETE` rule intact.
Never reuse `yms.db.query` for writes: impacket can report SQL errors as reply tokens.
Do not add another write path without equivalent review and offline guard tests.
Webhook payloads contain PII; never log bodies. Successful payloads are securely erased;
failed payloads may remain only in the owner-only queue for the bounded retry window.

## Publishing Invariants

- R# (`r_number`) is the stable SKU, handle key, photo stem, and state identity;
  `stock_number` is shared donor-vehicle data, not identity.
- `SHOPIFY_HANDLE_PREFIX` is a live storefront key. Changing it duplicates products.
- `productSet` has set semantics. Preserve omitted fields deliberately; existing
  status is read and re-sent by `shopify_write._upsert`.
- Product/image rendering feeds stored fingerprints. Changing
  `transform/shopify_product.py` or image resolution can trigger a full rewrite.
- Retirement archives rather than deletes. API sync guards mass retirement with
  `--limit` and `--max-retire-fraction`; the webhook also retires ordered parts.
- Both retirement paths record the pre-archive status in the sync state, and a part that
  returns to the yard is revived as that status. Never let a re-retire overwrite the
  memory with the archived status, and keep the memory outside the `parts` snapshot,
  which retirement clears.
- Webhook side effects require a paid order. De-duplicate by order identity as well as
  delivery ID, and preserve stage-specific retry behavior.
- Admin API 2026-07 removed older media mutations; introspect before adding one.
- Sale booking is idempotent on the stored order reference, not only webhook ID.
  Keep `order_write.line_items_taxable` false when the storefront remits tax; see
  `CLAUDE.md` before changing order or tax behavior.
