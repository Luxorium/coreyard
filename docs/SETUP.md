# Installing and configuring CoreYard

Everything between a clone and a first real sync: installing, giving CoreYard read access to
the yard's database and photo share, mapping that database to the fields CoreYard renders
from, and issuing a Shopify token.

Nothing here is guesswork you have to do alone — `coreyard init` asks these questions
interactively, and `coreyard doctor` tells you which answers are still missing. This document
is what those two commands are asking about, for when you want to know why.

[← back to the README](../README.md)

## Install

```bash
git clone https://github.com/luxorium/coreyard.git
cd coreyard
./install.sh
```

That's the whole thing. The installer detects your package manager (apt, dnf, yum, pacman,
zypper, or apk), installs Python 3.10+ and `smbclient`, builds a virtualenv, installs CoreYard
into it, writes a `bin/coreyard` launcher, offers to put `coreyard` on your PATH, and verifies
the result by running the offline test suite. It asks before touching anything with `sudo`.

```bash
./install.sh --no-deps    # system packages already present
./install.sh --yes        # never prompt (for scripted installs)
./install.sh --home DIR   # where this installation's own files live
./install.sh --help
```

**Code and data are separate.** The checkout holds the code; configuration, state, locks and
output live under the *data home*. That is the checkout itself when it already holds an
installation — so re-running the installer on a running yard moves nothing — and
`$XDG_DATA_HOME/coreyard` otherwise. Give each installation its own `--home` to run several on
one host.

The installer does **not** write `.env`; `coreyard init` does, and it refuses to overwrite an
existing one. Run the installer, then `coreyard init`.

If your distro ships Python with `ensurepip` stripped out, the installer detects the failed
`venv` creation and bootstraps `pip` itself — no manual workaround needed.

Only two Python dependencies exist, and each belongs to one capability:

| Needed for | Dependency | Without it |
|---|---|---|
| A database source | `impacket` (Python) | a tabular source (CSV/SQLite) still works |
| Shopify — every sink, order poll and diagnostic | `requests` (Python) | the CSV sink still works |
| Photos from an SMB share | `smbclient` (your distro's Samba client) | a local photo directory still works |
| Rendering, diffing, the CSV sink, the whole transform | — | nothing third-party is involved |
| Mirroring images to an S3-compatible bucket | `boto3`, optional and not installed | the default publishes Shopify-hosted media |

`coreyard doctor` reports each of these as a capability rather than as a package, so a missing
system tool reads as the feature it disables. The two Python dependencies pull roughly twenty
more of their own; `scripts/dependencies.py --licences` lists the closure as installed, and
`requirements-lock.txt` records the set this release was qualified against.

### Manual install

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/coreyard init
```
You will also need `smbclient` from your distro's Samba client package. A non-editable
`pip install .` works too — the wheel carries the example yard and both config templates — but
from a checkout prefer `-e`, because a copy into `site-packages` moves this installation's data
home to `$XDG_DATA_HOME/coreyard` unless you set `COREYARD_HOME` yourself.

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
coreyard init --write-schema   # copies the bundled template to <data home>/schema.json
coreyard schema                # lists your tables, columns and row counts
```

The template ships inside the package rather than at the top of the checkout, so this works
whether you cloned CoreYard or installed it. It is a separate command rather than part of
`init` on purpose: the template is full of `_TABLE`/`_COLUMN` placeholders, and a
`schema.json` created
without being asked for looks like a configured installation right up until it fails with a
SQL error naming a table you do not have. `--write-schema` tells you how many placeholders
are left.

Then edit `schema.json`, replacing every name ending in `_TABLE` or `_COLUMN` with your own.
The mapping is
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
`coreyard init` writes it (mode 600) and asks for what it needs; `.env.example` in the checkout
is the full reference if you would rather write one by hand. Fill in `YMS_DB_NAME`, the
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
  inherits (see [donor photos](OPERATIONS.md#parts-with-no-photographs-of-their-own)).
  Set `0` to use all available donor photos.
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
