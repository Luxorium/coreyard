# Repository Guidelines

## Agent Workflow

These instructions apply to Codex using GPT-6 Astra (`gpt-6-astra`) and to other coding
agents. Select the model in Codex configuration; this file does not select it. Keep the
configured reasoning effort unless the user requests a change. `CLAUDE.md` supplements
this file with operational context; this section owns the shared working conventions.

Treat requests to change or fix something as instructions to implement and verify it.
Carry authorized work through to completion, making routine choices from repository and
conversation context. Incorporate follow-up corrections and questions while continuing
the original task unless the user replaces it. Preserve unrelated working-tree edits.

Follow explicit user instructions over repository or skill guidance, within system and
developer constraints. Reuse authorization already given for the same action and scope.
If a missing decision blocks work, complete independent preparation first, then explain
the concrete decision needed. When a file causes a pause, link it and quote the relevant
instruction. The publishing, database, and portal safeguards below still apply.

Use concise progress updates and report the outcome, verification, and any remaining
blocker plainly. Batch independent read-only checks when useful; use subagents only when
the user or governing session instructions request them. For documentation-only edits,
review the diff, check references, and run the neutrality check and `git diff --check`.
For code or behavior changes, run the full offline unit suite and those same checks.
Repeat or broaden checks only for a change, failure, or unresolved concern. Add tests
when they establish meaningful behavior, including the regression coverage below.

## Project Structure & Module Organization

`coreyard/` implements the source-system-to-Shopify pipeline. `models.py`, `config.py` and
`profile.py` define the neutral data/config/policy contracts. `yms/` owns schema-driven SQL
extraction, SMB/TDS transport, photos (the part folder and, for parts the yard never
photographed, the donor-vehicle folder behind them), fitment, changed-since deltas
(`delta.py`), optional catalogue enrichment (`enrich.py`), and the narrowly scoped order
write. `transform/` holds
the canonical renderer (`render.py`) plus the SEO engine, tag ownership, weights, money formatting,
CSV framing; `sink/` contains the one Shopify client and the CSV and API
publishers. `orders/` owns the order pipeline and its transports, `reconcile/` compares the
store with the yard, `repair/` rewrites output an older renderer produced, and `audit/`
reports listing quality. `ebay/` is the optional
listing-portal channel: a portal map, its client, guarded writes, and `daily.py`, the
unattended pass that composes them. `yms/part_types.py` reports every part
type the yard can inventory and where the renderer's wording runs out.
`overrides.py` carries the reviewed decisions that channel hands back to the renderer. `run_sync.py` orchestrates syncs,
`state.py` tracks fingerprints, `webhook.py` is the webhook transport, and `schedule.py`
installs timers. Tests are in `tests/`; operational checks belong in `scripts/`. Photos are
external; `out/`, `*.sqlite3`, `.env`, `schema.json`, `portal.json`, `notes/`, and generated
`bin/` content are private or generated.

A storefront repository may live beside this one. CoreYard must never import it, assume it,
or reach for a sibling path: site policy arrives as configuration files whose paths are named
in `.env` (`STORE_PROFILE_FILE`, `STORE_WEIGHT_RULES_FILE`, `STORE_SHIPPING_POLICY_FILE`,
`STORE_ORDER_POLICY_FILE`, and the reviewed decisions in `STORE_CATALOG_OVERRIDES_FILE`), and
everything else goes through Shopify or this CLI. Those schemas are checkable offline with
`bin/coreyard validate`, which is what lets a storefront repository verify its own files in CI
without importing anything from here.

## Build, Test, and Development Commands

Python 3.10+ is required; there is no build step.

```bash
./install.sh --no-deps --yes
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/check_neutrality.py
.venv/bin/python scripts/demo_offline.py
bin/coreyard --help                 # every command; routing lives in coreyard/cli.py
bin/coreyard doctor                 # installation + liveness, read-only
bin/coreyard status                 # what the pipeline believes, read-only
bin/coreyard sync --dry-run
bin/coreyard sync --sink csv --limit 25 --dry-run
bin/coreyard reconcile              # plan only; writes nothing
bin/coreyard repair titles --dry-run
bin/coreyard audit catalog          # read-only
bin/coreyard validate               # external config against its schemas, offline
bin/coreyard schema
```

The installer creates the venv and launcher; the launcher is one `exec` onto `python -m
coreyard` and holds no command list. The demo is database-free but fetches SMB photos.
`doctor` performs configured live checks; the limited CSV dry run previews output without
committing sync state.

Commands are mounted in `coreyard/cli.py` by calling each module's `add_arguments(parser)`
— the same function that module's own `main(argv)` uses, so `coreyard sync` and the legacy
`python -m coreyard.run_sync` cannot disagree about a flag. Keep the legacy module entry
points working: schedulers name them.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions/modules, `PascalCase` classes,
and `UPPER_CASE` constants. Group imports as standard library, third-party, then
local. Preserve type hints and add short docstrings where behavior is non-obvious.
Prefer `pathlib.Path`, dataclasses, and explicit mappings. No formatter or linter is
configured; match nearby code and keep lines near 88 characters. Read new settings
through `config._get`, not `os.environ`, so `_LEGACY_KEYS` remains effective.

## Testing Guidelines

Tests use `unittest` and must run without a database, network, or `.env`. That last one is
easy to break indirectly: a config gate on the render path that calls `load_env` itself will
pull the installation's real `.env` into the whole suite and change unrelated assertions.
Read config through `_get` and let the caller load the environment. Name files
`test_<area>.py`, classes for the behavior, and methods `test_<expected_behavior>`.
Add regression coverage for identifiers, mappings, fingerprints/state diffs, retirement,
revival, webhooks, order guards, tag ownership, the listable policy, weight rules,
reconciliation, repair, order polling, rendered Shopify output, and — for the listing-portal
channel — cap guards, title validation, exact source-price mapping, aspect
derivation, and which listing ids a write resolves to. Run one test with, for
example, `.venv/bin/python -m unittest tests.test_state.Diff.test_summary`.

Anything that talks to Shopify takes a client so a fake can be passed in, and every planning
decision that could empty a catalogue — retirement fractions, reconciliation buckets, repair
diffs — lives in a pure function that a test can call directly. The same rule covers the
listing portal: its client is injectable, its planning (`ebay/engines.py`) is pure, and no
test may reach a portal.

CoreYard runs no model and calls no LLM. Titles come from the renderer and prices come
unchanged from the source database, so every command is reproducible and a test that wanted
to stub inference would have nothing to stub.

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
`schema.example.json` generic. The listing portal's routes, field ids and action ids follow
the same rule in gitignored `portal.json`, with `portal.example.json` generic and the
vendor's captured reference material confined to the ignored `notes/`. Portal session
cookies are owner-only and never passed on a command line. CI enforces vendor neutrality
with `scripts/check_neutrality.py`.

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
- **There is one renderer.** `transform/render.py` produces the `RenderedProduct` that the
  fingerprint hashes and that both sinks serialize. Never add a second place that renders a
  title, tag, description or SEO field for publishing: fingerprinting one renderer while
  publishing another is the bug that made stale products read as "unchanged" forever.
- Anything shopper-visible belongs on `RenderedProduct`, so it moves the fingerprint. That
  includes the shipping classification tag and the structured metafields: a theme renders
  them, so they are catalogue content, not decoration. Anything CoreYard does not own —
  product status, channel publication, another system's tags — deliberately does not,
  because those are read from the live product instead.
- A namespace CoreYard generates into is *owned*, not preserved. `transform/tags.merge`
  replaces a stale `ship:` tag rather than leaving two classifications on one product;
  everything else namespaced is still carried through untouched.
- Shipping classification happens during the publish that creates the product. A later pass
  is a window in which a product is live, buyable and wearing the wrong shipping.
- `productSet` has set semantics. Preserve omitted fields deliberately; existing status is
  read and re-sent by `shopify_write._upsert`, and existing external tags are merged back by
  `transform/tags.merge`. Sending only generated tags deletes the storefront's own.
- Product/image rendering feeds stored fingerprints. Changing `transform/render.py`,
  `transform/seo.py`, the weight table, the site profile, or image resolution can trigger a
  full rewrite.
- "Listable" has one definition in `yms/inventory.py`: the mapping's `scope`, the positive
  source-price guard on `Part`, and the optional `photos_required` policy
  (`STORE_REQUIRE_IMAGES`). `fetch_parts` and `listable_r_numbers` both apply it, so sync,
  reconciliation, bulk publishing and the audit cannot disagree; a second condition anywhere
  lets them publish and archive the same part in turn.
- Retirement archives rather than deletes. API sync guards mass retirement with
  `--limit` and `--max-retire-fraction`; the webhook also retires ordered parts.
- **Anything that narrows a run blocks retirement**: `--limit`, `--r-number`, and any scope
  other than `inventory`. Absence is the only evidence retirement has, and a run that looked
  at a slice of the yard has none. `run_sync._partial_view` is the single place that decides.
- A narrowed or scoped run records only what it published and never replaces the snapshot.
  Committing would mark the parts it skipped as up to date and strand their changes forever.
- Sync scopes are projections of the one `RenderedProduct`, never a second rendering. Media
  is rebuilt only for a part whose photo *set* moved, never for a copy change.
- Photo refresh stages and attaches before deleting the superseded media, so a failure part
  way through leaves a stale photograph rather than a product with none.
- A part with no photographs of its own may inherit its donor vehicle's, when `schema.json`
  maps `donor_images`. A part that *has* its own never consults that folder, donor references
  are namespaced (`run_sync._DONOR_PREFIX`) because the two folders share a key space, and
  both resolvers go through `run_sync._photo_view` so the full and delta paths spell an
  unchanged photo identically. Resolver and publisher must trim to the same
  `images.donor_photo_limit`, or the part republishes forever. A configured limit of zero
  selects all donor frames in both paths.
- Donor frames are uploaded once and referenced by every part off that donor
  (`state.donor_media`, written only after `fileCreate` returned an id). The body note and the
  alt text say the picture is of the car, not the part: that is a fact about the data, so it
  travels on `Part.uses_donor_photos` through the renderer.
- A long run banks its progress (`run_sync.CHECKPOINT_EVERY`), so a scheduled job stopped by
  its timeout keeps what it published instead of starting the same backlog again.
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
- The listing-portal channel has three write surfaces, and they stay three commands:
  saving in the portal changes nothing a shopper sees, pushing makes it live, and delisting
  is irreversible on eBay — a relist mints a new item id and loses the watchers and ranking.
  The gap between the first two is the review window for portal changes.
- Every portal write is a dry run without `--apply`, and its cap counts listings rather than
  plan entries and refuses *before* the first write, so a refused batch writes nothing.
- `ebay auto-titles` owns the bounded unlisted-title queue. It uses the shared renderer's
  80-character budget, rechecks live unlisted membership before saving, and records success
  only after read-back verification. It never changes prices, exports Shopify overrides,
  or submits listings. Its checkpoint is private generated state in `out/`.
- A listing is matched to a part from grid data alone, by the R# in the portal's own title or
  by (donor stock number, part-type code), then — inside a candidate set only — by the grid's
  interchange number and by the side the title names. Each refuses rather than guesses: a
  listing that resolves to no single part is left alone, because repricing the wrong part is
  worse than touching nothing.
- `EBAY_PORTAL_USER`/`EBAY_PORTAL_PASSWORD` are a sign-in of last resort, used only when the
  cookie sources yield nothing and only if `portal.json` maps `auth.login_fields`; the client
  re-signs at most once per expired request. Never print, log, or put a password in an error.
- The source database is the base-price authority. Shopify uses its unchanged positive
  price. eBay's optional configured markup and included shipping are computed only in
  `ebay/pricing.py`: markup applies to the part, then free-shipping listings add the
  configured Shopify rate. Pickup and freight do not include shipping in the item price;
  freight selects the mapped shipping policy. Never mark up an already-marked-up price.
- `ebay auto-prices --apply --revise-listed` may revise existing live listings through
  the guarded push surface after price/policy read-back. It never lists unlisted inventory.
  Failed or interrupted revisions retain private pending state for retry.
- Reviewed titles reach Shopify only as `STORE_CATALOG_OVERRIDES_FILE`, read by the one
  renderer and keyed by R#. Never put prices in that file or patch a product price directly:
  the next sync must restore the source amount.
