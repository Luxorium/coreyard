# CLAUDE.md

Read [AGENTS.md](AGENTS.md), including its Agent Workflow section, for the shared
contributor and execution conventions used with GPT-6 Astra in Codex and other coding
agents. This file retains its `CLAUDE.md` name for Claude Code discovery and adds
architecture and operational invariants. Keep shared workflow guidance in `AGENTS.md`
to avoid conflicting copies. Model selection belongs in the coding client's
configuration; CoreYard itself continues to run without an LLM.

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

Those files are a contract with whatever storefront sits on the other side, so their
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
.venv/bin/python scripts/inventory.py --check              # docs/INVENTORY.md is current
.venv/bin/python scripts/ledger.py --summary               # release acceptance status
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

**"Listable" has one definition.** The source mapping's scope, `Part.is_listable`'s positive
price check, and `inventory.photos_required` (`STORE_REQUIRE_IMAGES`) are applied in
`yms/inventory.py` by `fetch_parts` *and* `listable_r_numbers` — reconciliation asking a
different question from sync is how a part gets published on one run and archived on the
next. The photo policy defaults off so an installation with no image mapping is unchanged by
an upgrade. The price is always the source database's value.

```text
coreyard/cli.py      the one command tree; every command is mounted here
coreyard/setup_wizard.py  `coreyard init`: writes a working .env and store.json
coreyard/store.py    one store file (profile/weights/shipping/orders), explicit files win
coreyard/status.py   read-only overview of what the pipeline believes
coreyard/doctor.py   installation and liveness diagnostics (shared with scripts/healthcheck.py)
coreyard/capabilities.py  what this installation is configured to do; read before any check runs
coreyard/ops.py      run history in the state database
coreyard/source/     where inventory comes from: the Source seam, the database, a CSV/SQLite file
coreyard/yms/        schema mapping, SMB/TDS reads, images, fitment, opt-in orders
coreyard/transform/  the canonical renderer, SEO, tags, shipping, weights, CSV
coreyard/sink/       the Shopify client, CSV output, publisher, bulk, OAuth, alt text
coreyard/orders/     order pipeline + queue, poll transport, lifecycle sync, policy
coreyard/reconcile/  yard-vs-store comparison, planning, and guarded application
coreyard/repair/     rewrite catalog output an older renderer produced
coreyard/audit/      read-only listing-quality checks with configurable thresholds
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

**How many vehicles a title names is a character budget, not a count.**
`profile.title_max_models` is a hard ceiling on vehicle names, and **0 means the only
ceiling is the destination's own limit** — 255 on Shopify. The
CoreYard default stays at 4 because lifting it rewrites every multi-vehicle title a site has
published, which is an installation's decision rather than something an upgrade does to a
live catalogue; a yard with a rich fitment catalogue should set 0 in its profile. The names
a cap suppresses are exactly the long-tail searches a salvage listing wins on, and the
phrase they were replaced with ("and 17 more") names no vehicle and carries no search term.
`fit_title` bounds its scan by the labels that exist and by what the budget could hold, so a
capped site's titles are byte-for-byte unchanged.

Two rules keep an uncapped title readable, and both only became visible once titles stopped
stopping at four vehicles. A title qualifier is filtered against the words the title has
already used, and a phrase reduced to a bare connector is dropped rather than left holding
the end of the line ("Master Switch Mirror And"). And `seo._fix_token` cases a token's
alphanumeric *core*, because the model column annotates itself in brackets — "SAFARI (GMC)",
"BLAZER/JIMMY (full size)" — and casing the bracket as if it were a letter published "(gmc)"
and "full Size" in the middle of otherwise clean vehicle names.

A third rule protects the VIN code. `_title_phrase` drops single letters because a truncated
catalogue note reads "Driver s" — but in "2.4L (VIN B, 8th digit)" the single letter *is* the
engine code, the most useful character in the phrase. Dropping it left the label naming
nothing, which published as "Modulator Assembly 2.4L VIN 8th Digit". So a letter directly
after "VIN" is kept, `_VIN_POSITION` strips the counter's bookkeeping ("8th digit", "7th and
8th digit" — `_NOISE_PHRASE` only ever caught a phrase that was *entirely* an ordinal), and
`_drop_orphan_vin` removes a trailing "VIN" that names no code, because a label pointing at
nothing says less than no label at all.

**What a title may state about condition is the yard's record, never an inference.**
`seo.title_condition` reads the part's own note; `seo.title_grade` states the yard's grade
under the site's own wording (`title_grade`, e.g. "{grade} Grade" -> "A Grade"); and
`seo.title_mileage` states the donor's odometer. All three default to silent, and the
mileage one is doubly gated because the source system stamps the donor's mileage on *every*
part pulled from it — the number exists for a door glass as surely as for the engine, and on
the glass it is noise. So it is stated only for the part types in
`title_mileage_part_types`, and only at or below `title_mileage_max`; above that the number
argues against the part and a seller may reasonably say nothing. Mileage renders in
thousands ("142K Miles"), floored and never rounded up, because the exact figure needs a
comma and a comma is one of the symbols `seo_clean` strips — "142,684" would reach a shopper
as "142 684".

**A reviewed title is extended, never replaced.** `overrides.py` titles win over the
renderer because they hold facts the database does not: the engine titles reviewed through
a listing portal state displacement, VIN code and cylinder configuration for parts whose
note in the yard system is *empty*. They are also
deliberately narrower than fitment — one engine variant, not every model the interchange
group covers, which for a 5.2L V8 is the difference between "1998-2003 Dodge 1500 Van" and
eleven models across 1992-2003. What they predate is the grade and the donor's mileage, and
those are facts about *this part* rather than about the engine family, so
`seo.extend_override_title` appends them to the reviewed wording instead of losing them.
Nothing already said is repeated, and a title that would overflow the budget is returned
untouched — a reviewed decision is not worth truncating for an addition.

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

### Resolving fitment, and not resolving it twice

Fitment is one query per interchange **group**, not per part. `InterchangeResolver` has
always cached within a run, but this installation has 16,239 distinct groups behind 26,858
parts — a reuse factor of 1.65 — so that saves about a third and leaves 16,239 round trips
over the named pipe, near enough an hour, paid again by every command that renders a title.

`yms/fitment_cache.py` keeps those answers between runs (48.9s -> 0.1s measured on 400
parts). It is sound because the applications table is the catalogue's *reference data*: it
changes when the catalogue is updated, not when a car arrives or a part sells. Four rules
make it safe to leave on:

- **The raw catalogue rows are cached, never the parsed fitment.** Parsing is local, cheap
  and pure — and it is the half of this pipeline that keeps getting *fixed*: a model number
  read as a year, a make-less row widening a title's years, a qualifier cut where the
  restriction lives. Caching parsed output would hide the next such fix behind a stale cache
  until somebody remembered to clear it.
- **The site's own SQL is fingerprinted.** Edit `interchange_applications` or
  `interchange_makes` in `schema.json` and every entry is dropped, because the stored rows
  answered a different question.
- **Entries expire** (`COREYARD_FITMENT_CACHE_DAYS`, default 30), so a catalogue update is
  picked up without anyone remembering to act.
- **A failed query is never stored.** An empty result is — a part number that fits nothing is
  a real answer — but a transport blip must not be frozen in as "this part fits nothing".
- **Losing the cache never loses the run.** `get` and `put` swallow every `sqlite3.Error`:
  a cache that cannot answer is indistinguishable from one with nothing to say, and the
  caller has a database to fall back on. `summary()` reports a cache that gave up, because a
  silent cache and a broken one look identical from outside and only one of them is fine.

Every scheduled job on a host like this resolves fitment — a five-minute `sync delta`, the
the reconcile pass, the hourly sync — so **several processes share this
file and contention is the normal case**. WAL allows exactly one writer, so `put` commits on
the spot rather than batching. Holding one transaction across hundreds of round trips is
what starved a `sync delta` and killed a two-hour repair at part 1,000 of 26,660 with
"database is locked"; the write is a single small row and the caller has just waited on a
network query, so committing immediately costs nothing measurable.

The cache is an optimisation, not a record: deleting the file costs one slow run.

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
- **A part inherits the configured donor frames** (`images.donor_photo_limit`,
  `STORE_DONOR_PHOTO_LIMIT`, default 6), which are the general views. A donor shoot documents
  a whole car — a median of 16 frames and up to 99 — because it was taken to record a vehicle,
  not to sell one bracket off it. The resolver and the publisher must trim to the same number,
  or the publisher attaches a set the fingerprint did not cover and the part republishes
  forever. Set `STORE_DONOR_PHOTO_LIMIT=0` to use every available donor frame in both paths.
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

**"Not configured" and "broken" are different answers.** `coreyard/capabilities.py` is the
only place that decides which, and every diagnostic asks it before it asks anything else. A
capability is `on` when the operator configured it, `missing` when something else that *is*
on depends on it, and `off` when it is simply not part of this installation — and only the
middle case is a failure. That distinction is what lets order booking stay opt-in without
its absence reading as a broken install, and what stopped `doctor`
telling a yard running `COREYARD_SOURCE=tabular:parts.csv` to supply SMB credentials it does
not need, a `schema.json` nothing would read, and an `smbclient` it never shells out to.

Three rules keep it honest. It **reads configuration and nothing else** — no socket, no
query, no write — because it runs before the checks decide what to check. It **must not call
`load_env`**, for exactly the reason the render-path gates must not: a gate that loads the
environment leaks the site's real `.env` into every unit test that touches it. And the
resolved snapshot is **passed down, never re-derived**: `source.load(None)` re-reads the
environment, so a check that resolved a tabular source and then asked for "the source" was
handed the database instead — which is how a unit test came to open a live named pipe.

**A check that stays quiet is the same defect as one that stays lit.** The delta cursor is
allowed to age while a full sync holds the shared lock, because the catch-up tick is skipped
by design. That excuse must be *bounded* — one full sync interval past the staleness
threshold — or it inverts: on a host whose hourly full sync takes most of an hour a sync is
almost always running, so "a sync is running" explained a cursor frozen for five days as
readily as one frozen for six minutes, and `doctor` reported the five days as OK.
`doctor._suppression_grace` and `alerts.check_delta` share the rule and read the interval
from the host's own schedule (`schedule.installed_intervals`, which recognises hand-written
crontab entries by the launcher, not by a marker this project wrote).

**Alerting notifies three times and no more.** `coreyard alert` (`coreyard/alerts.py`)
records each condition by a stable key, so it can tell a new breakage from a continuing one
and can match a recovery to what it resolves: once when it appears, again after
`COREYARD_ALERT_REPEAT` if still true, once when it clears. A delivery that failed is never
recorded as sent — the condition stays open and is retried, because a page nobody received
must not read as a page that was answered — and a breakage nobody was told about sends no
recovery notice, which would otherwise arrive as a fresh incident. Alerts leave the host, so
they carry order *names* and never payloads, credentials, or a notifier's own stdout, which
may echo a URL with a token in it. Delivery is a configured command rather than a built-in
mail or chat client: no transport dependency, no vendor, and every operator already has
something that works.

**Code paths and data paths are different roots.** `config.REPO_ROOT` is where the code
is — the launcher, the virtualenv interpreter, `schema.example.json`, the source
fingerprint. `config.DATA_ROOT` (and `config.out_dir()`) is where this installation's own
files are: `.env`, `store.json`, `schema.json`, the state and queue
databases, `out/` and the locks. Anchoring the second to the first is correct only in a
source checkout; installed as a package it means writing into `site-packages`, which fails
on a read-only tree and is wrong even when it works, because the next upgrade replaces the
directory holding the yard's sync state. Resolution order is `COREYARD_HOME`, then the
checkout when this is one and it is writable, then `$XDG_DATA_HOME/coreyard` — the middle
rule is what guarantees an upgrade moves nothing for an existing installation.
`COREYARD_HOME` is read from the real environment only, never through `_get`, because the
location of `.env` cannot be configured inside `.env`.

**An unsupported combination refuses before side effects.** `cli.SUPPORT` declares what
each of the 55 command nodes writes and the capabilities it cannot run without; `cli.unmet`
compares that against the resolved snapshot and `cli.main` exits 2 before the lock is taken,
naming the capability, the reason and the configured source. The tree is walked by one
implementation (`cli.tree`), used by both the preflight and `scripts/inventory.py`, because
a generated document that walked the tree differently from the code enforcing it would
describe a different tool. Two rules keep the declarations honest: "cannot run without" is
strict, so a capability only *some* invocations need does not belong there — gating `orders
poll` on the webhook secret would refuse to poll for a site that deliberately never
registered a webhook — and a refusal is *recorded*, so a scheduled job that starts refusing
every tick does not read as a job that stopped being scheduled.

**A run's exit code is part of its record.** A command reports failure by *returning* a
nonzero code, not by raising, so from inside `ops.record` a run that exited 2 and one that
exited 0 look identical. Believing the second wrote `ok=1` for runs the shell had already
called failures, and `coreyard status` then reported the last full sync as successful.
`cli.main` assigns the result to the yielded `Outcome`; `record` treats an exception as a
failure whatever the code says, and stores the code so a failure can be told from another.
`exit_code` is NULL for rows written before the column existed, because "not recorded" and
"exited cleanly" are different answers.

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

## Reviewed Title Overrides

`overrides.py` reads a per-R# title override file named by `STORE_CATALOG_OVERRIDES_FILE`,
and the canonical renderer prefers those titles over its own. They hold facts the database
does not — an engine's displacement, VIN code and cylinder configuration for parts whose
note in the yard system is empty — so `seo.extend_override_title` appends the grade and the
donor's mileage rather than losing them.

The file was originally produced by a listing-portal channel that CoreYard no longer has.
It is now a **hand-maintained input**: the existing entries keep applying, and nothing
regenerates them. Deleting it would re-title those parts from the renderer alone and lose
the engine facts permanently, so it is kept deliberately rather than by neglect.

## No inference, anywhere

**CoreYard runs no model and calls no LLM.** Titles come from the canonical renderer and
prices come from the source database. This is a hard constraint: the same part must produce the same
product on every run, and no external pricing source or inference may override the yard's
recorded amount.

If a future task seems to want a model — better titles, a judgement call on a defect note —
the answer is a rule in the renderer or a held product for a person to decide, not a
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

For documentation-only changes, review the diff, check references, and run the neutrality
check and `git diff --check`. For code or behavior changes, also run the full offline
unit suite. When a command, subcommand or flag changes, regenerate the functionality
inventory (`scripts/inventory.py`) and give the new node an entry in `cli.SUPPORT` — the
suite fails if the tree and the declared support statuses disagree, because a public
command with an undocumented support status cannot be qualified for release. Once these pass, repeat or broaden checks only for a change, failure, or
unresolved concern, as described in `AGENTS.md`.
Explain any schema/config assumptions and any storefront-visible output changes. Call
out changes that affect handles, fingerprints, retirement, API fields, webhook PII,
source writes, or tax behavior. Keep `README.md`, `.env.example`, `schema.example.json`,
`AGENTS.md`, and this file synchronized when their documented interfaces change.
