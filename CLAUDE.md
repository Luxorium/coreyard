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
handle prefix. Site configuration belongs in `.env` and in `store.json` (sections `profile`, `weights`,
`shipping`, `orders`; the older per-file settings still work and still win); source table and column names belong in the local, gitignored
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
bin/coreyard init                          # write .env + store.json (start here)
bin/coreyard init --demo                   # ...against the bundled example yard
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
bin/coreyard part-types                    # part-type catalogue + wording gaps, read-only
bin/coreyard ebay daily                    # unattended pass, dry run
bin/coreyard ebay daily --apply            # ...and SAVE it in the portal (never pushes)
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

**"Listable" has one definition, and it now has two halves.** `inventory.photos_required`
(`STORE_REQUIRE_IMAGES`) and `inventory.researched_prices_required`
(`STORE_REQUIRE_RESEARCHED_PRICE`, which reads the R#s carrying a price in
`STORE_CATALOG_OVERRIDES_FILE`) are both asked in `yms/inventory.py`, by `fetch_parts` *and*
by `listable_r_numbers` — reconciliation asking a different question from sync is how a part
gets published on one run and archived on the next. Both default to off, because an
installation that has photographed or researched nothing must not have its whole catalogue
judged unlistable by an upgrade. `researched_r_numbers` returns `None` when the gate is off
and a set when it is on: those mean opposite things, and collapsing them would silently
unlist the entire catalogue.

```text
coreyard/cli.py      the one command tree; every command is mounted here
coreyard/setup_wizard.py  `coreyard init`: writes a working .env and store.json
coreyard/store.py    one store file (profile/weights/shipping/orders), explicit files win
coreyard/status.py   read-only overview of what the pipeline believes
coreyard/doctor.py   installation and liveness diagnostics (shared with scripts/healthcheck.py)
coreyard/ops.py      run history in the state database
coreyard/source/     where inventory comes from: the Source seam, the database, a CSV/SQLite file
coreyard/yms/        schema mapping, SMB/TDS reads, images, fitment, opt-in orders
coreyard/transform/  the canonical renderer, SEO, tags, shipping, weights, CSV
coreyard/sink/       the Shopify client, CSV output, publisher, bulk, OAuth, alt text
coreyard/orders/     order pipeline + queue, poll transport, lifecycle sync, policy
coreyard/reconcile/  yard-vs-store comparison, planning, and guarded application
coreyard/repair/     rewrite catalog output an older renderer produced
coreyard/audit/      read-only listing-quality checks with configurable thresholds
coreyard/ebay/       the listing-portal channel: portal map, client, research, guarded writes
coreyard/ebay/index.py      the one place pricing evidence is fetched from, and so replaceable
coreyard/yms/part_types.py  every part type the yard can inventory, and wording coverage
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

The renderer asks for the **compact** title form (`seo.build_title(..., compact=True)`), which
spans the years ("1998-2000") rather than listing them. The builder's own note argues the
listed form tokenizes better for web search, and that is a real trade — but the two
storefronts were publishing the same part under two different-looking titles, and one title
on both channels is the whole point of a single renderer. Three rules downstream of it are
worth keeping intact: `seo.group_model_labels` says each make once and drops the commas
(a truck fitting three cab weights lists "Silverado 1500 2500 3500", not the model three
times); `seo.title_condition` states finish and condition only from vocabularies it
recognises in the part's own note, and never a negated one, because reading "chrome" out of
"w/o chrome" asserts the opposite of what the yard wrote; and `seo._loss_rank` weights *what*
an over-long title gives up instead of counting drops. Counting alone let a truncated title
naming four vehicles beat an intact one naming a single vehicle — so the cheaper-looking
answer was the one that stopped saying what the part is.

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

### One snapshot, several channels

`parts` is the canonical **yard-side** snapshot and is deliberately not keyed by channel:
its fingerprint hashes the rendered product, which is the same product whichever channel
publishes it. What differs per channel is whether that channel *received* it, and with two
channels one succeeding while the other fails is the normal case, not the exception.

`channel_state(channel, r_number, fingerprint, remote_id, status, last_synced, last_error)`
records what each channel holds. Three rules make it worth having:

- A row is written only **after** a publish returns cleanly, for the same reason
  `CHECKPOINT_EVERY` banks only completed work.
- `record_channel_failure` records an error **without** advancing the fingerprint, so a
  failed part stays in `channel_pending` and is retried. Advancing it would settle the part
  on the strength of the attempt rather than the outcome.
- `remote_id` and `status` survive a write that does not carry them, so a price-only update
  cannot lose the listing id it was applied to.

`status` prints a per-channel line only once a second channel exists — with one, the
canonical snapshot already says everything it would, and a line that repeats another line
is a line people stop reading.

**A run may only record what it published.** The scoped and delta paths were built this way
(`state.update(subset(...))`); the full path was not, and committed everything it extracted.
A part the diff called added-or-changed that never landed was therefore written at its *new*
fingerprint, so the next run found it unchanged and never retried it — the change lost
silently and permanently. `run_sync._committable` is the one place that decides this now:
an outstanding part keeps the version the storefront actually has, a never-published part
stays absent so it still reads as new, and the photo manifest moves with the fingerprint.

### The photo manifest, and banking progress

`image_fingerprint` hashes the share's manifest — name, size and modification time — not
filenames alone. A yard corrects a bad photograph by saving the new one over the old under
the same name, which a filename-only manifest could not see, so the storefront kept showing
the replaced picture indefinitely. `images.list_all_inventory_manifest` (whole share, one
round trip) and `images.list_inventory_manifest` (one part) must produce **byte-identical**
stamps via `images._stamp`, or every delta run re-fingerprints what it touches.

Changing the manifest shape means bumping `state.IMAGE_MANIFEST_VERSION`. A run whose stored
version is behind **rebaselines**: it records the new manifest and reports no photo changes.
Only `commit` may declare the new version, because only `commit` rewrites every row. Version
**3** is the donor fallback below: a part that used to stamp nothing now stamps its donor's
frames, and without the bump the first run after the upgrade would have reported a photo
change for every unphotographed part in the yard at once.

### Photographs of the donor, when there are none of the part

A yard photographs the cars it buys and only some of the parts it pulls, so a large part of
the catalogue is listable in every respect except that it has no picture. Those parts may
publish with photographs of the vehicle they came off. The fallback is **opt-in and inert by
default**: it needs a `donor_images` query in `schema.json`, and without one an unphotographed
part publishes with no images exactly as before, and the second share listing is skipped
entirely.

- **A part with photographs of its own never consults the donor folder.** That is what keeps
  the fallback from moving one already-published product's fingerprint.
- **Donor references are namespaced** (`run_sync._DONOR_PREFIX`). The two folders share a key
  space — donor vehicle 1001 and R# 1001 both file a `1001_01.jpg` — so an unnamespaced
  reference could not tell the two pictures apart, and a part that later gets photographed
  properly would read as unchanged.
- **Both resolvers share one implementation** (`run_sync._photo_view`). The full path serves
  it from one listing of each folder and the delta path lists per key, but the choosing, the
  trimming and the spelling are the same code — the delta invariant above now has two folders
  to get identically right instead of one.
- **A part inherits only the opening frames** (`images.donor_photo_limit`,
  `STORE_DONOR_PHOTO_LIMIT`, default 6), which are the general views. A donor shoot documents
  a whole car — a median of 16 frames and up to 99 — because it was taken to record a vehicle,
  not to sell one bracket off it. The resolver and the publisher must trim to the same number,
  or the publisher attaches a set the fingerprint did not cover and the part republishes
  forever.
- **Each donor frame is uploaded once**, not once per part. Roughly seven parts come off each
  donor, so staging its frames per product would send the same photograph seven times.
  `state.donor_media` maps (donor key, filename) to the Shopify file id that `FileSetInput`
  references, and a row is written only *after* `fileCreate` returned an id — for the same
  reason a channel row is written only after a clean publish. A frame whose upload produced no
  id is skipped rather than guessed at, and the parts after it try again.
- **The listing says whose picture it is.** `profile.donor_photo_note` renders above
  everything else in the body, and the alt text describes the donor vehicle rather than the
  part. That a part was not photographed individually is a fact about the data, not a promise
  about the business, which is why it rides on `Part.uses_donor_photos` and through the
  renderer rather than being left to each site to remember.

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

The channel is optional and inert: an installation that sets no `EBAY_PORTAL_FILE` never
reaches any of it, and nothing in the Shopify pipeline imports it.

### Keeping up, unattended

`ebay daily` is the whole chain as one command, and it is a *driver*: every decision is made
by the module that already owned it, so nothing in it can be tested only through it. It
walks part types rather than the tab as a whole, because the grid will not serve a whole tab
but answers a filtered read completely — and the types it walks come from the yard extract,
so a type a worker files a part under tomorrow is walked tomorrow without anyone editing a
list. The unlisted tab is the queue, so there is no cursor to keep or corrupt.

**It stops at the portal.** It never calls submit. An unattended job is exactly the thing
that must not close the review window, so pushing stays a separate command a person runs.

### Staying signed in

Borrowing the browser's session needs no password on disk, which is why it was the whole
design. It does not survive a run that outlives the session, though, and an overnight research
pass is exactly that — dying two thirds of the way through costs the night.

`EBAY_PORTAL_USER`/`EBAY_PORTAL_PASSWORD` are therefore a sign-in of **last resort**:
consulted only after the explicit jar, the live browser and the owner-only cache have all come
up empty, and only when `portal.json` maps `auth.login_fields`. Which input the form calls the
user, the password and the anti-forgery token is portal vocabulary and belongs in the map,
exactly like a grid parameter or a tab status. `PortalClient._request` also re-signs **once**
when a request is redirected to the login path mid-pass, and retries — once, so a genuinely
wrong password fails fast instead of hammering the form, and the stale session handle is
dropped with it. The token is read from the form on every attempt rather than cached, because
it is bound to the page that issued it. A password is never printed, logged, or included in an
error message, and a sign-in that returns HTTP 200 without every mapped cookie is treated as a
failure — that is exactly what a wrong password looks like.

### Which part a listing is

`ebay/link.py` answers this from grid data alone, because reading each listing's edit form
costs about forty seconds — roughly sixty-six hours for the unlisted tab. Two independent
rules cover different halves of the catalogue:

1. **The R# the portal put at the end of its own title**, accepted only when the part it
   names agrees with the donor stock number *and* part type the grid reported separately.
   This is what resolves the parts a car carries two of; the donor key alone is genuinely
   ambiguous for every tail lamp, mirror and headlamp.
2. **(donor stock number, part-type code)**, when that pair names exactly one yard part.
   This is what resolves engines, whose titles carry no R#.

Where a donor yielded a left and a right of the same type and no R# is in the title, two
tie-breakers run inside that candidate set, strongest first:

3. **The interchange number the grid reports** (`narrow_by_interchange`). It is not unique
   across the yard — that is what makes it an *interchange* — but inside a set that already
   agrees on donor and part type it is decisive, because the catalogue gives a car's left and
   right the same number with a different side suffix. An identifier agreeing with an
   identifier, so it goes first.
4. **The side the portal's own title names** (`narrow_by_side`), against the side the yard
   recorded. Measured on the unlisted tab the two agreed 1,009 times and contradicted zero
   times; the interchange number settled 53 the title could not, and the title then settled
   1,187 of the 1,274 listings the first two rules had left, 93%. Both are grid fields, so
   neither costs an extra request.

None of the four is trusted alone, and each tie-breaker refuses as readily as it decides:
`narrow_by_side` requires *every* candidate to have a side on file, because picking the only
part recorded as left out of a pair whose other side was simply never entered is a coin flip
wearing a rule's clothes. A listing that resolves to no single part is **left alone** rather
than retitled as a candidate. Keep that refusal: a listing repriced as the wrong part is worse
than one nothing touched.

### Three separate write surfaces, in increasing order of consequence

1. **Saving in the portal** (`ebay apply`, `ebay engine-apply`, `ebay aspects --apply`)
   changes stored values only. Nothing a shopper sees moves. This is the review window.
2. **Pushing** (`ebay push --apply`) sends those values to eBay. Now they are live.
3. **Delisting** (`ebay delist --apply`) ends live listings. This is **irreversible**:
   relisting mints a new item id and loses the watchers and the ranking the old one had.

Keep those three as three commands. Collapsing the first two removes the only point at
which a person sees what a batch of researched prices actually says before buyers do.

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
- Prices are guarded outside the calculation that proposes them
  (`ebay/research.apply_guards`): a floor, a ceiling at `MAX_OVER_MEDIAN` times the
  comparable median, and a hard hold on any listing whose condition note mentions a defect.
  The unguarded answer is kept as `raw_suggested` so a reviewer can see what was overridden.
- Every published price lands a penny under the same five-dollar grid (`research.PRICE_STEP`),
  including one the floor supplied: a floor is a business minimum, not a shopper-facing
  number, and `$12.50` sitting beside `$64.99` reads like two different shops. A
  comparable-derived price still rounds **down**, for the reason it always has — the market
  cleared there and rounding past it invents evidence. A floored one has no evidence to
  respect, so it rounds to nearest, and either is nudged up a step if that would land it under
  the floor. `STORE_PRICE_STEP` puts the catalogue's own charm rounding on the same grid; it
  defaults to a dollar so no existing installation's prices move on upgrade.

### Where this channel meets Shopify

At exactly one place: `engine-plan` writes a per-R# overrides file, and configuring it as
`STORE_CATALOG_OVERRIDES_FILE` makes the canonical renderer read those reviewed titles and
prices. That is deliberate — it is the same rule `transform/render.py` exists to enforce.
A researched price must reach Shopify *through the renderer*, so it lands in the
fingerprint and both sinks serialize it, rather than as an out-of-band product patch that
the next sync would silently revert. Overrides are keyed by **R#**, never by portal listing
id; `build_overrides` refuses rather than guessing when a listing has no R#.

### No inference, anywhere

**CoreYard runs no model and calls no LLM.** Titles come from the canonical renderer,
comparables from a parsed public index, and prices from arithmetic over those comparables.
This is a hard constraint, not a preference, and it is the reason the channel can run
unattended: a program that prices 20,000 listings the same way twice can be reviewed once
and trusted, while an answer that varies between runs has to be read every time — and
nobody reads 20,000 of anything.

Three properties follow, and are worth keeping:

- **Every price explains itself.** `research.price_listing` returns the named adjustments
  that produced it ("96% of the supplied domestic median; under 100k miles +7%; tested
  +8%"), so a reviewer checks the reasoning rather than trusting the number.
- **Nothing is invented.** With no comparable median a listing gets *no price* and is held,
  because a listing left at the yard's own price merely fails to improve, while a guessed
  one is wrong in a direction nobody can predict.
- **A run has no external budget to exhaust.** There is no rate limit, no backoff, no
  wall-clock deadline, and no reason for a scheduled job to bank partial work against a
  quota. Checkpointing survives because a portal read is still slow, not because a
  provider might refuse.

If a future task seems to want a model — better titles, a judgement call on a defect note —
the answer is a rule in the renderer or a held listing for a person to decide, not a
dependency that makes the nightly run non-reproducible.

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
