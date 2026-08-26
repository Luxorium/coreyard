# CLAUDE.md

Guidance for Claude Code when working in this repository. Read `AGENTS.md` first;
this file adds architecture and operational invariants that are easy to violate.

## Project Boundaries

CoreYard publishes salvage-yard inventory from a source SQL Server database and SMB
photo share to Shopify. The primary flow is extract -> transform -> diff -> publish.
The optional order webhook is the sole reverse write: it can book a paid storefront
sale as a work order in the source system.

This is a generic project, not a site-specific script. Never hardcode an installation's
host, credentials, business identity, storefront copy, claims, shipping policy, schema, or
handle prefix. Site configuration belongs in `.env` and in the files it names
(`STORE_PROFILE_FILE`, `STORE_WEIGHT_RULES_FILE`, `STORE_SHIPPING_POLICY_FILE`,
`STORE_ORDER_POLICY_FILE`); source table and column names belong in the local, gitignored
`schema.json`. Keep `schema.example.json` generic.

Those four files are a contract with whatever storefront sits on the other side, so their
schemas are validated offline by `coreyard/validate.py`. Adding a key means extending the
validator, or the other side cannot check it in CI.

A storefront repository may sit beside this one. CoreYard must never import it, add it to
`sys.path`, or assume a sibling checkout exists. The two sides meet at documented
configuration files, environment variables, this CLI, and Shopify itself — a sibling-path
import turns a backend refactor into a broken storefront script.

What CoreYard may claim about a part is itself configuration. The built-in profile
(`coreyard/profile.py`) states only what the yard data supports: used, OEM, off an
inventoried donor vehicle. It does not say every part was tested or inspected, because that
is not true at every yard, and a tool must not put a claim in a seller's mouth. When adding
copy to the render path, ask whether it is a fact about the data or a promise about the
business — the second belongs in the profile with a neutral default.

Vendor neutrality applies to code, tests, docs, and commit messages. Refer to "the yard
system" or "the source database" and run `scripts/check_neutrality.py` before handing
off changes. Customer-facing output must not identify any interchange-data vendor.

## Database and Configuration Safety

The source account can have more privileges than CoreYard should exercise. Every
database path must remain `SELECT`-only except `coreyard/yms/orders.py`. Do not reuse
`coreyard.yms.db.query` for a write: impacket exposes some server errors as reply
tokens, making a failed statement resemble an empty result.

The order write is disabled unless a complete `order_write` mapping is present and
writing is explicitly requested: `YMS_WRITE_ORDERS=1` or `--write-orders` for the
webhook, or `--execute` for the manual order command. Its safety properties are part
of the feature:

- one transaction under `SET XACT_ABORT ON`;
- atomic ID allocation from the source application's counter rows;
- allocated IDs checked unused before insertion;
- inventory decrement guarded by available quantity and exactly one affected row;
- idempotence on the stored storefront order reference; and
- no `DELETE` statements.

Do not weaken these guards or add another write path without equivalent observed
invariants, a dedicated executor, rollback-safe behavior, and offline regression tests.

`coreyard/config.py` parses `.env` without overriding real environment variables. Read
new configuration through `config._get`; direct `os.environ` access bypasses legacy-key
fallbacks. Configuration keys are an interface: when renaming one, extend
`config._LEGACY_KEYS`. Keep comments on their own lines because the `.env` parser does
not strip trailing comments.

Never commit `.env`, `schema.json`, customer/order payloads, `out/`, generated `bin/`,
logs, photos, or SQLite state/queue databases.

## Commands

Use the repository venv and run commands from the repository root. Python 3.10+ is
supported; there is no build, formatter, or linter.

```bash
./install.sh --no-deps --yes
.venv/bin/python -m unittest discover -s tests -v          # full offline suite
.venv/bin/python -m unittest tests.test_state.Diff.test_summary
.venv/bin/python scripts/check_neutrality.py
.venv/bin/python scripts/demo_offline.py                   # no DB; uses SMB photos
```

Configured/live operations:

```bash
bin/coreyard --check
bin/coreyard --sink csv --limit 25 --dry-run
bin/coreyard --sink api --dry-run
bin/coreyard bulk --limit 50 --workers 4
bin/coreyard images 51 --fetch
bin/coreyard schema
bin/coreyard reconcile                     # plan only
bin/coreyard reconcile --apply --activate  # WRITES status changes
bin/coreyard repair titles --dry-run
bin/coreyard repair tags --apply           # WRITES tags
bin/coreyard audit catalog                 # read-only
bin/coreyard validate                      # config schemas, offline
bin/coreyard orders status
bin/coreyard orders poll --check
bin/coreyard orders retry --id <webhook-id>
bin/coreyard orders sync-status            # plan only
.venv/bin/python -m coreyard.schedule status
```

Commands without `--dry-run` can write Shopify or sync state. `bin/coreyard orders
serve --write-orders` can also write the source database when the schema gate is
configured. Do not treat a connectivity check or DB-free demo as an offline unit test:
they use configured services.

Tests use `unittest` and must not require a database, network, `.env`, or Shopify.
Live diagnostics belong in `scripts/` or explicit CLI commands. Add regression tests
for identifiers, query mappings, rendering, state diffs, retirement, media changes,
webhook signatures/queues, order escaping, and transaction guards.

## Architecture

`Part.aliases` and `Part.vehicle` (`yms/enrich.py`) are optional enrichment and default to
empty. They feed the rendered product and therefore the fingerprint, so they stay inert
unless `COREYARD_ENRICH` is set: enabling it republishes every product it covers. Keep
`tests/test_enrich.py::FingerprintStability` passing. Render-path config gates read the
already-loaded environment and must not call `load_env` themselves, or the site's real
`.env` leaks into unit tests.

`Part` in `coreyard/models.py` is the neutral extract-to-publish contract. Downstream
code must use `Part` fields, never source columns. `StoreProfile` in
`coreyard/config.py` carries installation-specific storefront identity through
rendering and fingerprinting so customer-facing strings are not hardcoded.

```text
coreyard/yms/        schema mapping, SMB/TDS reads, images, fitment, opt-in orders
coreyard/transform/  the canonical renderer, SEO, tags, shipping, weights, CSV
coreyard/sink/       the Shopify client, CSV output, publisher, bulk, OAuth, alt text
coreyard/orders/     order pipeline + queue, poll transport, lifecycle sync, policy
coreyard/reconcile/  yard-vs-store comparison, planning, and guarded application
coreyard/repair/     rewrite catalog output an older renderer produced
coreyard/audit/      read-only listing-quality checks with configurable thresholds
coreyard/profile.py  site merchandising policy (claims, wording, metafield namespace)
coreyard/validate.py offline schema checks for the four external config files
coreyard/state.py    SQLite content/image fingerprints and incremental diff
coreyard/run_sync.py incremental sync orchestration and retirement planning
coreyard/webhook.py  HMAC verification and the HTTP receiver for the order pipeline
coreyard/schedule.py systemd-user/cron scheduling helper
```

`transform/render.py` is load-bearing. One `RenderedProduct` per part is what the fingerprint
hashes, what `productSet` serializes, and what the CSV writer serializes. The bug it exists
to prevent: the two sinks used to render their own titles and tags, and the fingerprint
covered only the CSV one, so changing the renderer that actually published left the stored
hash identical and every stale product read as "unchanged" forever. If you are about to write
a second title/tag/description builder for publishing, don't — extend the renderer.

There are four related publishing workflows, plus three that operate on what is already on
the store:

1. `run_sync --sink api` diffs against `coreyard_sync_state.sqlite3`, publishes
   additions/changes, refreshes changed photos, and retires removals.
2. `shopify_bulk.py` performs resumable concurrent initial loads, recording progress
   in `out/shopify_bulk_results.jsonl`; it does not share progress with sync state.
3. `webhook.py` optionally books, and retires parts from paid orders. `--no-retire`
   disables its Shopify retirement.
4. `run_sync --sink api --delta` (`yms/delta.py`) publishes only what moved since the
   stored cursor and retires what left scope. It is a catch-up for the hourly full sync,
   not a replacement.

The CSV sink renders previews/import files but cannot retire Shopify products.

5. `reconcile/` asks the store what it holds instead of trusting the snapshot, and closes the
   differences it can: activate listable drafts (policy-gated), revive archived parts that
   came back, retire ACTIVE products that are no longer listable (under the same fraction
   guard as the sync), publish products that reached no sales channel, and forget snapshot
   entries whose product does not exist — the last of which is what unsticks a part the sync
   believes it already published.
6. `repair/` compares live product copy against the canonical renderer and rewrites only the
   differences. It exists because the sync's diff cannot see storefront drift: when the
   renderer improves, a product whose yard data has not moved keeps its old text forever.
   It is idempotent, so it needs no progress file.
7. `audit/` reports listing quality and writes nothing.

`StoreProfile` carries both the site's identity and its policy (`catalog`, `weights`), so the
whole render path is a pure function of (part, images, store). Do not read the environment
inside it: that is what keeps the fingerprint reproducible and the tests free of any
installation's `.env`.

## Identifier and Publishing Invariants

| Meaning | `Part` field | Role |
|---|---|---|
| R# | `r_number` | unique SKU, handle key, photo stem, state identity |
| Stock # | `stock_number` | donor vehicle; shared by multiple parts |
| Interchange # | `interchange_number` | customer-facing identifier |
| Fitment key | `interchange_code` | internal application lookup only |

Never substitute Stock # for R#. Products are keyed by the stable handle
`<SHOPIFY_HANDLE_PREFIX>-<R#>`. Changing the prefix on a live store makes every item
look new and duplicates the catalog.

`productSet` has set semantics: an omitted field is reset rather than preserved.
`shopify_write._upsert` reads and re-sends an existing product's status so an
incremental update does not turn an activated product back into DRAFT. New products
default to DRAFT. Retirement zeros inventory before setting ARCHIVED and never deletes
the product, photos, or URL.

That status read-back is also why retirement has to remember. A part can return to the
yard when a work order is voided, and republishing it would re-send ARCHIVED and restore
its stock onto a product no shopper can see. `state.SyncState` keeps the pre-archive
status in a `retired` table — deliberately not in `parts`, which retirement clears — and
`publish(..., revive_status=)` applies it to archived products only, so a hand-set status
still stands. `retire(..., record_prior=)` records only a real transition; recording on an
already-archived product would overwrite the memory with ARCHIVED and strand the part.
The order webhook records through the same table and degrades to "don't remember" if the
state file is unavailable, because delisting a sold part outranks remembering it.

API-sync retirement treats a missing part cautiously. `--limit` always blocks
retirement, and removals above `--max-retire-fraction` (default 10%) require
`--force-retire`. Failed or refused retirements retain their old state entry so the next
run retries them. Keep `tests/test_retire.py` passing before changing these rules.

The delta path has its own invariants, and they are not the full path's:

- It holds a *subset* of the yard, so it must call `state.diff(..., detect_removals=False)`
  and `state.update`/`state.forget`, never `state.commit` — committing a subset deletes
  every part the run did not look at.
- Retirement there is evidence-based: a part is retired only because its row was read and
  reported out of scope. Never retire from absence on this path.
- Only a full run may write the cursor (`_delta_baseline`), and it captures the source
  server's clock *before* the extract, minus `delta.DEFAULT_OVERLAP`. Advancing it from this
  host's clock, or from after the read, silently drops rows.
- The delta resolver lists photos per part while the full resolver lists the share once.
  Both must yield identical filenames and order, or a delta run re-fingerprints everything
  it touches.

`state.product_fingerprint` covers rendered product content, ordered images, and the
fitment key. Changes to `transform/shopify_product.py` or image resolution can make the
next run rewrite the full catalog. A separate image fingerprint limits expensive media
replacement to parts whose photo set changed. The global image resolver intentionally
lists the share once per run; `--no-image-scan` skips change detection.

The 2026-07 Admin API removed older product-media mutations. Current code attaches
`FileSetInput` through `productSet`, updates alt text with `fileUpdate`, and removes
media with `fileDelete`; these require `write_files`. Introspect the live schema before
assuming a mutation or input field exists.

## Webhook and Order Invariants

The order *pipeline* — queue, worker, booking, delisting — is
`coreyard/orders/pipeline.py`, and `webhook.py` is one transport onto it. `orders/poll.py` is
the other, for hosts that cannot accept an inbound connection; `replay` is a third. All three
write to the same queue, which is why a site can run polling and webhooks at once without
booking a sale twice. Add a transport, never a second pipeline.

Registration uses `ORDERS_PAID`, and the worker independently requires
`financial_status=paid` before any side effect. The receiver verifies the raw-body HMAC
in constant time, queues the delivery, and returns promptly; slow work happens on a
worker. Shopify delivery is at-least-once, so the queue deduplicates on both delivery ID
and order ID. This also protects the transition from a legacy create subscription.

Payloads contain customer PII: never log request bodies, and write no copy of one outside
the owner-only queue. CoreYard deliberately produces no pull ticket or other document: the
source system already renders the work order in a printable form, and a second document is
both a PII copy with its own retention problem and a chance for the two to disagree about
what was sold. Successful payloads are securely erased immediately; failed payloads remain
only for the configured retry window. Booking and retirement errors are independent, and
`orders retry` reruns only failed stages. Database idempotence still comes from the stored storefront order
reference, not from webhook delivery identity.

Tax behavior is deliberate. When Shopify collects and remits the sale tax,
`order_write.line_items_taxable` must remain false so later source-system recalculation
cannot tax the same sale again. The configured customer account is for bookkeeping
consistency, not the tax exemption mechanism. Do not change order/tax behavior without
reviewing `schema.example.json`, `coreyard/yms/orders.py`, and `tests/test_orders.py`.

## Transport Constraints

SQL is reached through the `\\sql\\query` named pipe over SMB2/3, using the SMB
credentials and Windows authentication. Do not replace it with a conventional TCP SQL
driver without confirming that an installation actually exposes a listener.

Impacket's TDS behavior shapes schema expressions: cast fixed-width character fields to
trimmed `varchar`, cast bit fields to integers, and clean the literal string `NULL` in
the extraction layer. Keep those details inside `schema.json` and `inventory.py`.

Per-part photo lookups use an anchored `{R#}_*` mask so similar R#s cannot bleed into
one another. Full sync photo-change detection enumerates the directory once and groups
the results. `smbclient` receives credentials through a mode-0600 temporary auth file,
never command-line arguments.

`orders/lifecycle.py` writes back to Shopify: tags, a note, and — only where the site's
policy says nothing else will close the order — a fulfillment. That last decision is not
CoreYard's to make. Shopify has no "picked but not shipped" state, so creating a fulfillment
under a shipping app that has not bought the label yet costs the buyer their tracking number,
while never fulfilling an order nothing else touches leaves it open forever. Which applies
depends on the site's shipping arrangements, so it comes from `STORE_ORDER_POLICY_FILE` and
defaults to "tag and note only".

## Change Checklist

Before handoff, run the full unit suite, neutrality check, and `git diff --check`.
Explain any schema/config assumptions and any storefront-visible output changes. Call
out changes that affect handles, fingerprints, retirement, API fields, webhook PII,
source writes, or tax behavior. Keep `README.md`, `.env.example`, `schema.example.json`,
`AGENTS.md`, and this file synchronized when their documented interfaces change.
