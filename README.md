# CoreYard

[![CI](https://github.com/luxorium/coreyard/actions/workflows/ci.yml/badge.svg)](https://github.com/luxorium/coreyard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

A [Luxorium](https://luxorium.dev) project

**CoreYard publishes a salvage yard's parts inventory to Shopify and keeps it in step** —
products, photos, fitment, weights and inventory levels. It reads an existing yard management
system's SQL Server database and its part-photo file share, both over SMB, because the
database host commonly exposes no SQL TCP port.

It is a self-hosted backend integration engine, not a storefront: extraction, product rendering, catalog
synchronization, orders, reconciliation, catalog repair and operational automation. Everything
specific to a site — storefront design, business policy, hosts, credentials, schema, copy —
is supplied through local configuration, never hardcoded here.

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

## Install

```bash
git clone https://github.com/luxorium/coreyard.git
cd coreyard
./install.sh
```

The installer sets up Python and `smbclient`, builds a private virtualenv, installs CoreYard
into it, offers to put `coreyard` on your PATH, and runs the offline test suite to prove the
install. Then `coreyard init` asks what it needs and `coreyard doctor` says what is still
missing. Code stays in the checkout; configuration, state and output live under a separate
data home the installer names.
[Full setup instructions](docs/SETUP.md) — database access, schema mapping, storefront
policy and the Shopify token.

## Status

CoreYard runs a real yard: a live catalogue of roughly 9,400 image-backed parts, with orders
booked back into the yard's own system. That is one installation, and one installation is not
a release.

What is supported, per source and per feature, is in the **[capability
matrix](docs/CAPABILITY_MATRIX.md)** — including the limitations and, honestly, which rows
have no evidence beyond an offline test. What has actually been verified is in the
**[evidence ledger](docs/EVIDENCE_LEDGER.md)**. What must be true before a numbered release
is the **[v0.1.0 acceptance criteria](RELEASE_ACCEPTANCE_v0.1.0.md)**. Those three documents
disagree with nothing: the matrix says what is claimed, the ledger says what is proven, and
the gap between them is the work left.

The supported product is **source database → Shopify → sales**. There is no marketplace
integration in this repository.

Known gaps: a part's *variant-level* media assignment is not managed — photos attach to the
product, not to a specific variant, which is fine while every part is a single-variant product.

## Documentation

| | |
|---|---|
| [Setup](docs/SETUP.md) | Install, database access, schema mapping, storefront policy, Shopify token |
| [Operations](docs/OPERATIONS.md) | The daily loop, sync modes, reconcile and repair, storefront links, orders, scheduling |
| [Architecture](docs/ARCHITECTURE.md) | Repo layout, the canonical rendering, data-model mapping |
| [Adding a yard system](docs/SOURCES.md) | The source contract: what an adapter must do, and what it may decline |
| [Capability matrix](docs/CAPABILITY_MATRIX.md) | What is supported per source and feature, with limitations |
| [Command inventory](docs/INVENTORY.md) | Every command, generated from the live command tree |
| [Evidence ledger](docs/EVIDENCE_LEDGER.md) | What has been verified, and what has not |
| [Contributing](CONTRIBUTING.md) | How to work on CoreYard |

## Security notes
- Secrets live only in `.env` (gitignored). The SMB password is written to a `0600` temp
  auth file for `smbclient`, never passed on the command line.
- Extraction and discovery issue `SELECT` only. The separately gated order-booking transaction
  in `coreyard/yms/orders.py` is the sole source-database write path.
- No hostnames, IP addresses, or account names appear in this repository; they all come from
  your local `.env`.

## Support

Bugs and feature requests go to [GitHub
issues](https://github.com/luxorium/coreyard/issues); anything that does not suit a public
issue can go to **parts@abmotorsla.com**. Security reports have their own route —
see [SECURITY.md](SECURITY.md). CoreYard is maintained by people who also run a yard, so
replies are best-effort rather than same-day.

## Contributing

Bug reports and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The one
rule CI enforces automatically is vendor neutrality: no product names and no real table or
column names in the tree. Run `python scripts/check_neutrality.py` before opening a PR.

For security issues, please follow [SECURITY.md](SECURITY.md) rather than opening a public
issue.

## License

[MIT](LICENSE) © Luxorium.

CoreYard is an independent tool and is not affiliated with, endorsed by, or a product of any
yard management software vendor. It reads a database you already license and operate; optional
storefront order booking is explicit, guarded, and off by default.
