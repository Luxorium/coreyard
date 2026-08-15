# Repository Guidelines

## Project Structure & Module Organization

The `coreyard/` package implements the yard-system-to-Shopify pipeline. Models and configuration live in `coreyard/models.py` and `coreyard/config.py`; `coreyard/yms/` reads SQL Server inventory and SMB images; `coreyard/transform/` builds Shopify data; and `coreyard/sink/` writes CSV or calls Shopify. `coreyard/run_sync.py` is the main CLI, while `coreyard/state.py` tracks incremental changes. Tests live in `tests/`; utilities and connectivity checks belong in `scripts/`. Treat `out/`, `coreyard_sync_state.sqlite3`, and `.env` as generated or private data.

## Build, Test, and Development Commands

Create and activate a virtual environment, then install live-integration dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

There is no build step. Use these commands from the repository root:

- `python -m unittest discover -s tests -v` runs the complete unit suite.
- `python scripts/demo_offline.py` exercises the offline CSV flow.
- `python -m coreyard.run_sync --check` checks configured service connectivity.
- `python -m coreyard.run_sync --sink csv --limit 25 --dry-run` previews a small sync without committing state.
- `python -m coreyard.yms.discover_schema` inspects the source schema and writes to `out/schema/`.

## Coding Style & Naming Conventions

Follow standard Python style: four-space indentation, `snake_case` for functions and modules, `PascalCase` for classes, and `UPPER_CASE` for constants. Group imports as standard library, third-party, then local modules. Preserve type hints and short docstrings on non-obvious behavior. Prefer `pathlib.Path`, dataclasses, and explicit mappings. No formatter or linter is configured; keep changes consistent with nearby code and within 88 characters when practical.

## Testing Guidelines

Tests use `unittest`. Name files `test_<area>.py`, classes after the behavior under test, and methods `test_<expected_behavior>`. Add regression tests for mapping, identifier, state-diff, and CSV/API changes. Keep unit tests offline; live database or Shopify checks belong in scripts or explicit CLI checks.

## Commit & Pull Request Guidelines

This repository has no commit history yet. Start with concise, imperative subjects such as `Add inventory mapping validation`; keep each commit focused. Pull requests should explain the behavior changed, list verification commands, call out configuration or schema assumptions, and link relevant issues. Include sample CSV snippets or screenshots when Shopify-facing output changes, but redact inventory credentials, tokens, customer data, and `.env` contents.

## Security & Configuration

Copy `.env.example` to `.env` and never commit secrets. Keep database access read-only. Do not commit generated images, CSV files, schema dumps, logs, or the sync-state database.

Vendor neutrality is enforced, not just requested: `scripts/check_neutrality.py` runs in CI and fails on the yard system's name, its vendor's name, or that vendor's column names anywhere in the tracked tree. Table and column names live in a local, gitignored `schema.json`; `schema.example.json` documents the shape with placeholders.

Read configuration through `config._get`, never `os.environ` directly — `config._LEGACY_KEYS` maps renamed keys to their old names so an existing `.env` survives an upgrade, and going straight to the environment bypasses it. When renaming a key, add it to that map rather than cutting over.

## Publishing Behavior to Know Before Changing It

- `productSet` has *set* semantics: an omitted field is reset, not preserved. `shopify_write._upsert` re-sends the existing product's status for this reason.
- Retirement (`--sink api`) is the only destructive operation. It is guarded by `--limit` and `--max-retire-fraction`; see `tests/test_retire.py` before relaxing either.
- Editing `transform/shopify_product.py` or the image resolver invalidates every stored fingerprint and makes the next run a full rewrite. Weigh that first.
- The 2026-07 Admin API has removed several media mutations. Introspect before assuming one exists.
