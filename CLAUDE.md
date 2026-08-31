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

The listing portal behind `coreyard ebay` follows the same rule one level further out.
CoreYard speaks no portal's protocol natively: routes, parameter names, tab statuses, form
field ids and bulk-action ids all come from the gitignored `portal.json`, exactly as source
column names come from `schema.json`. Keep `portal.example.json` generic, and keep the
vendor's own reference material (captured request shapes, category metadata, filter-field
tables) in the ignored `notes/` directory rather than in the tree.

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
bin/coreyard doctor                        # installation + liveness, read-only
bin/coreyard status                        # what the pipeline believes, read-only
bin/coreyard sync --dry-run
bin/coreyard sync                          # WRITES Shopify + state (sink defaults to api)
bin/coreyard sync --deep                   # ...and pages the live store first
bin/coreyard sync delta                    # cheap catch-up
bin/coreyard sync inventory|photos|catalog # one kind of change only
bin/coreyard sync --r-number 51 --dry-run
bin/coreyard sync --sink csv --limit 25 --dry-run
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
bin/coreyard schedule status
bin/coreyard ebay pull --tab unlisted      # read-only portal download
bin/coreyard ebay engine-plan --titles ... --prices ...   # plan only
bin/coreyard ebay engine-apply --titles ... --apply       # WRITES the portal
bin/coreyard ebay push --apply             # LISTS on eBay
bin/coreyard ebay delist --apply           # ENDS live eBay listings (irreversible)
```

Commands without `--dry-run` can write Shopify or sync state. `bin/coreyard orders
serve --write-orders` can also write the source database when the schema gate is
configured. Do not treat a connectivity check or DB-free demo as an offline unit test:
they use configured services.

### The CLI

`coreyard/cli.py` is the only command tree. Each command is mounted by calling the owning
module's `add_arguments(parser)` — the *same* function that module's own `main(argv)` calls,
so a flag cannot mean one thing under `coreyard sync` and another under `python -m
coreyard.run_sync`. Add a command by adding a row to `cli.COMMANDS` and an `add_arguments`
to the module; do not add routing to `bin/coreyard`, which is generated by `install.sh` and
deliberately holds no command list.

Every legacy `python -m coreyard.<module>` entry point still works and is still tested,
because this installation's crontab named them for a long time and a scheduled job that
changes behaviour silently costs a day before anyone notices. `coreyard sync` defaults to
`--sink api`; the legacy `run_sync` entry point keeps its original `csv` default.

`--lock NAME` and `--timeout DUR` are global flags (they precede the command). The lock is
`out/.NAME.lock` under `flock(2)`, the same file and mechanism `flock(1)` used, so the two
interoperate. A busy lock exits 0: a skipped tick is the intended outcome when a full sync
is still running.

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
coreyard/cli.py      the one command tree; every command is mounted here
coreyard/status.py   read-only overview of what the pipeline believes
coreyard/doctor.py   installation and liveness diagnostics (shared with scripts/healthcheck.py)
coreyard/ops.py      run history in the state database
coreyard/yms/        schema mapping, SMB/TDS reads, images, fitment, opt-in orders
coreyard/transform/  the canonical renderer, SEO, tags, shipping, weights, CSV
coreyard/sink/       the Shopify client, CSV output, publisher, bulk, OAuth, alt text
coreyard/orders/     order pipeline + queue, poll transport, lifecycle sync, policy
coreyard/reconcile/  yard-vs-store comparison, planning, and guarded application
coreyard/repair/     rewrite catalog output an older renderer produced
coreyard/audit/      read-only listing-quality checks with configurable thresholds
coreyard/ebay/       the listing-portal channel: portal map, client, research, guarded writes
coreyard/ai.py       opt-in local-CLI inference transport (no API key, no provider SDK)
coreyard/overrides.py reviewed per-R# title/price decisions, read by the canonical renderer
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

Photo refresh is **stage, attach, verify, then delete**. The old order deleted the live
media first, so any failure after that point — an unreachable share, a rejected staged
upload, a timed-out PUT, a `productSet` userError — left a product on sale with no
photographs at all, which is worse than the stale photo the refresh was called to fix. If
nothing can be staged, the existing media are left alone.

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

### Scopes and domain fingerprints

`sync inventory`, `sync photos` and `sync catalog` are **selectors over one plan**, not three
engines. There is one extract, one renderer and one diff. Each scope owns a projection of the
same `RenderedProduct.payload()` (`render.SCOPE_FIELDS`, `RenderedProduct.scope_fingerprint`),
never a second rendering — a domain fingerprint that disagreed with the renderer that
actually publishes would recreate the exact bug `transform/render.py` exists to prevent.

- `inventory` owns `inventory` and `price`; `photos` owns `images`; `catalog` is *everything
  else*, so a field added to `RenderedProduct` joins a scope automatically instead of falling
  outside all of them.
- Alt text is deliberately **not** in the photo scope. It is generated from the part's copy,
  so counting it as a photo change would tear down and re-upload the media of every retitled
  part — the one expensive thing a scoped run exists to avoid.
- Publishing always sends the whole product (`productSet` has set semantics), so scopes never
  decide *what* is written, only which parts a run acts on.
- Only a full `sync` or `sync inventory` may retire. A photo or catalog run has gathered no
  evidence about availability.
- **A scoped, `--limit`ed or `--r-number` run must never `state.commit`.** It records only what
  it published (`state.update(subset(...))`) and leaves the rest of the snapshot alone.
  Committing would mark every part the run *declined* to publish as up to date, and the change
  it skipped would never be published by anything. `run_sync._partial_view` is the one place
  that decides a run saw a slice of the yard.

New scope columns migrate on the pattern `image_fingerprint` established: `DEFAULT ''` reads
as "unknown", so the first run after an upgrade reports no scope changes rather than
republishing the catalogue once per scope. But unknown is not the same as unchanged: a part
whose canonical fingerprint moved while its scope baseline is unknown lands in
`DiffResult.unattributed`, and **every scope claims it**, exactly as every scope claims a
new part. Dropping those made `sync inventory` print "0 to publish" against 1,264 pending
changes, which reads as "the catalogue is in step". It self-heals as runs rewrite rows. **`parts.fingerprint` is never rewritten by a
migration.**

### The photo manifest, and banking progress

`image_fingerprint` hashes the share's manifest — name, size and modification time — not
filenames alone. A yard corrects a bad photograph by saving the new one over the old under
the same name, which a filename-only manifest could not see, so the storefront kept showing
the replaced picture indefinitely. `images.list_all_inventory_manifest` (whole share, one
round trip) and `images.list_inventory_manifest` (one part) must produce **byte-identical**
stamps via `images._stamp`, or every delta run re-fingerprints what it touches.

Changing the manifest shape means bumping `state.IMAGE_MANIFEST_VERSION`. A run whose stored
version is behind **rebaselines**: it records the new manifest and reports no photo changes.
Only `commit` may declare the new version, because only `commit` rewrites every row.

### Saying nothing when nothing is wrong

Three checks reported non-problems, and a check that stays lit for something already fixed
— or for something working exactly as designed — is how people learn to stop reading the
output. The fixes are worth keeping intact:

- **Run headers.** A scheduled job appends to one log forever, so without a boundary there
  is no way to tell a failure that is happening from one fixed days ago. `cli.main` writes
  `ops.run_header(...)` when stdout is not a TTY, and `doctor.check_logs` scans only from
  the last one. A log nothing here writes (a storefront script, a backup) has no boundary,
  so it still falls back to a shallow tail and can re-report a stale error — the one
  remaining gap.
- **Runs are recorded when they start,** not only when they end, so `ops.running()` can
  say what is in flight. `doctor` uses it to stop calling the delta cursor stale while a
  full sync holds the shared lock and the catch-up is being skipped *by design*. An
  unfinished row older than `ops.STALE_RUN` is treated as gone, because a SIGKILLed run
  never closes its own row. `ops.last()` skips unfinished rows: a run still going must not
  mask the outcome of the one before it.
- **Exit 124 is a deadline, not a fault.** A run stopped by `--timeout` kept everything it
  banked; reporting it as FAILED buries the runs that actually broke.

`run_sync.CHECKPOINT_EVERY` banks published fingerprints during a long run. Scheduled jobs
are wrapped in a timeout, so without this a backlog larger than one window can never be
worked off — this installation spent twelve consecutive hourly runs republishing the same
~2,000 of the same ~9,200 products, because each was stopped before its commit. Checkpointing
is safe only because a fingerprint is recorded strictly *after* the publish it describes
returned cleanly.

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

## The Listing-Portal Channel

`coreyard ebay` is a *second* sales channel, not a second publishing pipeline. It reads and
writes a configured listing portal, which in turn lists on eBay; Shopify is not involved
except through one file. Everything portal-specific is in `portal.json` (see Project
Boundaries) — if you find yourself writing a route, a form field id or a tab name into a
`.py` file, it belongs in the map instead.

The channel is optional and inert: an installation that sets no `EBAY_PORTAL_FILE` and no
`COREYARD_AI_ENABLED` never reaches any of it, and nothing in the Shopify pipeline imports
it.

### Three separate write surfaces, in increasing order of consequence

1. **Saving in the portal** (`ebay apply`, `ebay engine-apply`, `ebay aspects --apply`)
   changes stored values only. Nothing a shopper sees moves. This is the review window.
2. **Pushing** (`ebay push --apply`) sends those values to eBay. Now they are live.
3. **Delisting** (`ebay delist --apply`) ends live listings. This is **irreversible**:
   relisting mints a new item id and loses the watchers and the ranking the old one had.

Keep those three as three commands. Collapsing the first two removes the only point at
which a person sees what a batch of AI-researched prices actually says before buyers do.

### Guards that are part of the feature

- Every write is a dry run unless `--apply` is passed.
- `check_cap` counts **listings**, not plan entries, and raises *before* the first write, so
  a refused batch writes nothing rather than half of itself. `--yes-i-mean-it` lifts it.
- A per-group portal error is recorded in that group's result, not raised — one rejected
  group must not abandon the groups after it.
- `delist` resolves its target set live rather than trusting a saved file, and refuses when
  the grid returns fewer rows than it reports records: a listing that left the tab since the
  last pull must not be ended.
- Titles are validated against the *facts parsed from the source title*. A rewrite that
  drops the VIN code, the fitment years or the word "Engine" is rejected, and two
  interchange groups may never publish under one title — they are different parts.
- Title length is measured **escaped** (`ebay/util.title_length`): the portal escapes `&`
  and `"` before eBay counts them, so a title that fits locally can overflow there.
- Item specifics are validated against eBay's own category vocabulary
  (`EBAY_ASPECTS_FILE`). A value that is not derivable with confidence is left unset, and
  `--apply` refuses outright without the metadata: eBay ranks on aspect *match* and buyers
  filter on it, so a wrong aspect is worse than a missing one.
- Prices are guarded outside the model (`ebay/research.apply_guards`): a floor, a ceiling
  at `MAX_OVER_MEDIAN` times the comparable median, and a hard hold on any listing whose
  condition note mentions a defect. The unguarded answer is kept as `raw_suggested` so a
  reviewer can see what was overridden.

### Where this channel meets Shopify

At exactly one place: `engine-plan` writes a per-R# overrides file, and configuring it as
`STORE_CATALOG_OVERRIDES_FILE` makes the canonical renderer read those reviewed titles and
prices. That is deliberate — it is the same rule `transform/render.py` exists to enforce.
A researched price must reach Shopify *through the renderer*, so it lands in the
fingerprint and both sinks serialize it, rather than as an out-of-band product patch that
the next sync would silently revert. Overrides are keyed by **R#**, never by portal listing
id; `build_overrides` refuses rather than guessing when a listing has no R#.

### Inference

`coreyard/ai.py` is a transport, like `yms/db.py` and `sink/shopify_api.py`. Two of its
properties shape every caller: one invocation carries roughly 13k tokens of overhead, so
callers batch; and a rate-limited call returns a *successful-looking* envelope with no
error text, recognisable only by having spent no time, no turns and no tokens. Do not
"simplify" `_looks_rate_limited` — nothing else distinguishes that shape from a real
failure, and misreading it turns a fifteen-minute wait into an abandoned run.

The retry budget is bounded by an absolute deadline, not by an attempt count alone: five
retries at a doubling sixty-second base is about half an hour, and a run that dies inside
`time.sleep` banks nothing. Every AI-backed command checkpoints, and `--deterministic`
gives `engine-research` an explainable comp-median fallback for rows inference never
reached.

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
