# CoreYard v0.1.0 evidence ledger

One row per acceptance criterion, as QA-02 requires. `scripts/ledger.py` fails if a
criterion in [the specification](../RELEASE_ACCEPTANCE_v0.1.0.md) has no row here, or if a
row names a criterion the specification no longer defines.

**Missing evidence is NOT VERIFIED, never PASS** (REL-04). Most rows below are NOT
VERIFIED, and that is the honest state of the release rather than an oversight: the offline
suite passing is development evidence, and does not establish clean customer installation,
live integration behaviour, recovery, capacity or the soak gates.

A status here is a claim about *this* commit. Any later artifact or code change invalidates
the affected rows and requires re-verification (QA-03).

| Status | Meaning |
|---|---|
| PASS | Evidence collected at this commit and reviewed. |
| IN PROGRESS | Work landed and referenced, but the criterion is not yet fully met. |
| NOT VERIFIED | No evidence collected. The default, and not a judgement that it fails. |
| FAIL | Evidence collected and the criterion is not met. |

Owner and environment are unassigned until a release owner is named (SEC-04, QA-03).

Baseline commit: `e9c88ab` · offline suite: 1119 tests · Python 3.14.7 locally, 3.10-3.13 in CI.

| Criterion | Title | Status | Evidence and outstanding work |
|---|---|---|---|

| `REL-01` | Supported product | IN PROGRESS | Matrix published: `docs/CAPABILITY_MATRIX.md` covers sources, outputs, photos, every sync mode, reconcile/repair/audit, setup, diagnostics, scheduling, all four order stages, with prerequisites, limitations and per-row evidence. Unsupported combinations now refuse before side effects: `cli.unmet` checks declared requirements against resolved capabilities and exits 2 naming the capability, the reason and the configured source (`tests/test_command_inventory.py::UnsupportedCombinationsRefuseFirst`). Fixed 2026-09-08: the matrix said a tabular source supports sync, reconcile, repair and audit, and the code refused all four without a database `schema.json` — the document was right and the guard was source-blind. Outstanding: no combination has live acceptance evidence.
| `REL-02` | Optional features | IN PROGRESS | Order booking is `off` rather than `missing` when unconfigured, so it does not read as a broken install (`tests/test_capabilities.py::OptionalFeatures`). The listing-portal channel was removed by an explicit scope decision (2026-09-08): the supported product is source database → Shopify → sales, and REL-02's no-reduction rule is satisfied by that decision being recorded rather than assumed. Nothing else has been removed, deferred or relabelled. Outstanding: each optional feature still needs its own gates passed, which is what the rest of this ledger tracks.
| `REL-03` | Version and compatibility | PASS | `coreyard/__init__.py` is the single declaration; `pyproject.toml` reads it via setuptools dynamic metadata. No 1.0.0 artifact was ever distributed (no git tag, no GitHub release, no sdist/wheel), so no migration note is owed. `tests/test_inventory.py::VersionIsDeclaredOnce`; CI job `hygiene` compares the built wheel against `coreyard.__version__`. |
| `REL-04` | Decision rule | NOT VERIFIED | No evidence collected. |
| `REL-05` | Complete functionality inventory | IN PROGRESS | Inventory of all 55 CLI nodes generated from the live tree: `docs/INVENTORY.md`, `scripts/inventory.py`, CI job `inventory`. `cli.SUPPORT` gives every node an effect and its required capabilities; `tests/test_inventory.py` fails if the tree and the declarations disagree. The tree is walked by one implementation (`cli.tree`), used by both the preflight and the generator, so the document cannot describe a tree the code does not enforce. Outstanding: per-entry links to acceptance evidence, legacy entry-point coverage, and flag-interaction cases. |
| `DIST-01` | Public release | NOT VERIFIED | No evidence collected. |
| `DIST-02` | Reproducible dependency selection | NOT VERIFIED | No evidence collected. |
| `DIST-03` | Clean installation | IN PROGRESS | Paths with spaces are covered (`tests/test_data_root.py`). The documented quickstart now runs against a data root that has no `schema.json`, no `.env` and no credentials, in a subprocess with this machine's configuration stripped from the environment (`tests/test_clean_install.py`, 7 tests): it had failed four times in a row on database requirements a CSV source does not have, each invisible in a checkout that already had them. A wheel built from the tree, installed into a clean virtualenv and run from an unrelated directory renders the example yard (manually, 2026-09-08; the CI job is outstanding). `install.sh` now installs the package into the virtualenv, chooses an explicit data home (the checkout when it already holds an installation, `$XDG_DATA_HOME/coreyard` otherwise, or `--home`), offers to put `coreyard` on PATH, and no longer seeds a `.env` — which had made the very next documented command refuse with "already exists" on every clean machine. Two CI jobs cover it: the installer smoke test now runs the quickstart on the installation it just made, and a new `packaged` job builds the wheel, installs it into a clean virtualenv and runs the quickstart from a directory with no checkout in it. **Reinstallation preserving configuration and state: PASS** — `./install.sh --no-deps` was re-run on a live production installation on 2026-09-08. The data home resolved to the existing checkout, `.env` and `schema.json` were left untouched, and the sync-state and order-queue databases were byte-identical in size before and after (20,135,936 and 32,768). `doctor` reported every check OK afterwards, including source, photo share, Shopify, and the scheduled jobs still producing output; `scripts/healthcheck.py` exited 0. Outstanding: each advertised OS/Python pair (only ubuntu-latest is exercised), missing system tools, and interrupted installation.
| `DIST-04` | Installed runtime | IN PROGRESS | `config.DATA_ROOT` separates writable files from the installation: `COREYARD_HOME`, else a writable checkout, else `$XDG_DATA_HOME/coreyard`. Every `.env`, config file, database, lock, log and `out/` path moved; the installation directory now holds code only, guarded by a regression test. Arbitrary working directory, spaces in paths and two separate installations on one host are covered by `tests/test_data_root.py`. A source checkout resolves to itself, so no existing installation moves. An actual installed package has now been exercised from an arbitrary working directory: the bundled example yard and both config templates ship inside the wheel and are anchored to the package rather than the checkout (`tests/test_data_root.py`), `coreyard init --demo` copies the example into the data root, and the source path, the capability probe and the image resolver all resolve against the data root rather than the process directory. A real migration was performed on 2026-09-08: the production installation moved from "dependencies in a virtualenv, launcher that must `cd` into the checkout" to the package installed into the virtualenv with the data home exported explicitly. `import coreyard` now resolves from any working directory, `DATA_ROOT` still resolves to the checkout, and both scheduled entry points — the launcher and `scripts/healthcheck.py`, which runs with no `cd` — were verified afterwards. Outstanding: a read-only installation tree, and two installations on one host proven concurrently rather than by construction.
| `UX-01` | Independent onboarding | NOT VERIFIED | No evidence collected. |
| `UX-02` | Configuration contract | IN PROGRESS | `coreyard/capabilities.py` reports each setting as on/off/missing with the key that decides it, and never echoes a secret (`tests/test_capabilities.py`). Outstanding: the documented type, default, requirement, precedence and example for every supported setting. |
| `UX-03` | Explicit effects | IN PROGRESS | Every node declares its effect and the flag that unlocks it (`docs/INVENTORY.md`); `tests/test_inventory.py` fails any external write with no named gate. Fixed: `images --delete` was classified read-only and left no run record. Outstanding: dry-run non-mutation proofs per command. |
| `UX-04` | Actionable failure | IN PROGRESS | `coreyard doctor` help and every command's `--help` run without credentials (`tests/test_inventory.py::HelpWorksWithoutCredentials`). Outstanding: documented exit codes for automation, and the injected-failure cases. |
| `UX-05` | Streamlined workflows | NOT VERIFIED | No evidence collected. |
| `UX-06` | Simple, consistent controls | NOT VERIFIED | No evidence collected. |
| `UX-07` | Verbose, useful feedback | NOT VERIFIED | No evidence collected. |
| `UX-08` | Dynamic configuration and capability handling | IN PROGRESS | Available operations and probes derive from validated configuration; one run resolves one capability snapshot and passes it down (`coreyard/capabilities.py`, `coreyard/doctor.py`). Outstanding: part-type coverage, unknown-type rendering, and the no-restart verification. |
| `UX-09` | Customer comprehension | NOT VERIFIED | No evidence collected. |
| `DATA-01` | Identity and isolation | NOT VERIFIED | No evidence collected. |
| `DATA-02` | Extraction safety | NOT VERIFIED | No evidence collected. |
| `DATA-03` | One rendering contract | NOT VERIFIED | No evidence collected. |
| `DATA-04` | Truthful content | NOT VERIFIED | No evidence collected. |
| `DATA-05` | Media correctness | NOT VERIFIED | No evidence collected. |
| `DATA-06` | Customer mapping acceptance | NOT VERIFIED | No evidence collected. |
| `DATA-07` | Source freshness | NOT VERIFIED | No evidence collected. |
| `SYNC-01` | Convergence | NOT VERIFIED | No evidence collected. |
| `SYNC-02` | Partial failure | NOT VERIFIED | No evidence collected. |
| `SYNC-03` | Retirement guards | NOT VERIFIED | No evidence collected. |
| `SYNC-04` | Time and contention | NOT VERIFIED | No evidence collected. |
| `SYNC-05` | Sale versus publication race | NOT VERIFIED | No evidence collected. |
| `ORD-01` | Authenticated paid orders | NOT VERIFIED | No evidence collected. |
| `ORD-02` | Database transaction | NOT VERIFIED | No evidence collected. |
| `ORD-03` | Lifecycle accuracy | NOT VERIFIED | No evidence collected. |
| `ORD-04` | Durable receipt and bounded retries | NOT VERIFIED | No evidence collected. |
| `MKT-01` | Listing identity and guards | NOT VERIFIED | No evidence collected. |
| `SEC-01` | Review and scans | NOT VERIFIED | No evidence collected. |
| `SEC-02` | Least privilege and isolation | NOT VERIFIED | No evidence collected. |
| `SEC-03` | Payload lifecycle | NOT VERIFIED | No evidence collected. |
| `SEC-04` | Response ownership | NOT VERIFIED | No evidence collected. |
| `OPS-01` | Truthful status | IN PROGRESS | The observed `exit=2` / `ok=1` defect is fixed and regressed: `cli.main` assigns the command's return code to the recorder, `ops.record` stores it and treats an exception as failure regardless (`tests/test_ops.py`). Diagnostics honour the configured source and enabled capabilities (`tests/test_doctor.py`). Outstanding: agreement under interrupted processes, and queue-age reporting. |
| `OPS-02` | Unattended service | NOT VERIFIED | No evidence collected. |
| `OPS-03` | Alerts | IN PROGRESS | `coreyard alert` implements every named condition: full sync stale for two of its own scheduled intervals (read from the host's crontab or timer), delta cursor stale 15 min, an order stage pending/failed 10 min, and storage/credentials blocking progress — plus the timing-out and failing sync a staleness check cannot see. Delivery and recovery are exercised through a real command (`tests/test_alerts.py`, 30 tests), a failed delivery is retried rather than recorded as sent, and alerts carry no payload or secret. `doctor` warns when no notifier is configured. Outstanding: delivery to a real paging service on a customer host, and a scheduled-job entry in the documented runbook.
| `OPS-04` | Backup and restore | NOT VERIFIED | No evidence collected. |
| `OPS-05` | Upgrade and rollback | NOT VERIFIED | No evidence collected. |
| `OPS-06` | Emergency stop and bounded rollout | NOT VERIFIED | No evidence collected. |
| `PERF-01` | Reference workload | NOT VERIFIED | No evidence collected. |
| `PERF-02` | Steady state | NOT VERIFIED | No evidence collected. |
| `PERF-03` | Order latency | NOT VERIFIED | No evidence collected. |
| `PERF-04` | Initial import | NOT VERIFIED | No evidence collected. |
| `PERF-05` | Measured efficiency | NOT VERIFIED | No evidence collected. |
| `QUAL-01` | Soak | NOT VERIFIED | No evidence collected. |
| `QUAL-02` | External contracts and buyer-visible output | NOT VERIFIED | No evidence collected. |
| `DOC-01` | Complete operator path | IN PROGRESS | The operator path is published and split by task: [`docs/SETUP.md`](SETUP.md) covers install, prerequisites, source mapping and storefront policy; [`docs/OPERATIONS.md`](OPERATIONS.md) covers preview, first publish, the sync modes, reconcile/repair/audit, orders and scheduling; [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) covers the design and the invariants. The quick-start example runs as written against the bundled seven-part demo yard and needs no credentials. Outstanding: backups, upgrades, rollback, troubleshooting decision paths and uninstall — including the requirement that uninstall stop services without deleting business state or remote listings — are not documented anywhere yet. |
| `DOC-02` | Honest positioning | IN PROGRESS | `README.md` describes CoreYard as a self-hosted backend integration engine, not a storefront; names the prerequisites (a licensed source database, a mapping, a Shopify token) before the install; states the supported product as source database → Shopify → sales and says there is no marketplace integration; and sends the reader to the capability matrix rather than claiming universal source compatibility. The single-variant assumption is stated in both the README's known gaps and the matrix's Shopify row. The Status section says the scale this runs at and that one installation is not a release. Outstanding: reviewed against the artifact at the release commit (QA-03). |
| `DOC-03` | Supportability | IN PROGRESS | Support channel published in `README.md`, `CONTRIBUTING.md`, `SECURITY.md` and the issue-template config: GitHub issues, an email route for what does not suit a public issue, and a separate private route for vulnerabilities. Issue templates exist for bugs and features. Supported configurations are in [`docs/CAPABILITY_MATRIX.md`](CAPABILITY_MATRIX.md). The response expectation is stated rather than implied. Outstanding: the redacted diagnostic bundle (version, capability validation, recent outcomes, correlation ids), troubleshooting decision paths, and the exercise in which someone other than the author resolves a simulated auth failure, stuck queue and stale sync from the runbooks alone. |
| `QA-01` | Continuous gates | IN PROGRESS | Offline suite: 1124 tests, no `.env`, credentials or network, on Python 3.10-3.13 in CI. CI now also checks the functionality inventory, tracked-file whitespace and built-artifact version. Example config validation added for `schema.example.json` (`tests/test_command_inventory.py`) and `store.example.json` (`tests/test_store_file.py`). The installed-artifact demo now exists (CI job `packaged`). Outstanding: the dependency-metadata job. |
| `QA-02` | Evidence ledger | IN PROGRESS | This ledger, with `scripts/ledger.py` holding it level with the specification. Outstanding: owner, environment and redacted result link per row. |
| `QA-03` | Final acceptance | NOT VERIFIED | No evidence collected. |
| `QA-04` | Artifact provenance and withdrawal | NOT VERIFIED | No evidence collected. |

## What the next work is, in the specification's own order

The specification's closing list, with what has moved:

1. ~~Fix nonzero command results being recorded as successful runs (OPS-01).~~ **Done** —
   `cli.main` hands the command's exit code to `ops.record`, which stores it and refuses to
   call an exception a success. `coreyard status` prints the code beside a failed run.
2. ~~Resolve `1.0.0` metadata versus the intended `v0.1.0` release (REL-03).~~ **Done** —
   one declaration in `coreyard/__init__.py`, read by `pyproject.toml`. Nothing at 1.0.0 was
   ever distributed, so nothing needs a transition note.
3. ~~Make diagnostics honour tabular sources and optional capabilities (UX-02, OPS-01).~~
   **Done** — `coreyard/capabilities.py` decides what this installation is configured to do,
   and every check asks it first. A file-backed yard is no longer told to install
   `smbclient`, supply SMB credentials and map a `schema.json` it will never read.
4. Qualify clean distributed-artifact installs and runtime paths (DIST-03, DIST-04). **Not
   started.** CI builds no artifact today beyond the new version check, and `setuptools` is
   absent from the development venv, so the wheel build is unexercised locally.
5. Create and exercise backup, restore, upgrade and rollback procedures (OPS-04, OPS-05).
   **Not started.** `scripts/backup_state.sh` exists and is neither documented as the
   procedure nor exercised by a restore drill.
6. ~~Establish the complete functionality inventory, capability matrix and evidence
   ledger (REL-01, REL-05, QA-02).~~ **Done as documents** — `docs/INVENTORY.md`,
   `docs/CAPABILITY_MATRIX.md` and this ledger, all three CI-enforced against the code they
   describe. What remains is not more documenting: it is collecting the evidence the
   matrix currently records as `NOT VERIFIED`.

## Where the work goes next

The three documents now say precisely what is unqualified, which makes the order obvious:

- **Nothing has live acceptance evidence.** Every "Live: `NOT VERIFIED`" row in the matrix
  needs a controlled environment — a test store and a disposable mapped database. That is the bulk of sections 4 through 9 of the specification.

- **Clean-install and recovery drills have not been run** (DIST-03, DIST-04, OPS-04,
  OPS-05), and `setuptools` is absent from the development venv, so the wheel build is
  unexercised outside CI.

