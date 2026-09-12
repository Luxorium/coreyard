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
.venv/bin/python -m unittest discover -t . -s tests -v          # full offline suite
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

## Architecture and invariants

Moved to **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** so that contributors and agents read
one document rather than two copies that drift. Read it before changing anything under
`coreyard/`: it carries the identifier and publishing invariants, the canonical renderer and
the rules downstream of it, fitment resolution, scopes and fingerprints, the photo manifest,
the webhook and order invariants, the reviewed-title contract, and the transport constraints —
every rule here whose violation is both expensive and silent.

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
source writes, or tax behavior. Keep `README.md`, the documents under `docs/`
(`SETUP.md`, `OPERATIONS.md`, `ARCHITECTURE.md`), `CONTRIBUTING.md`, `.env.example`,
`schema.example.json`, `AGENTS.md` and this file synchronized when their documented
interfaces change.
