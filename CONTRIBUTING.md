# Contributing to CoreYard

Thanks for taking a look. CoreYard publishes a salvage yard's parts inventory to Shopify,
reading a yard management system's own SQL database. It runs a real yard, so the bar for
changes is set by what a mistake costs: a duplicated catalogue, a part sold twice, a product
on sale with no photographs.

Contributions are genuinely welcome, including from people who have never seen a salvage
yard. Much of the codebase — rendering, SEO, state diffing, CSV output, configuration
validation — is unit-tested against synthetic parts and needs no database at all.

## Where to ask

Open a [GitHub issue](https://github.com/luxorium/coreyard/issues) for bugs and feature
requests, or email **parts@abmotorsla.com** if GitHub does not suit. Security issues go
through [SECURITY.md](SECURITY.md) instead — please do not open a public issue for those.

This is a small project maintained by people who also run a yard, so replies are best-effort
rather than same-day.

## Getting set up

```bash
git clone <your fork>
cd coreyard
./install.sh --no-deps        # or plain ./install.sh to pull system packages too
.venv/bin/python -m unittest discover -t . -s tests -v
```

You do not need a live database, a Shopify store, or credentials. To see the whole pipeline
run end to end on a bundled seven-part example yard:

```bash
bin/coreyard init --demo
bin/coreyard sync --sink csv --dry-run
```

## Read this first

**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** is the important document. Its second half is
the set of invariants whose violation is expensive and silent, each written with the failure
that taught it. If you are changing anything under `coreyard/`, read the sections that touch
your change before you start — several rules look like arbitrary indirection until you know
what went wrong without them.

The short version: `Part` (`coreyard/models.py`) is the contract between extract and publish.
The extract layer owns all schema knowledge and loads it from `schema.json` at runtime;
nothing downstream may name a database column. `StoreProfile` (`coreyard/config.py`) is the
matching contract for the installation — business name, city, warranty, handle prefix — and
threads through the whole render path so no customer-facing string is hardcoded.

## Three ground rules

**1. Vendor neutrality is enforced, not just encouraged.**
CoreYard reads a database you license from someone else. Their product names and their table
layout are theirs, and neither belongs in this source tree. Every installation supplies those
locally in `.env` and `schema.json` — both gitignored. `scripts/check_neutrality.py` runs in
CI and fails the build on a leak.

**2. Source writes are exceptional.**
Every database path is `SELECT`-only except `coreyard/yms/orders.py`, the explicitly enabled,
transaction-wrapped storefront order booking path. Do not reuse `yms.db.query` for writes, or
add another write operation, without equivalent observed invariants, rollback guarantees and
offline guard tests. A yard's inventory system is its livelihood.

**3. Tests stay offline.**
The suite must pass with no database, no network, no Shopify and no `.env`. Anything needing a
live server belongs in `scripts/` or behind an explicit CLI flag.

## The checks

CI runs these on Python 3.10 through 3.13. Run them locally before opening a PR:

```bash
.venv/bin/python -m unittest discover -t . -s tests    # the offline suite
python scripts/check_neutrality.py                # no vendor or site names in the tree
python scripts/inventory.py --check               # docs/INVENTORY.md matches the command tree
python scripts/ledger.py --summary                # every acceptance criterion has an evidence row
git diff --check                                  # no trailing whitespace
```

If you add, rename or remove a command, `scripts/inventory.py` regenerates
[`docs/INVENTORY.md`](docs/INVENTORY.md) and the new node needs an entry in `cli.SUPPORT`. The
suite fails when the command tree and the declared support statuses disagree, because a public
command whose support status is undocumented cannot be qualified for a release.

## Style

Four-space indent, `snake_case`, type hints, and short docstrings on non-obvious behaviour.
Imports grouped stdlib / third-party / local. No formatter or linter is configured — match the
surrounding code. Comments explain *why*; the *what* is usually already readable. When a
comment records a bug that a rule prevents, keep the bug in the comment.

## Pull requests

- One focused change per PR.
- Add a regression test for anything touching identifier mapping, state diffing, rendered
  output, retirement, webhook PII, or the guarded order transaction.
- Say which commands you ran to verify.
- Note any change to rendered titles, descriptions or handles.

## Two things that will bite you

**The handle prefix is a storefront primary key.** Products are keyed `<prefix>-<R#>`. Changing
the prefix on a live store makes every product look new and duplicates the entire catalogue.

**`state.product_fingerprint` hashes rendered output.** Any change to the renderer or the image
resolver invalidates every stored fingerprint, so the next incremental run treats the whole
catalogue as changed. That is safe — upserts are idempotent — but expensive. Mention it in the
PR so nobody is surprised by a six-hour sync.

## Where help is most useful

- **The release gates.** [`docs/EVIDENCE_LEDGER.md`](docs/EVIDENCE_LEDGER.md) tracks what has
  actually been verified against the [v0.1.0 acceptance
  criteria](RELEASE_ACCEPTANCE_v0.1.0.md). Most rows are `NOT VERIFIED`, and many need only a
  clean machine and patience rather than yard access — clean installs across distributions,
  interrupted and repeated installs, documented exit codes, dry-run non-mutation proofs.
- **A second source system.** The `Source` seam (`coreyard/source/`) exists so a yard system
  other than the one this was built against is an adapter rather than a fork. A CSV or SQLite
  export already works; a second real transport is the interesting work.
  [`docs/SOURCES.md`](docs/SOURCES.md) is the contract, and a new adapter's whole test file
  is a subclass with a factory in it. Read the first section before starting: if the system
  you have in mind is SQL Server over the same named pipe, it is a `schema.json`, not an
  adapter.
- **Rendering and SEO.** `coreyard/transform/` is pure, synthetic-fixture-tested, and where
  the storefront's quality actually comes from.
