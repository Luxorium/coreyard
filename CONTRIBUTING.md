# Contributing to CoreYard

Thanks for taking a look. CoreYard publishes a salvage yard's parts inventory to Shopify,
reading a yard management system's own SQL database.

## Ground rules

**1. Vendor neutrality is enforced, not just encouraged.**
CoreYard reads a database you license from someone else. Their product names and their table
layout are theirs, and neither belongs in this source tree. Every installation supplies those
locally in `.env` and `schema.json` — both gitignored.

`python scripts/check_neutrality.py` runs in CI and fails the build on a leak. Run it before
you open a PR.

**2. The source database is read-only.**
Every query is a `SELECT`. There is no code path that writes, and none should be added. A yard's
inventory system is its livelihood; this tool must never be the reason it breaks.

**3. Tests stay offline.**
The suite must pass with no database, no network, and no `.env`. Anything needing a live server
belongs in `scripts/` or behind a CLI flag.

## Getting set up

```bash
git clone <your fork>
cd coreyard
./install.sh --no-deps        # or plain ./install.sh to pull system packages too
python -m unittest discover -s tests -v
```

You do not need a live database to work on the transform, CSV, state, or SEO layers — they are
all unit-tested against synthetic `Part` objects.

## Architecture in one paragraph

`Part` (`coreyard/models.py`) is the contract between extract and publish. The extract layer owns
all schema knowledge and loads it from `schema.json` at runtime; nothing downstream may reference
a database column. `StoreProfile` (`coreyard/config.py`) is the matching contract for the
installation — business name, city, warranty, handle prefix — and threads through the whole render
path so no customer-facing string is hardcoded. See `CLAUDE.md` for the detailed map.

## Style

Four-space indent, `snake_case`, type hints, and short docstrings on non-obvious behaviour.
Imports grouped stdlib / third-party / local. No formatter or linter is configured — match the
surrounding code. Comments should explain *why*, since the *what* is usually already readable.

## Pull requests

- One focused change per PR.
- Add a regression test for anything touching identifier mapping, state diffing, or rendered
  output. Those are where silent breakage hurts most.
- Say which commands you ran to verify.
- Note any change to rendered titles, descriptions, or handles — see below.

## Two things that will bite you

**The handle prefix is a storefront primary key.** Products are keyed `<prefix>-<R#>`. Changing
the prefix on a live store makes every product look new and duplicates the entire catalogue.

**`state.product_fingerprint` hashes rendered output.** Any change to `shopify_product.py` or the
image resolver invalidates every stored fingerprint, so the next incremental run treats the whole
catalogue as changed. That is safe (upserts are idempotent) but expensive. Mention it in the PR.
