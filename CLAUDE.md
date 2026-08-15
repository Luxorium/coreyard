# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**CoreYard** ([coreyard.luxorium.dev](https://coreyard.luxorium.dev)) — a one-way translator
that publishes a salvage yard's parts inventory to Shopify, reading an existing yard management
system's SQL Server database and its part-photo SMB share. The Python package is `coreyard`.

This is a **generic tool for any yard running a compatible system + Shopify**, not a one-site
script. Nothing
identifying an installation — host, account, credential, business name, city, storefront copy,
handle prefix — may be hardcoded in source or docs. It all comes from `.env` via
`config.StoreProfile` / `load_settings()`. Treat a literal business name or IP in a diff as a bug.

**Vendor neutrality is a project rule.** Do not name the yard management software, its vendor,
or its parent company anywhere in this repository — not in code, comments, docs, or commit
messages. Refer to "the yard system" / "the source database". `scripts/check_neutrality.py`
enforces this in CI, over `git ls-files`, and also bans the operating yard's own business name.

**Read-only is a discipline, not a permission boundary.** Verified 2026-08-15: the service
account is `db_owner`, mapped to `dbo`, holding INSERT/UPDATE/DELETE/ALTER/CONTROL on the
source database. Nothing at the database level would stop a write — which is exactly why the
`SELECT`-only rule lives in the code and has to be kept there deliberately. A loop bug in this
repo has the privileges to destroy the system of record for the whole business. Any future
write must be a specific, reviewed, transaction-wrapped operation against a schema whose
invariants have been *observed*, never inferred.

## Commands

Use the repo venv (`.venv/bin/python`, Python 3.13) — `impacket` and `requests` live there.

```bash
./install.sh --no-deps --yes                                  # rebuild venv + bin/coreyard, verify
python -m unittest discover -s tests -v                       # full suite (25 tests, offline)
python -m unittest tests.test_transform.Rows.test_no_images   # a single test
python scripts/demo_offline.py                                # transform + real SMB photos -> CSV, no DB
python -m coreyard.run_sync --check                                # DB (and Shopify) connectivity
python -m coreyard.run_sync --sink csv --limit 25 --dry-run        # preview without committing state
python -m coreyard.run_sync --sink api                             # push new/changed parts to Shopify
python -m coreyard.yms.inventory 10                          # print first 10 extracted parts
python -m coreyard.yms.images 51 --fetch                     # list/fetch one part's photos
python -m coreyard.yms.discover_schema                       # re-dump schema to out/schema/
python -m coreyard.sink.shopify_bulk --limit 50 --workers 4        # resumable bulk publish (DRAFT)
python -m coreyard.sink.shopify_oauth                              # exchange client id/secret -> admin token
python -m coreyard.sink.backfill_alt --dry-run --limit 20      # preview photo alt-text backfill
COREYARD_SMB_DEBUG=1 python -m coreyard.run_sync --check                # trace the SMB/TDS pipe
```

Tests must stay offline; anything needing the live server belongs in `scripts/` or a CLI flag.
There is no build, formatter, or linter.

## Architecture

`Part` (`coreyard/models.py`) is the single contract between extract and publish. Extract owns all
source-schema knowledge; nothing downstream may reference a database column. This is what
lets transform/CSV/state be unit-tested with synthetic parts.

`StoreProfile` (`coreyard/config.py`) is the matching contract for the *installation*: vendor,
city, warranty, handle prefix. It threads through the whole render path (`shopify_product`,
`seo`, `shopify_csv`, `state`, both sinks) so no customer-facing string is hardcoded. Every
field defaults to empty and each clause drops out when blank, which is why tests can render
without a `.env`.

```
coreyard/yms/        extract    inventory.py (the ONLY module that knows the schema)
                           db.py -> smb_tds.py (TDS over an SMB named pipe)
                           interchange.py (fitment), images.py (photos via smbclient)
coreyard/transform/  render     seo.py (API path), shopify_product.py + shopify_csv.py (CSV path)
coreyard/sink/       publish    csv_sink.py | shopify_api.py -> shopify_write.py -> shopify_bulk.py
coreyard/state.py    diff       SQLite R# -> fingerprint, drives added/changed/unchanged/removed
coreyard/run_sync.py CLI        orchestrates extract -> fingerprint diff -> sink
```

**Two publish paths exist. They now share a publisher but not their progress.**
- `run_sync --sink csv|api` is the incremental path: it diffs against
  `coreyard_sync_state.sqlite3` and only acts on adds/changes/removals. `--sink api` drives
  `shopify_write.ShopifyPublisher`, so it gets the same fitment, SEO, photos and inventory
  quantities as a bulk run — plus sold-part retirement, which only this path does.
- `sink/shopify_bulk.py` -> the same `ShopifyPublisher` is the catalog-loading path: bounded
  concurrency, `--limit`, and resume from `out/shopify_bulk_results.jsonl` (last
  `status == "ok"` per R#), **not** from the SQLite state, so the two do not share progress.
- `--sink csv` still renders through `transform/shopify_product.py` (plain titles/bodies) and
  cannot retire anything; it is the offline preview path.

Both paths key products by the same handle `<prefix>-<R#>` (`shopify_product.handle_for`) and use
`productSet` for idempotent upsert, so re-running updates rather than duplicates. Photos are
attached only when the product has none, so re-runs don't duplicate images; `publish(part,
refresh_images=True)` is the deliberate exception, and `run_sync` passes it only for the R#s
`diff.image_changed` names.

**`productSet` has set semantics, so an omitted field is reset, not left alone.** `_upsert`
reads the existing product's `status` back and re-sends it for exactly this reason — otherwise
every sync would drag a product the owner activated by hand back to DRAFT. New products get
`--status` (default DRAFT). Retirement is the one intentional status write: qty 0, then
ARCHIVED, so "not reviewed yet" (DRAFT) and "sold" stay distinguishable in Admin.

**Retirement is guarded, because "missing from the extract" has innocent causes.**
`run_sync._retirement_plan` refuses to retire under `--limit` (a partial view of the yard) or
when removals exceed `--max-retire-fraction` (default 10%, override with `--force-retire`).
Anything not retired is carried forward in the state snapshot at its old fingerprint, so it is
re-detected next run instead of being silently forgotten. `tests/test_retire.py` pins all of it.

Delisting a part that is being sold is a **schema** concern, not a code one: exclude open
work orders in the `scope` predicate of `schema.json` (see `schema.example.json`, `_scope`).
Dropping out of scope is what makes CoreYard retire the listing.

`webhook.py` closes the same loop in seconds instead of a timer interval: Shopify posts
`orders/create`, the receiver verifies the HMAC and queues, and a worker prints a pull ticket
and archives the sold products. It touches the source database only to *read* the bin location
and donor vehicle for the ticket. Two constraints shape it — Shopify wants a 2xx inside ~5s
(hence verify-queue-ack, work on a thread) and delivers at-least-once (hence the
`X-Shopify-Webhook-Id` primary key). Order payloads are customer PII, so the queue clears the
payload column once handled and nothing logs a body.

The prefix comes from `StoreProfile.handle_prefix` (`SHOPIFY_HANDLE_PREFIX`, default
`coreyard`). It is a live storefront key, not a namespace: for any store that has already
published, changing it makes every product look new and duplicates the catalog. Never change
the default or an installation's configured value. `tests/test_transform.py` pins the behavior.

`state.product_fingerprint` hashes the `shopify_product.primary_row` output, the ordered
image list and `interchange_code` — so changing anything in `shopify_product.py` or the image
resolver invalidates every fingerprint and makes the next run look like a full rewrite. Weigh
that before editing those. `state.image_fingerprint` is tracked alongside it in its own column
so a run can tell a text edit (cheap re-upsert) from a photo change (tear down and re-upload
the media). Rows written before that column existed read as *unknown*, not *changed*, so the
upgrade run doesn't re-upload the whole catalog.

The image resolver lists the **whole share once per run** (`SmbImageStore.list_all_inventory_images`,
~0.3 s for 35k photos) rather than once per part (26k round trips, ~54 min). This is the one
place that enumerates the photo folder, and the reason is that "which parts gained a photo?"
cannot be answered any other way. `--no-image-scan` skips it.

## Identifier semantics (the easiest thing to get wrong)

| Business label | schema field | Example | Role |
|---|---|---|---|
| R# | `r_number` | `51` | unique, never reused → SKU, handle, photo filename stem, state key |
| Stock # | `stock_number` | `251026` | donor vehicle; **shared by many parts** — never an identity |
| Interchange # | `interchange_number` | `545-01883` | customer-facing |
| Fitment key | `interchange_code` | `01883` | internal lookup only; never shown as the interchange # |

`tests/test_transform.py` and `tests/test_inventory.py` pin these; keep them passing.
Customer-facing text must never name the interchange data vendor (there's a test for that).

## Live-server constraints

- **SQL reaches the server only over an SMB named pipe.** The database host exposes no SQL TCP
  port and SMB1 is off, so `smb_tds.py` opens `\sql\query` with impacket's SMB2/3 client and
  runs impacket's TDS engine over a socket shim. Auth is Windows/NTLM using the **SMB**
  credentials (`SMB_USER`/`SMB_PASSWORD`, the existing service account); the DB password is unused,
  and no SQL login exists on the installed system. A normal TCP driver will not work here.
- **impacket's TDS quirks shape every query.** Fixed-length CHAR/NCHAR columns come back as
  bytes and can truncate a result set, so wrap each in `RTRIM(CAST(x AS varchar(n)))` and bits
  in `CAST(x AS int)`. SQL `NULL` arrives as the literal string `'NULL'` — always run values
  through `_clean`/`_to_int`/`_to_decimal` in `inventory.py`.
- **Listing scope** comes from the site's `schema.json` `scope` predicate, with
  `Part.is_listable()` as a second guard.
  an anchored `{R#}_*` mask (never enumerate the directory — it holds tens of thousands of
  files). The SMB password goes into a `0600` temp auth file, never onto the command line.

## Configuration

`coreyard/config.py` parses `.env` itself (no python-dotenv) and never overrides real environment
variables. `.env` is gitignored and holds the only secrets; `.env.example` documents the keys.
`out/`, `coreyard_sync_state.sqlite3`, and fetched images are generated — don't commit them.

## Docs

`README.md`, `requirements.txt`, and `.env.example` were reconciled with the code on
2026-08-14 (they previously described a TCP SQL port, an `abm_readonly` login, `python-tds`,
and an unfilled `inventory.py` template). Keep them that way when the transport, the extract
scope, or the publish paths change. `AGENTS.md` holds the same repo conventions in shorter
form; keep the two consistent.

Note `coreyard/config.py` does not strip trailing comments from `.env` lines, so keep comments on
their own lines in `.env`/`.env.example`.

Config keys are interface. `config._LEGACY_KEYS` maps renamed keys to their old names so an
existing `.env` keeps working across an upgrade, with a one-time note on stderr. Read config
through `config._get`, never `os.environ` directly, or the fallback is bypassed. Add to that
map when renaming a key; don't do a hard cutover — a deployed installation would fail at the
next timer tick with nothing but "missing required config" to go on.

**Admin API drift.** `productCreateMedia`, `productUpdateMedia` and `productDeleteMedia` no
longer exist in the 2026-07 Admin API. Photos are attached by passing `files` (`FileSetInput`)
to `productSet`, alt text on existing media is changed with `fileUpdate`, and media are
removed with `fileDelete` (product media are Files now). All need the `write_files` scope — a
token without it fails with `ACCESS_DENIED`. `InventorySetQuantitiesInput` has lost
`ignoreCompareQuantity`; omitting `changeFromQuantity` per item is the unconditional set.
**Introspect before assuming a mutation or field still exists** — this API sheds them steadily,
and the introspection query in `sink/shopify_api.py`'s client makes it a 5-second check.
