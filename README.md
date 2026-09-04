# CoreYard

[![CI](https://github.com/luxorium/coreyard/actions/workflows/ci.yml/badge.svg)](https://github.com/luxorium/coreyard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

A [Luxorium](https://luxorium.dev) project

**CoreYard is the backend integration engine.** It handles inventory extraction, product
rendering, Shopify catalog synchronization, orders, reconciliation, catalog repair, and
generic operational automation for a salvage yard. Site-specific storefront design and
business policy are supplied through external configuration — never hardcoded here.

It publishes a yard's parts inventory to **Shopify** — products, photos, fitment, weights,
and inventory levels, kept in sync. It reads an existing yard management system's SQL Server
database and its part-photo file share, both over SMB, because the database host commonly
exposes no SQL TCP port.

CoreYard is an independent tool. It is not affiliated with, endorsed by, or a product of any
yard management software vendor; it reads a database you already license and operate.
Everything specific to your site — hosts, credentials, schema, business name, storefront copy —
lives in local configuration, never in this repository.

```
   your yard system                       this repo (translator)                 Shopify
  ┌──────────────────────┐        ┌───────────────────────────────┐        ┌──────────────┐
  │ parts + donor-vehicle│  SMB   │ extract → transform → diff    │  push  │ products,    │
  │ tables (priced/avail)│──────▶ │ (Part → Shopify product)      │──────▶ │ inventory,   │
  │ part-photo share     │  2/3   │ + part photos by unique R#    │  API / │ photos       │
  └──────────────────────┘        └───────────────────────────────┘  CSV   └──────────────┘
```

Source of truth is the yard system's own database — no middleware, no export files.

## Try it in a minute

CoreYard can run with no database, no photo share and no credentials, on a bundled
seven-part example yard:

```bash
./install.sh --no-deps --yes          # or: pip install -e .
bin/coreyard init --demo              # writes .env and store.json
bin/coreyard sync --sink csv --dry-run
```

That renders real products — titles, descriptions, tags, weights, fitment — and writes
`out/products.csv`. Nothing is published anywhere.

To point it at a real yard, `bin/coreyard init` asks the same questions interactively. A
yard whose inventory does not live in a SQL Server database can export a CSV or SQLite file
whose columns are named after `Part` fields and set `COREYARD_SOURCE=tabular:<path>`; see
`examples/parts.csv`.

## Status

| Layer | State |
|---|---|
| Image fetch from the photo share over SMB (by R#) | **working, tested vs live server** |
| Live SQL connect over the `\sql\query` named pipe | **working** (`coreyard/yms/smb_tds.py`) |
| Schema discovery | **done** — real schema mapped 2026-08-14 |
| Extract query (`inventory.py`) | **working** against a real mapped schema (`is_configured()`) |
| Interchange fitment + SEO titles/meta/tags | **working, unit-tested** |
| One canonical rendered product behind the fingerprint and both sinks | **working, unit-tested** |
| Site policy (claims, wording, weights, audit thresholds) from external config | **working, unit-tested** |
| Storefront-owned tags preserved through every update | **working, unit-tested** |
| Shipping weight published on every upsert, from a site weight table | **working, unit-tested** |
| Incremental sync diff (add/change/unchanged/sold) | **working, unit-tested** |
| Delta catch-up (`--delta`: changed-since cursor, sub-second) | **working, unit-tested** |
| Optional part-type aliases + donor-vehicle detail | **working, unit-tested**, off by default |
| Offline end-to-end demo (real photos → CSV) | **working** (`scripts/demo_offline.py`) |
| Shopify Admin API sink (products + photos + inventory) | **working** — validated on a live catalog of ~9,400 image-backed parts |
| Sold-part retirement (qty 0 → archived, with safety guards) | **working, unit-tested** |
| Photo-change detection (share listing folded into the fingerprint) | **working, unit-tested** |
| Donor-vehicle photos for parts the yard never photographed | **working, unit-tested**, off by default |
| Paid-order webhook → yard work order + instant delist | **working, unit-tested** |
| Shipping classification applied during publish, from a site policy | **working, unit-tested** |
| Structured metafields (grade, mileage, condition, fitment) | **working, unit-tested** |
| Offline validation of every external config file (`validate`) | **working, unit-tested** |
| Order polling (same pipeline, for hosts with no inbound port) | **working, unit-tested** |
| Order webhook → work order in the source database (opt-in) | **working, unit-tested** |
| Source order status → Shopify tags/note/fulfillment (policy-gated) | **working, unit-tested** |
| Reconciliation against the live store, with retirement guards | **working, unit-tested** |
| Catalog repair (titles, tags, SEO, weights) against the renderer | **working, unit-tested** |
| Generic catalog audit with site-configurable thresholds | **working, unit-tested** |
| Guarded listing-portal titles and source-price synchronization | **working, unit-tested** |

Known gaps: a part's *variant-level* media assignment is not managed — photos attach to the
product, not to a specific variant, which is fine while every part is a single-variant product.

## Repo layout

```
install.sh                   one-command installer (any Linux distro)
bin/coreyard                 launcher (generated by install.sh); one exec onto the CLI
coreyard/
  cli.py                     THE command tree: every command, mounted in one parser
  __main__.py                `python -m coreyard`
  status.py                  read-only "what does the pipeline believe right now"
  doctor.py                  installation and liveness diagnostics
  ops.py                     run history: what ran, when, and whether it worked
  models.py                  Part dataclass (the neutral extract-to-transform contract)
  config.py                  .env loader + typed settings + the store profile
  overrides.py               reviewed per-R# title/price decisions for the renderer
  ai.py                      opt-in local-CLI transport for batched catalogue analysis
  profile.py                 site merchandising policy (what a listing may claim)
  state.py                   SQLite fingerprint store + add/change/remove diff, per-channel
                             state, retirement memory, and uploaded donor frames
  run_sync.py                the sync itself: full, delta, and the scope selectors
  yms/
    smb_tds.py               TDS over an SMB2/3 named pipe (impacket); the transport
    db.py                    read-only connect/query/ping on top of smb_tds
    discover_schema.py       introspect + rank likely part/price/vehicle tables
    schema.py                the site-supplied mapping (schema.json), parsed
    inventory.py             the extract query + the one definition of "listable"
    interchange.py           resolve full fitment (which vehicles a part fits)
    delta.py                 changed-since catch-up extraction
    enrich.py                optional part-type aliases and donor-vehicle detail
    images.py                fetch {R#}_NN.jpg photos, and the donor vehicle's behind them
    orders.py                guarded, opt-in storefront order booking
  transform/
    render.py                THE canonical renderer: Part -> RenderedProduct -> fingerprint
    seo.py                   the title/meta/description/tag engine the renderer drives
    tags.py                  tag ownership: keep ours current, preserve everyone else's
    weights.py               shipping weight from a site-supplied rules table
    pricing.py               exact money parsing and formatting
    shopify_product.py       RenderedProduct -> Shopify CSV rows
    shopify_csv.py           Shopify product-CSV writer
  sink/
    csv_sink.py              write out/products.csv
    shopify_api.py           the Shopify client + RenderedProduct -> productSet input
    shopify_write.py         publisher: product + staged photos + inventory + weight
    shopify_bulk.py          resumable, concurrent bulk publish over shopify_write
    shopify_oauth.py         one-time Client ID/Secret -> SHOPIFY_ADMIN_TOKEN exchange
    backfill_alt.py          alt text for photos published before it was generated
  orders/
    pipeline.py              optional work order + delisting, and the queue
    poll.py                  pull transport, for hosts that cannot receive webhooks
    lifecycle.py             push source-system order status back onto Shopify
    policy.py                site order policy (which shipping groups others fulfill)
  reconcile/                 compare the yard with the store; plan and close the gap
  repair/                    rewrite catalog output an older renderer produced
  audit/                     read-only listing-quality report
  ebay/                      neutral listing-portal client and guarded engine workflow
  webhook.py                 paid-order receiver: HMAC, HTTP, subscriptions
  schedule.py                systemd/cron sync scheduling helper
scripts/demo_offline.py      offline proof (no DB needed)
scripts/check_neutrality.py  CI guard: no vendor or site names in the tree
scripts/test_db_connection.py  one-off live DB connectivity check
tests/                       unittest suite - offline, no DB, no network, no .env
```

### One rendered product, two sinks

Everything a shopper can see is produced once, by `transform/render.py`, as a
`RenderedProduct`. That object is what gets fingerprinted, what the Admin API serializes, and
what the CSV writer serializes:

```
your yard database
      |
      v
   Part                       the neutral extract-to-publish contract
      |
      v
   render()  ------------->   RenderedProduct
                                |-- title, description, vendor, type, price, inventory
                                |-- tags, including the shipping classification tag
                                |-- SEO title and description
                                |-- structured metafields (grade, mileage, condition, fitment)
                                |-- images, alt text, shipping weight
                                +-- fingerprint()   <- everything above, hashed
      |
      +--> productSet input   (Admin API sink)
      +--> CSV rows           (CSV sink)
      |
      v
   Shopify
      |
      v
   your theme                 reads the fields and metafields above directly
```

The last line is the point of the structured metafields. A theme that had to recover a
vehicle by taking a generated title or tag apart would break the moment that wording
improved; reading `fitment` gives it named fields that mean the same thing next year.

This is a correctness property, not tidiness. When the two sinks rendered their own titles
and tags and the fingerprint covered only one of them, changing the renderer that actually
published left the stored hash identical — so every stale product was classified "unchanged"
and the storefront kept showing output no current version of the code would produce.

## Install

```bash
git clone https://github.com/luxorium/coreyard.git
cd coreyard
./install.sh
```

That's the whole thing. The installer detects your package manager (apt, dnf, yum, pacman,
zypper, or apk), installs Python 3.10+ and `smbclient`, builds a virtualenv, seeds a `.env`,
installs a `bin/coreyard` launcher, and verifies the result by running the offline test suite.
It asks before touching anything with `sudo`.

```bash
./install.sh --no-deps   # system packages already present
./install.sh --yes       # never prompt (for scripted installs)
./install.sh --help
```

If your distro ships Python with `ensurepip` stripped out, the installer detects the failed
`venv` creation and bootstraps `pip` itself — no manual workaround needed.

Only two Python dependencies exist: `impacket` (the database transport) and `requests` (the
Admin API). Photo fetching shells out to the `smbclient` CLI, and the transform/CSV code needs
no third-party packages at all.

### Manual install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
```
You will also need `smbclient` from your distro's Samba client package.

## One-time setup

`bin/coreyard init` writes both files a site owns: `.env` for connection and identity
settings, and `store.json` for storefront policy. Run it first; the rest of this section is
the reference for what it wrote and what else can be set.

`store.json` holds this storefront's policy as four optional sections — `profile` (what the
yard is willing to claim), `weights`, `shipping` and `orders`. Copy `store.example.json` and
edit, or point `STORE_FILE` elsewhere. Every section is optional and an absent one means
CoreYard's neutral default. The older `STORE_PROFILE_FILE`, `STORE_WEIGHT_RULES_FILE`,
`STORE_SHIPPING_POLICY_FILE` and `STORE_ORDER_POLICY_FILE` settings still work and still
take precedence, so an installation with four separate files need not change anything.

`bin/coreyard validate` checks all of it offline.


### 1. Database access — no server changes required
Many installs expose SQL Server **only** through the `\sql\query` named pipe over SMB (SMB1
disabled, no SQL TCP listener), so no port has to be opened and no SQL login has to be created.
`coreyard/yms/smb_tds.py` opens that pipe with impacket's SMB2/3 client as the existing service
account and runs impacket's TDS engine over it with **Windows Authentication** — the same
"ODBC + Windows Auth" path such systems already support, just from Linux. Consequences worth
knowing:

* Authentication uses the **SMB** credentials. `YMS_DB_PASSWORD` is unused and there is no
  dedicated SQL login. Normal extraction is `SELECT`-only; optional order booking is the sole
  write and uses its own guarded executor.
* A normal Linux SQL driver (`python-tds`, `pyodbc`, jTDS) cannot reach this server at all.
* Verify with `.venv/bin/python scripts/test_db_connection.py`, which prints `@@VERSION` and
  the user databases.

### 2. Map your database (`schema.json`)

**CoreYard ships no vendor schema.** Table and column names belong to whoever wrote your yard
management system, so you supply them locally — exactly like `.env`, and also gitignored.

```bash
cp schema.example.json schema.json
python -m coreyard.yms.discover_schema   # lists your tables, columns and row counts
```

Then edit `schema.json`, replacing each `PLACEHOLDER` with your own names. The mapping is
expressed as SQL fragments rather than a rigid model, because real installations differ in ways
a fixed model can't capture:

| Key | What it is |
|---|---|
| `select` | output field → SQL expression. `r_number`, `part_type` and `price` are required |
| `source` | the `FROM` clause plus any joins that resolve make/model/part-type names |
| `scope` | `WHERE` predicate choosing publishable rows (priced, in stock, not blocked) |
| `images_filter` | optional predicate for "this part has photos" |
| `interchange_*` | optional fitment-catalogue queries; omit if your system has none |
| `modified_at` | optional "when did this row last change" expression; enables `--delta` |
| `image_changes` | optional query for parts whose *photos* moved since `{since}` |
| `part_type_aliases` / `vehicle_details` | optional enrichment; see `COREYARD_ENRICH` |
| `donor_images` | optional: maps a donor vehicle to the folder key its photographs are filed under, so a part the yard never photographed can publish with pictures of the car it came off |

**Before adding a work-order exclusion to `scope`, check whether you need one.** Many yard
systems already decrement available quantity when a part is reserved, in which case the
quantity term covers it. Where they don't, note that line-item tables are often revisioned:
matching a line without pinning it to the order's *current* revision makes superseded rows
from long-closed orders read as live reservations, and quietly delists parts that are
genuinely for sale. `schema.example.json` spells out both traps.

**Driver quirks worth knowing**, because they shape how you write the expressions: impacket's TDS
parser mangles fixed-length `CHAR`/`NCHAR` columns and can truncate a result set, so wrap each in
`RTRIM(CAST(x AS varchar(n)))` and bits in `CAST(x AS int)`. SQL `NULL` arrives as the literal
string `'NULL'`, which the extract coerces away for you.

### 3. Configure `.env`
`./install.sh` creates it for you; otherwise `cp .env.example .env`. Fill in `YMS_DB_NAME`, the
`SMB_*` values, and the Shopify block — every one of them is required and has no default.
Every host, account, and credential is read from `.env` — nothing site-specific is baked into
the code. `SMB_SERVER_NAME` must be the database host's NetBIOS name; SMB session setup
needs it, not just the IP.

### 3b. Storefront policy and weights (optional, but read this)

CoreYard renders every listing's title, description, SEO metadata and tags. It cannot know
what is true at your yard, so its built-in wording claims only what the data supports: that a
part is used, OEM, and came off an inventoried donor vehicle. It does **not** say every part
was tested or inspected, because that is not true everywhere and a tool should not put a
claim in a seller's mouth.

If more is true at your yard, say so in a profile file and point `STORE_PROFILE_FILE` at it:

```json
{
  "condition": "Used, tested",
  "body_lead": "Genuine OEM {part_type}, removed from an inventoried donor vehicle and inspected.",
  "availability": "In stock and tested at {origin}.",
  "title_include_oem": false,
  "part_types": {"tail lamp": "Tail Light Lamp Assembly"},
  "preserved_tag_prefixes": ["ship:"],
  "audit": {"min_price": 5, "min_description_chars": 120}
}
```

Every key is optional and the file carries text, never HTML — CoreYard escapes what it is
given and owns the markup. The full list of keys is documented in `coreyard/profile.py`.

Two more file-backed settings, both optional and both documented in the module that reads
them:

| Setting | File | What it decides |
|---|---|---|
| `STORE_PROFILE_FILE` | your profile | what a listing may claim, part-type wording, the metafield namespace, audit thresholds |
| `STORE_WEIGHT_RULES_FILE` | your weight table | packed shipping weight per part type (`coreyard/transform/weights.py`) |
| `STORE_SHIPPING_POLICY_FILE` | your shipping policy | how each part ships, and the tag that says so (`coreyard/transform/shipping.py`) |
| `STORE_ORDER_POLICY_FILE` | your order policy | order tagging; fulfillment rules are derived from the shipping policy (`coreyard/orders/policy.py`) |
| `STORE_CATALOG_OVERRIDES_FILE` | reviewed catalogue decisions | per-R# SEO title overrides used by the canonical renderer |

Check all four against their schemas at any time, with no database, store or credentials:

```bash
bin/coreyard validate                                   # whatever this install configures
bin/coreyard validate --shipping /path/to/freight.json  # or one file, by path
```

That second form is how a storefront repository validates its own configuration in CI
against the backend's schemas, without either application importing the other.

Catalogue overrides use a small versioned JSON object keyed by the stable R#:

```json
{"version": 1, "parts": {"51": {"title": "Reviewed title"}}}
```

They are not an out-of-band Shopify patch. The canonical renderer reads them, both sinks
serialize the same result, and the fingerprint changes when a reviewed title does. Prices
are never accepted in this file; they come directly from the source database.

Without a weight table, products publish with no weight and every carrier-calculated rate is
quoted for an empty box, so supplying one is worth the hour it takes.

> **These files change what is published.** The profile's strings and the resolved weight are
> part of the rendered product, and the rendered product is what the sync fingerprints — so
> editing either makes the next run republish everything it covers. Check the count with
> `bin/coreyard --sink api --dry-run` first.

These policy switches live in `.env` rather than in a file:

* `STORE_REQUIRE_IMAGES=true` publishes only parts that have at least one photo. It needs an
  `images_filter` expression in `schema.json`, and it applies to sync, reconciliation, bulk
  publishing and the audit at once, so those four cannot disagree about what is listable.
  Off by default, so upgrading never silently changes which parts you list.
* `STORE_DONOR_PHOTO_LIMIT=6` caps how many of a donor vehicle's frames an unphotographed part
  inherits (see below).
* `STORE_PUBLICATIONS=Online Store` puts activated products on a sales channel. A product's
  status and its channel publication are different things in Shopify and only the second
  makes its URL resolve — an ACTIVE product on no channel is a 404 to shoppers and to Google.

### 4. Shopify Admin API token
Either path produces the `SHOPIFY_ADMIN_TOKEN` the sinks need, alongside
`SHOPIFY_STORE=your-store.myshopify.com`:

* **In-admin custom app** — Shopify admin → **Settings → Apps and sales channels → Develop apps
  → Create an app** → **Configuration → Admin API scopes**: `read_products, write_products,
  read_inventory, write_inventory, read_locations, read_files, write_files, read_orders` →
  **Save** →
  **API credentials → Install app**
  → copy the `shpat_…` token into `.env`.
* **Dev-dashboard app** (Client ID + Secret instead of a token) — add the redirect URL
  `http://localhost:3456/callback` to the app, set `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET`
  in `.env`, then run `bin/coreyard oauth` and approve the install. It writes
  `SHOPIFY_ADMIN_TOKEN` back into `.env`.

## Running

The day-to-day loop is four commands:

```bash
bin/coreyard doctor          # is this installation wired up, and are the jobs running?
bin/coreyard status          # what does the pipeline believe right now?
bin/coreyard sync --dry-run  # what would a sync do?
bin/coreyard sync            # do it
```

`bin/coreyard --help` lists every command; each one's `--help` describes its own flags and
says whether it writes. `python -m coreyard` is the same CLI, and `coreyard` is on your PATH
after `pip install -e .`.

```bash
# 0. Prove connectivity (DB, photo share, and Shopify if configured)
bin/coreyard doctor

# 1. Eyeball the extract
.venv/bin/python -m coreyard.yms.inventory 10   # first 10 priced-and-available parts

# 2. Preview: first 25 parts to a CSV you can eyeball / import to a test store
bin/coreyard sync --sink csv --limit 25 --dry-run

# 3. Incremental run (diffs against coreyard_sync_state.sqlite3)
bin/coreyard sync --dry-run      # report the diff, write nothing
bin/coreyard sync                # publish adds/changes, retire sold parts
bin/coreyard sync --deep         # ...and ask the live store, rather than the snapshot

# 3b. Catch-up run: only what changed since the last cursor (seconds, not minutes)
bin/coreyard sync delta --dry-run
bin/coreyard sync delta

# 3c. One kind of change at a time, or one part
bin/coreyard sync inventory      # availability: new, revived, repriced, restocked, retired
bin/coreyard sync photos         # only parts whose photo set moved on the share
bin/coreyard sync catalog        # only parts whose rendered copy moved
bin/coreyard sync --r-number 51 --dry-run

# 4. Bulk catalog load — photos, fitment, SEO text, weights, inventory; DRAFT products
bin/coreyard bulk --limit 50 --workers 4
bin/coreyard bulk                # everything the listable policy allows

# 5. Ask the store what it actually holds, rather than trusting the state file
bin/coreyard reconcile           # plan only
bin/coreyard audit catalog       # read-only listing-quality report
```

### Listing-portal workflow

The optional `ebay` command manages a configured listing portal without hardcoding that
portal's protocol. Copy `portal.example.json` to an ignored local file, fill in its routes,
fields, statuses and action IDs, then point `EBAY_PORTAL_FILE` at it. Authentication may come
from an explicit cookie header or an existing local Firefox session; cached cookies and all
portal checkpoints stay under ignored, owner-only local paths.

A borrowed browser session is shorter-lived than the runs that use it — an overnight
maintenance pass can outlast it. Setting
`EBAY_PORTAL_USER`/`EBAY_PORTAL_PASSWORD` and mapping `auth.login_fields` in `portal.json`
lets CoreYard establish a session of its own. It is a fallback of last resort, consulted only
when every cookie source has come up empty, and the client re-signs at most once per expired
request so a wrong password fails fast rather than hammering the form. The password is never
printed, logged, or included in an error message.

```bash
bin/coreyard ebay pull --tab unlisted --part-type engine \
  --out out/ebay-engines.json
bin/coreyard ebay details out/ebay-engines.json \
  --out out/ebay-engine-details.json
bin/coreyard ebay titles out/ebay-engines.json \
  --details out/ebay-engine-details.json
bin/coreyard ebay apply --titles out/ebay-engine-titles.json   # dry run
```

Title plans are optional reviewed inputs. Prices are not: Shopify and the portal receive the
exact positive price on the matched source part. There is no price override file or
second pricing rule in CoreYard.

Item specifics are a separate pass, because eBay ranks on aspect *match* and buyers filter
on it:

```bash
bin/coreyard ebay aspects --part-type engine --deep      # dry run + coverage report
bin/coreyard ebay aspects --part-type engine --apply
```

Values are validated against eBay's own category metadata (`EBAY_ASPECTS_FILE`); anything
not derivable with confidence is left unset, and `--apply` refuses without that metadata.

`daily` is the whole chain as one unattended pass, walking the part types the yard extract
reports rather than a hand-kept list:

```bash
bin/coreyard ebay daily              # titles + exact source prices; dry run
bin/coreyard ebay daily --apply      # save them in the portal
```

It stops at the portal and never calls submit: an unattended job is exactly the thing that
must not close the review window.

Saving in the portal is not publishing. Three commands, in increasing order of consequence:

```bash
bin/coreyard ebay apply --apply     # stored values only; nothing a shopper sees moves
bin/coreyard ebay push --apply      # sends them to eBay; now they are live
bin/coreyard ebay delist --apply    # ENDS live listings — irreversible
bin/coreyard ebay undo <ids>        # put titles or prices back to the portal's defaults
```

Delisting cannot be undone: relisting mints a new eBay item id and loses the watchers and
the item ranking the old listing had. `delist` therefore resolves its target set live rather
than trusting a saved file, and refuses if the portal reports more records than it returned.
Every one of these writes is a dry run without `--apply`, and each refuses to touch more
listings than its `--cap` without `--yes-i-mean-it`.

### Scopes, and what they are not

`sync inventory`, `sync photos` and `sync catalog` are *selectors over one plan*, not three
engines. There is one extract, one renderer and one diff; a scope only decides which of the
parts that moved this run is allowed to act on. Each scope owns a projection of the same
rendered product, so a repriced part is an inventory change and a retitled one is not.

Two consequences worth knowing:

* **Only `sync inventory` (and a full `sync`) may retire.** A photo run has no evidence
  about whether a part is still in the yard, so it never archives anything.
* **A scoped or `--r-number` run never replaces the snapshot**, it only records what it
  actually published. Committing would mark every part it *declined* to publish as up to
  date, and the change it skipped would never be published by anything.

### Long runs and overlap

```bash
bin/coreyard --lock sync --timeout 45m sync
```

`--lock NAME` holds `out/.NAME.lock` for the run and exits quietly (status 0) if another run
already has it — a skipped tick is the intended outcome when a full sync is still going.
`--timeout` stops a long run cleanly, exiting 124. Both are global flags, so they go before
the command.

A run that is stopped this way **keeps the work it had already published**: progress is
banked into the snapshot every 100 products. Without that, a backlog larger than one timeout
window can never be worked off — each run publishes what it can, records nothing, and the
next starts again from the beginning.

### Reconciling, repairing, auditing

The incremental sync compares the yard against its own fingerprint snapshot. That is the
right trade for a run on a timer, and it is blind in one specific way: it never asks Shopify
what it actually holds. A part the snapshot calls "published" is invisible to every later
run, whether or not the product exists, is a draft, or ever reached a sales channel. Three
commands close that gap, and none of them writes without `--apply`.

```bash
bin/coreyard reconcile                       # plan: create / activate / revive / retire / publish
bin/coreyard reconcile --apply --activate    # promote listable drafts too
bin/coreyard reconcile --repair-state        # forget snapshot entries whose product is gone
```

Reconciliation compares the yard's listable parts with the store and reports five buckets —
listable parts with no product, listable drafts, archived parts that came back, ACTIVE
products no longer listable, and products on no sales channel — plus where the state file and
the store disagree. It keeps the sync's retirement guards: more than `--max-retire-fraction`
(default 10%) of the active catalogue is refused unless you pass `--force-retire`, because a
yard database that answered partially looks exactly like a yard that sold out.

```bash
bin/coreyard repair titles --dry-run         # what an older renderer left behind
bin/coreyard repair titles --apply
bin/coreyard repair tags   --dry-run         # e.g. vehicle tags with a doubled make
bin/coreyard repair seo --apply --limit 200
bin/coreyard repair weights --apply          # products published before weights were owned
bin/coreyard repair metafields --apply       # grade, mileage, condition, fitment
bin/coreyard repair all --dry-run
```

Repair asks the store what each product says, asks the canonical renderer what it should say,
and rewrites only the differences. It is idempotent, so a re-run finds only what still
differs and an interrupted run simply resumes — no progress file. Tag repair reports which
tags a rewrite would delete *before* it does, and never removes a tag another system owns.

```bash
bin/coreyard audit catalog                   # missing SKU, no photos, zero weight, …
bin/coreyard audit catalog --show 20 --json out/audit.json
```

### How a part ships, and the tag that says so

A storefront has to tell a shopper *before* checkout that a door is pickup-only, and it
reads that from a tag. Whoever writes that tag makes a promise: get it wrong and a
freight-only engine quotes free ground, or a shippable alternator refuses to ship.

Point `STORE_SHIPPING_POLICY_FILE` at a file describing your groups and CoreYard applies the
right tag during the publish that creates the product — not in a later pass, which is a
window in which a product is live, buyable, and wearing the wrong shipping or none:

```json
{
  "groups": {
    "PICKUP": {"tag": "ship:pickup-only", "price": null,     "label": "Pickup only",
               "match": ["door assembly", "glass"], "exclude": ["glass channel"],
               "fulfillment": "yard"},
    "A":      {"tag": "ship:freight-299", "price": "299.99", "label": "Freight",
               "match": ["engine assembly"],        "fulfillment": "yard"},
    "GROUND": {"tag": "ship:free",        "price": "0.00",   "label": "Free ground",
               "default": true,                     "fulfillment": "external"}
  },
  "match_order": ["PICKUP", "A"]
}
```

Groups are tried in `match_order`, first substring hit against the part type wins, and
exactly one group is the `default` that catches the rest. `exclude` names what a pattern would
otherwise swallow: substring matching cannot say *glass but not glass channel*, and the two
belong in different groups — one is a two-person lift, the other fits in an envelope. Ordering
cannot settle it either, because the group that should win is the `default`, and a default
carrying match patterns is refused for good reason. `fulfillment` says who ships it —
`external` means another system buys the label and will close the order out, which is what
stops `orders sync-status` fulfilling a parcel order before the shipping app has attached a
tracking number. Because the shipping policy already declares that, the order policy derives
its fulfillment rules from it rather than repeating them.

The tag is part of the rendered product, so it is fingerprinted like any other
shopper-visible field, and CoreYard then **owns that namespace**: a stale `ship:` tag from
whatever wrote them before is replaced rather than accumulated next to the new one. Two
shipping tags on one product and the storefront reads whichever it tests for first.

### Parts with no photographs of their own

A yard photographs the cars it buys and only some of the parts it pulls, so a large slice of
the catalogue is listable in every respect except that it has no picture. Map a `donor_images`
query in `schema.json` and those parts publish with photographs of the vehicle they came off.

It is opt-in and one-way. **A part that has its own photographs never consults the donor
folder**, so nothing already published moves; without the mapping an unphotographed part
publishes with no images exactly as before, and the extra share listing is skipped entirely.
A part inherits only the opening frames — the general views — because a donor shoot documents
a whole car (a median of 16 frames, sometimes 99) and was taken to record a vehicle, not to
sell one bracket off it. `STORE_DONOR_PHOTO_LIMIT` sets how many; the default is 6.

Each donor frame is uploaded to Shopify **once** and referenced by every part off that donor,
rather than staged per product — roughly seven parts come off each car, so the naive version
uploads the same photograph seven times.

The listing says whose picture it is. The profile's `donor_photo_note` renders above
everything else in the body — "Photographs show the donor vehicle (2016 Buick Encore (Stock
#251026)). This part was not photographed individually." — and the alt text describes the
vehicle rather than the part. That is a fact about the data rather than a claim about your
business, so CoreYard states it for you; reword it in your profile if you like, but a shopper
must not be left believing the picture is of the part.

### Structured product data

The catalogue is not only prose. Grade, mileage, condition and full fitment are structured
values a theme can render as a table or a spec row, so CoreYard publishes them as metafields
rather than leaving a storefront to parse them back out of a description:

| Metafield | Type | Value |
|---|---|---|
| `<ns>.grade` | `single_line_text_field` | the source system's condition grade |
| `<ns>.mileage` | `number_integer` | donor mileage |
| `<ns>.condition` | `single_line_text_field` | the profile's condition wording |
| `<ns>.fitment` | `json` | `[{"years","make","model","note","label"}, …]` |

`<ns>` is `metafield_namespace` from the profile, defaulting to `coreyard`. Each fitment row
carries a `label` that is exactly the vehicle tag CoreYard also emits, so a theme can link to
a tag-filtered collection without reverse-engineering one from a title.

A part with no catalogue fitment still fits the car it came off, so its donor vehicle is
emitted as a single row — otherwise anything built from fitment would silently lose every
part the interchange catalogue does not cover. Values the source did not supply are left out
rather than published empty, and a value that later disappears is deleted from the product
rather than left showing something untrue.

### Tags another system owns

`productSet` replaces the whole tag list, so publishing only CoreYard's tags would delete
whatever else is on the product. That matters because storefront systems really do own tags:
a theme that has to warn "this part is pickup only" before checkout reads it from one, and
losing it turns a warning into a dead checkout the shopper discovers at the end.

The rule is ownership. CoreYard owns what it generates, so a stale generated tag is replaced
rather than accumulated — that is what lets `repair tags` repair anything. Anything
*namespaced* (`ship:free`, `promo:winter` — any tag containing `:`) is by definition somebody
else's, because CoreYard never emits one, and it is carried through every update untouched.
Systems that write plain tags can be named with `STORE_PRESERVED_TAG_PREFIXES` or the
profile's `preserved_tag_prefixes`.

Photo alt text is written when a product's photos are first attached. To fill it in on photos
published before that existed:

```bash
bin/coreyard altfix --dry-run --limit 20   # report what would change
bin/coreyard altfix                        # apply to the whole catalogue
```

`write_files` is required for this (see the scope list above); without it Shopify rejects the
update with `ACCESS_DENIED`.

**Adding a scope does not upgrade an existing token.** Shopify grants scopes at install
time, so after changing the app's scope list you must re-authorize to mint a new token:

```bash
bin/coreyard oauth        # re-runs consent, rewrites SHOPIFY_ADMIN_TOKEN in .env
```

Check what a token actually carries with
`{ currentAppInstallation { accessScopes { handle } } }`.

### Online orders → a work order in the yard system

A paid storefront order has to reach the counter as something a puller can act on, and it has
to stop the part being sold again. CoreYard does both by booking the sale into the source
system — which already renders a work order in a printable form — and archiving the product.
It writes no document of its own: a second sheet of paper would be a copy of the customer's
details with its own retention problem, and a chance for the two to disagree about what sold.

```bash
bin/coreyard orders register --url https://yard.example.com/webhook  # ORDERS_PAID
bin/coreyard orders serve --write-orders
bin/coreyard orders status                      # recent deliveries
bin/coreyard orders retry --id <webhook-id>     # retry only failed stages
bin/coreyard orders replay out/test_order.json  # re-run, no store or signature needed
```

If the yard machine cannot accept an inbound connection — behind NAT, no port, no tunnel —
poll instead. It is a different transport onto the same pipeline, not a second one: every
order it finds goes through the same queue, the same worker, the same optional booking and
the same delisting, and the queue de-duplicates on order identity, so running both during a
migration cannot book anything twice.

```bash
bin/coreyard orders poll --check                # scopes and connectivity, writes nothing
bin/coreyard orders poll                        # since the stored cursor, else 2 days
bin/coreyard orders poll --since 2026-08-01
bin/coreyard orders poll --write-orders
```

Registration uses Shopify's `ORDERS_PAID` topic. The receiver also requires
`financial_status=paid` before printing, booking, or retiring anything. During migration an
older `ORDERS_CREATE` subscription is safe: unpaid deliveries are acknowledged and ignored,
and paid duplicates are de-duplicated by order identity as well as webhook delivery ID.
Remove the legacy subscription after confirming the paid subscription is active.

`serve` binds to `127.0.0.1:8787` by default — put TLS in front of it (a reverse proxy or a
tunnel); Shopify only delivers to `https://`. Every request is checked against the app's
signing secret with a constant-time compare, and an unsigned or mis-signed POST is refused
without explanation. Deliveries are queued by `X-Shopify-Webhook-Id`, so Shopify's
at-least-once retries can't print an order twice, and the receiver answers immediately rather
than holding the connection open while a printer warms up.

Sold parts are archived on Shopify as each paid order arrives, which closes the window where a
part stays buyable until the next timer tick. `--no-retire` leaves them on sale. Order payloads
carry a customer's name, phone and address, so no request body is logged and no copy is written
outside the queue. Successful payloads are securely erased immediately. Failed payloads remain
in the owner-only queue for seven days so only their failed booking or retirement stage can be
retried. Configure the window with `COREYARD_WEBHOOK_ERROR_RETENTION_DAYS`.

Run `orders serve` under a process supervisor with automatic restart, and keep it bound to
loopback behind an HTTPS reverse proxy or tunnel. Monitor `orders status` for error rows.
Cancellation/refund reversal is deliberately manual: restoring source inventory would be a
second database write operation whose invariants must be observed and reviewed separately.

### Booking the sale into the yard system

Optionally, the same delivery creates a real order in your source database, so the storefront
does not become a second set of books:

```bash
bin/coreyard orders serve --write-orders
.venv/bin/python -m coreyard.yms.orders out/test_order.json  # print SQL, write nothing
bin/coreyard orders status                           # deliveries and their order numbers
```

This is the **only** write CoreYard makes to your database. It requires an `order_write`
section in local `schema.json` plus explicit runtime enablement: `YMS_WRITE_ORDERS=1`,
`--write-orders`, or `--execute` for the manual command. Without the mapping and an explicit
opt-in it stays read-only, so an upgrade never starts writing to a system of record on its own.
Read the `_order_write` notes in `schema.example.json` before filling it in — most
systems of this kind have no identity columns and no foreign keys, so ids come from counter
tables the application is allocating from concurrently, and nothing implicit will fix a
mistake.

What CoreYard guarantees, whatever names you map: the order is one transaction under
`SET XACT_ABORT ON`, so it commits whole or changes nothing; ids are allocated in a single
`UPDATE ... OUTPUT` under `UPDLOCK` and checked unused before insert; the inventory decrement
carries a quantity floor and must affect exactly one row, so an oversell fails loudly instead
of going negative; and nothing is ever deleted. Because the write is atomic and idempotent,
retrying after a failure is safe.

A redelivery is refused by the database, not just by the queue: the storefront order reference
is stored on the order and a second attempt returns the existing number instead of booking the
sale twice. Booking and delisting fail independently: if the booking does not land, the part is
still taken off sale and the failure is recorded against the delivery in
`bin/coreyard orders status`. Retry it within the retention window with
`bin/coreyard orders retry --id <webhook-id>`, which reruns only the stage that failed.

**Tax is a decision you have to make explicitly.** If your storefront already collects and
remits sales tax, leave `line_items_taxable` false, or the same sale is taxed twice. Do not
assume your customer account can carry the exemption — check whether your customer table even
has such a column first. Booking online sales under the customer number your existing
e-commerce integration uses is worth doing for consistency, but it is not a tax mechanism.

### Telling the storefront what the yard did

The sale reaches the yard system as a work order, and what happens next — picked, invoiced,
shipped — happens there. The storefront never hears about it, so a buyer looking at their
account sees "unfulfilled" on an order that left the building yesterday.

```bash
bin/coreyard orders sync-status              # plan; writes nothing
bin/coreyard orders sync-status --apply
```

It reads an `order_status` query from your `schema.json` — which tables record an invoice,
and how a storefront order links back to one, are your system's business — tags the matching
Shopify order, and appends a note carrying the source reference so the two systems can be
reconciled by hand.

Whether it also **fulfills** the order is deliberately configuration, in
`STORE_ORDER_POLICY_FILE`. Shopify has no "picked but not shipped" state: creating a
fulfillment closes the fulfillment order. If a third-party shipping app is going to buy the
label and attach the tracking number, it needs that left open, and fulfilling early costs the
buyer their tracking email. If nothing else will ever touch the order — a freight shipment
booked by phone, a counter pickup — then leaving it open means it stays unfulfilled forever.
So the site describes its own arrangements, keyed on the product tags its storefront already
uses, and an order with any line another system will ship is left alone. With no policy file,
nothing is ever fulfilled and the command only tags and notes.

### Taking sold parts off sale

A part leaves the listing when it drops out of your `schema.json` `scope` predicate — sold,
unpriced, blocked, or on an open work order. `--sink api` then zeroes its inventory and sets it
to `ARCHIVED`. It is never deleted: the product, its photos and its URL stay put, so an old link
or a search result lands on a real page rather than a 404, and the part can be revived.

Revival is automatic. A part can come back — a voided work order returns it to the yard — so the
status a product held before it was archived is recorded in the sync state, and a later run puts
it back exactly as it was. Without that memory the status read-back that stops a sync from
un-publishing a live product would re-send `ARCHIVED`, quietly restoring the part's stock onto a
product no shopper can see. Only archived products are revived, so a status you set by hand
always stands.

Scope is where the delisting rule lives, so put the work-order exclusion there — a part being
pulled for a customer is still priced and still in stock, and without that clause it stays
buyable and can be sold twice. `schema.example.json` has the `NOT EXISTS` pattern under `_scope`.

Retirement is guarded, because "missing from the extract" is also what a half-finished run or a
broken join looks like:

| Guard | Behavior |
|---|---|
| `--limit` is set | never retires — the run only saw part of the yard |
| removals > `--max-retire-fraction` (default 10%) | refuses, and says so; override with `--force-retire` |
| a retire call fails | logged and skipped; the R# stays in the state file and is retried next run |

Anything not retired is carried forward in the snapshot at its old fingerprint, so a refused
batch is re-detected rather than silently forgotten.

Photo changes are picked up the same way: the share is listed once per run and folded into the
fingerprint, so a part that gains or loses a photo is republished with its media rebuilt. Pass
`--no-image-scan` to skip that listing if you don't need it.

`shopify_bulk` is the path to use for the initial catalog load. It is resumable: every attempt
is appended to `out/shopify_bulk_results.jsonl`, and a re-run skips R#s already logged `ok`, so
transient upload failures simply retry on the next run. Note it tracks progress in that log,
**not** in `coreyard_sync_state.sqlite3` — the two publish paths do not share state.

Re-running is safe either way: products are keyed by the stable handle
`<SHOPIFY_HANDLE_PREFIX>-<R#>` and upserted with `productSet`, and photos are uploaded only when
the product has none.

> **Choose `SHOPIFY_HANDLE_PREFIX` once.** The handle is the storefront's primary key — it is how
> every later run finds the product it already published. Changing it on a live store makes every
> product look new and duplicates the whole catalog. If you are migrating an existing store, set
> the prefix to whatever your products already use.

Inspect any part's photos directly:
```bash
bin/coreyard images 51 --fetch
```
Set `COREYARD_SMB_DEBUG=1` to trace the SMB pipe traffic when a connection misbehaves.

### Catch-up runs (`--delta`)

A full sync reads every part to discover the few dozen that moved, which is why it can only
run hourly — and that hour is the window where a part sold at the counter (or on another
sales channel) stays buyable on the storefront. `--delta` asks the cheaper question instead:

```bash
bin/coreyard --sink api --delta --dry-run   # what would change
bin/coreyard --sink api --delta             # publish it
```

It needs `modified_at` in `schema.json` (plus `image_changes`, if photographing a part does
not touch the part row — it usually doesn't). Measured against a live 26k-part yard, a
24-hour window resolves in well under a second, so this is affordable every few minutes.

Two properties make it safe to run unattended:

* **Retirement is evidence-based.** A full run infers "sold" from absence, which is why it
  needs the fraction guard — a broken query also produces absence. A delta run never reasons
  from absence: it retires a part only when it has the row in hand and that row says
  out-of-scope. It also refuses to retire more than `--max-retire-fraction` in one pass.
* **The cursor is the source server's clock**, read before the query and stored with a
  deliberate overlap, so a write committed mid-run is re-examined next time rather than
  skipped. Re-examining costs nothing — publishing is idempotent.

A delta run is a catch-up, not a replacement. Only a full sync sets the cursor, because only
a full sync has just reconciled the whole yard; keep both scheduled.

## Ongoing sync (scheduled)

State in `coreyard_sync_state.sqlite3` makes the sync incremental: only new/changed parts are
pushed, and parts no longer priced-and-available are retired. Use the scheduling helper
(systemd when available, cron fallback):

```bash
bin/coreyard schedule install --every 1h                      # the full extract
bin/coreyard schedule install --every 5m --task "sync delta"  # the cheap catch-up
bin/coreyard schedule status
```

A typical installation runs three cadences, each stopping itself rather than relying on the
scheduler to do it:

| When | Command | Why |
|---|---|---|
| every 5 min | `coreyard --lock sync --timeout 4m sync delta` | closes the window where a part sold at the counter stays buyable online |
| hourly | `coreyard --lock sync --timeout 45m sync` | the full extract |
| daily | `coreyard --lock sync --timeout 45m sync --deep` | the only run that asks the store what it actually holds |
| every 1 min | `coreyard --lock counts counts --apply` | refreshes the exact source-inventory count shown by the theme; edits no products |

The delta job shares the full sync's lock on purpose: if a full run is in progress it already
supersedes the catch-up, so the tick is skipped rather than raced.

Whatever schedules them, the jobs should invoke `bin/coreyard`. The old `python -m
coreyard.<module>` entry points still work and are still tested, but the launcher is the
surface `coreyard --help` and the CLI's own tests both walk — so a renamed entry point breaks
a test rather than a production job.

`coreyard counts` is dry-run by default. It uses one `COUNT_BIG(DISTINCT ...)` query over the
configured saleable source scope and intentionally ignores the storefront-only photo gate;
`coreyard counts --apply` stores that result and its observation time as shop
metafields. Shopify Liquid supplies the separate listed-online count from the `all` collection
on each page render. The source database and its credentials are never exposed to a browser.

## Data model → Shopify mapping

Both sinks serialize the same `RenderedProduct`, so this table describes one rendering, not
two.

| Shopify field | Source (`Part`) |
|---|---|
| Handle | `<SHOPIFY_HANDLE_PREFIX>-<R#>` (stable and unique) |
| Title | multi-model SEO title from resolved fitment, ≤255 with a word-boundary trim: years spanned (`1998-2000`), each make named once without commas, finish and condition stated only from words recognised in the yard's own note |
| Body (HTML) | fitment/"Fits" list, donor detail, condition, interchange #, mileage, warranty, stock #, R# |
| SEO title / description | `coreyard/transform/seo.py`, under the site's profile |
| Type / Tags | expanded part type; year/make/model/interchange/spec terms/profile tags |
| Variant SKU | R# (`r_number`; unique and never reused) |
| Variant Price | exact source price, formatted to cents (parts with no positive price are skipped) |
| Variant Inventory Qty / Policy | quantity / `deny` (unique parts don't oversell) |
| Shipping weight | the site's weight table by part type, or a source weight if the yard records one |
| Shipping tag | the site's shipping policy, classified by part type during publish |
| Metafields | grade, mileage, condition, and structured fitment, in the profile's namespace |
| Product media | `{R#}_NN.jpg` from the photo share, uploaded via staged uploads (API path) or referenced by URL (CSV path); a part with none of its own falls back to its donor vehicle's frames when `donor_images` is mapped |
| Alt text | generated per photo, and covered by the fingerprint |

Listing scope comes from your mapping's `scope` predicate — typically priced, in stock,
and not blocked from online sale — plus `STORE_REQUIRE_IMAGES` if you set it. That one
definition is shared by sync, reconciliation, bulk publishing and the audit, so they cannot
drift into publishing and archiving the same part in turn.
Identifier mapping is intentionally explicit — these are the source system's own column
names, which you supply in local configuration (see "Schema mapping"):

| Business label | schema field | Example | Use |
|---|---|---|---|
| R# | `r_number` | `51` | Shopify SKU and unique product identity |
| Stock # | `stock_number` | `251026` | Donor vehicle identifier; shared by its parts |
| Interchange # | `interchange_number` | `545-01883` | Customer-facing interchange identifier |
| Fitment key | `interchange_code` | `01883` | Internal lookup only; never shown as the full interchange # |

## Testing
```bash
python -m unittest discover -s tests -v
python scripts/check_neutrality.py
```
The suite is offline by design — no database, no network, no Shopify, no `.env` — and
anything that needs the live server belongs in `scripts/` or behind a CLI flag. It covers the
canonical renderer and fingerprint sensitivity, tag ownership, shipping classification,
structured metafields and their staleness, weight rules, the listable photo policy, the
donor-photo fallback and the two resolvers agreeing on how an
unchanged photo is spelled, external config validation, reconciliation and its retirement
guards, catalog repair, order polling and normalization, the order lifecycle policy, the
Shopify client, retirement and revival, listing-to-part resolution, and the extract mapping.

## Security notes
- Secrets live only in `.env` (gitignored). The SMB password is written to a `0600` temp
  auth file for `smbclient`, never passed on the command line.
- Extraction and discovery issue `SELECT` only. The separately gated order-booking transaction
  in `coreyard/yms/orders.py` is the sole source-database write path.
- No hostnames, IP addresses, or account names appear in this repository; they all come from
  your local `.env`.

## Contributing

Bug reports and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The one rule
CI enforces automatically is vendor neutrality: no product names and no real table or column
names in the tree. Run `python scripts/check_neutrality.py` before opening a PR.

For security issues, please follow [SECURITY.md](SECURITY.md) rather than opening a public issue.

## License

[MIT](LICENSE) © Luxorium.

CoreYard is an independent tool and is not affiliated with, endorsed by, or a product of any
yard management software vendor. It reads a database you already license and operate; optional
storefront order booking is explicit, guarded, and off by default.
