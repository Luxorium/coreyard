# CoreYard v0.1.0 release acceptance

Status: proposed release contract; not a certification of the current implementation.

CoreYard v0.1.0 is accepted when a new customer can install it from a public release,
configure a supported source and store, preview changes, operate it unattended, diagnose
failures, and recover without developer intervention or silent loss of business data.
The user-directed scope is the complete existing CoreYard product: all features and full
functionality are required for v0.1.0. The version number does not lower data-safety,
customer-usability or feature-completeness requirements. This specification supersedes the
earlier suggestion of a smaller stabilization release with deferred integrations.

Product requirements: CoreYard must be streamlined, simple, optimized, dynamic, verbose
and user-friendly across its complete feature set. Routine operation should require few
decisions, while the system clearly explains its work. Dynamic behavior means adapting to
validated configuration, source data and enabled capabilities; business rules remain
deterministic. Simplicity must preserve functionality and safety.

## 1. Scope and release decision

- **REL-01 — Supported product.** Publish a capability matrix covering SQL Server over SMB,
  CSV and SQLite sources; CSV and Shopify outputs; photos and donor photos; full, scoped,
  delta and bulk sync; counts; audit, repair and reconciliation; setup, diagnostics and
  scheduling; and order receipt, polling, booking and lifecycle updates.
  channel. For each combination, name prerequisites, supported operations, limitations and
  the acceptance evidence. A tabular source need not support database order booking or
  database deltas, but unsupported combinations must fail before side effects with a useful
  explanation. No public command may have an undocumented support status.
- **REL-02 — Optional features.** Order booking remains opt-in. Every
  existing feature must be complete and pass its applicable gates, even if disabled by
  default. Optional means the customer chooses whether to enable it; it does not mean
  optional implementation, testing, documentation or support. No existing feature may be
  removed, deferred, hidden or relabeled experimental to satisfy release acceptance. An
  incomplete integration blocks v0.1.0. Any reduction requires a new explicit user decision.
- **REL-03 — Version and compatibility.** Resolve the existing `1.0.0` declarations before
  tagging `v0.1.0`. Verify whether any `1.0.0` artifacts were distributed, document the
  transition if so, and make metadata, CLI, artifact and tag versions agree from one
  authoritative definition. Document configuration, state and CLI compatibility, including
  supported legacy scheduler entry points. Future breaking changes require migration notes.
- **REL-04 — Decision rule.** All applicable criteria below must have PASS evidence at the
  exact release commit. No unresolved critical or high-severity defect is allowed. Critical
  includes unauthorized writes, credential/customer-data exposure, duplicate sale booking
  or destructive catalog corruption. High includes silent missed updates, false healthy
  status, incorrect prices/identity/availability, failed recovery, or a supported customer's
  inability to install or operate. Lower-severity issues need a documented impact, workaround,
  owner and target milestone. Missing evidence is NOT VERIFIED, never PASS.
- **REL-05 — Complete functionality inventory.** Create a checked-in inventory from the
  actual CLI tree, nested subcommands, supported flags, legacy entry points and documented
  workflows. Cover `init`, `status`, `counts`, `doctor`, every `sync` mode,
  operation/worker, `reconcile`, every `repair` and `audit` operation, every `orders` transport
  and lifecycle operation, `images`, `part-types`, `schema`, `bulk`, `altfix`, `oauth` and
  `schedule`. Include CSV/SQLite sources, enrichment, fitment caches, donor media, configuration
  overrides, shipping/weight policies, channel state, locks, deadlines and recovery behavior.
  Each inventory entry maps to implementation, public documentation, automated behavior
  coverage and appropriate integration/acceptance evidence. Verify important flag interactions
  and feature handoffs, not only individual help screens. No placeholder, no-op or success
  response for unimplemented behavior is acceptable. New features added during development
  join this inventory and its gates before release.

## 2. Public distribution and installation

- **DIST-01 — Public release.** Provide an immutable source tag, release notes, installable
  artifacts with checksums, license, dependency inventory, support matrix and upgrade guide.
  Build and test the distributed artifacts in CI; a passing source checkout alone is
  insufficient. The release must contain no private configuration, customer data, generated
  state, credentials or vendor-specific schema material.
- **DIST-02 — Reproducible dependency selection.** Record the exact dependency set used for
  release qualification and provide a repeatable supported installation path. Review
  dependency vulnerability and license scan findings; resolve critical/high findings that
  affect the shipped product. Explain optional dependencies and system tools by capability.
- **DIST-03 — Clean installation.** On every advertised OS/Python combination, install into
  a clean environment without a sibling repository or preexisting private files. Verify
  CLI discovery, version, demo and configuration validation. Test paths with spaces, missing
  system tools, interrupted installation and reinstallation. Reinstallation preserves user
  configuration and state. Declare the tested Linux scope instead of claiming every distro.
- **DIST-04 — Installed runtime.** Each advertised installer/package method works from an
  arbitrary working directory with documented writable config, state, cache and output
  locations. It does not require writing into a read-only package installation. Multiple
  installations on one host keep store identities, locks, credentials and state separate.

## 3. Customer onboarding and command experience

- **UX-01 — Independent onboarding.** Two people unfamiliar with the code complete, using
  only public documentation: installation, offline demo, configuration validation, preview,
  test-store publication, status inspection and scheduler setup. Neither edits Python nor
  uses maintainer-only files. Each finishes the demo within 15 minutes and the test-store
  workflow within 90 minutes, excluding account provisioning and source mapping preparation.
  Record obstacles and resolve every task-blocking documentation/product defect.
- **UX-02 — Configuration contract.** Every supported setting has type, default, requirement,
  precedence, example and effect documented. Validation identifies file/key and correction
  without printing secrets. Test malformed JSON, invalid flags, missing mappings, unknown
  keys, conflicting policies and legacy keys. Invalid safety-critical configuration fails
  before external writes; optional capabilities do not demand unrelated credentials.
- **UX-03 — Explicit effects.** Help and command output consistently identify external writes,
  local writes, dry runs and irreversible actions. Dry runs perform no external mutation and
  do not advance publication state or delivery completion; any preview/history files are
  documented. A new setup ends with a preview, not an automatic live publish. Existing CLI
  behavior changes require compatibility notes and migration tests.
- **UX-04 — Actionable failure.** Expected configuration, authentication, network, mapping and
  permission failures report what failed and the next action. Automation receives documented
  exit codes and machine-readable status. Help works without credentials. Color is optional;
  outcomes do not depend on color, and noninteractive commands do not hang on prompts.

- **UX-05 — Streamlined workflows.** Publish one recommended path for each customer task:
  setup, first publication, routine sync, order processing, marketplace maintenance, diagnosis
  and recovery. A configured routine task runs through one documented command or scheduled
  workflow without manually copying intermediate files, composing internal scripts or
  repeating configuration. Preserve deliberate review boundaries for marketplace saves,
  pushes and delisting. Advanced controls and legacy aliases remain available and documented;
  they do not create competing recommended paths. Exercise these workflows in UX-01.
- **UX-06 — Simple, consistent controls.** Shared options use the same name, meaning and
  output conventions across commands wherever compatibility permits. Setup asks only for
  the selected capabilities, explains required choices, validates entries and preserves
  existing settings when resumed. Defaults are documented and work for the demo without
  tuning. Common tasks require no knowledge of module paths, SQLite tables or internal
  checkpoints. Test errors, help examples and unattended equivalents for each recommended
  workflow; migrate conflicting legacy behavior explicitly rather than silently changing it.
- **UX-07 — Verbose, useful feedback.** Normal output identifies the operation, target alias,
  scope, preview/write mode and stages as they run. For a task lasting over 30 seconds,
  provide a progress or waiting update at least every 30 seconds, including bounded retries
  and lock waits. Report processed/total where known, elapsed time and the reason for a
  delay; do not invent totals or completion estimates. Every completed run summarizes
  succeeded, unchanged, skipped/held, failed and pending work, with reasons and a next action
  where needed. A verbose mode adds per-item decisions and redacted diagnostic detail;
  quiet mode suppresses routine progress but preserves failures. Test all modes, redirected
  logs and stable machine-readable output without mixed progress text. Never expose secrets
  or customer payloads at any verbosity level.
- **UX-08 — Dynamic configuration and capability handling.** Derive available operations,
  source behavior, part-type coverage and store policies from validated configuration and
  current source data. Adding a supported part type or configuring another yard must not
  require a code edit or a fixed inventory list. Unknown types use truthful generic rendering
  or an explained hold when required policy is missing. Configuration changes take effect
  on the next run and show their effect in preview; one run uses a consistent configuration
  snapshot. Required capability loss is a visible failure, never a silent fallback that
  changes write targets, pricing, identity or order policy. Verify configuration changes,
  new inventory types and optional capability enablement without reinstalling or rebuilding
  the application; document any service restart required.
- **UX-09 — Customer comprehension.** During the independent trials in UX-01, each participant
  must correctly identify whether a run wrote externally, what remains pending, why an item
  was held and what to do after an injected failure, using the output and public help alone.
  Correct all cases where success wording hides incomplete work or recovery requires a
  maintainer's explanation. Keep common instructions in plain language and expand necessary
  domain terms where first encountered.

## 4. Source integrity and catalog correctness

- **DATA-01 — Identity and isolation.** R# remains the SKU/state identity and configured handle
  key. Duplicate, empty or ambiguous identifiers are rejected or explicitly held before
  publication. Store/source or handle-prefix changes cannot silently reuse incompatible
  state or duplicate a live catalog; the customer receives a migration-required outcome.
- **DATA-02 — Extraction safety.** Extraction is read-only. Prove that failed queries,
  transport errors, partial results and malformed rows cannot masquerade as an authoritative
  empty yard and cause retirement. All sources apply the same documented listable policy.
  Test positive/zero/negative prices, missing data, quantities and photo requirements.
- **DATA-03 — One rendering contract.** CSV/API serialization and fingerprints use the same
  canonical product. Every owned shopper-visible field affects the appropriate fingerprint.
  Preserve external tags, publication and intended status. Shopify prices equal source
  prices to the documented currency precision. Marketplace adjustments originate only in
  the configured pricing policy and never compound an already adjusted price.
- **DATA-04 — Truthful content.** A reviewed synthetic catalog covers all renderer branches
  and configured part types, including left/right, VIN qualifiers, restricted fitment years,
  abbreviations, unknown types, Unicode and hostile HTML. Rendered output preserves material
  qualifiers, escapes untrusted text and invents no fitment, condition or business promise.
  Unknown facts are omitted or flagged. CoreYard uses no LLM or inference service.
- **DATA-05 — Media correctness.** Prove exact part/photo matching, same-name replacement
  detection, deterministic full/delta manifests, bounded donor fallback and truthful donor
  labeling. Own photos supersede donor photos. Failed refresh retains usable existing media;
  unchanged catalog updates do not repeatedly upload media. Stored media state describes
  successful publication. Single-variant media limitations are documented.
- **DATA-06 — Customer mapping acceptance.** Before first publication, produce a read-only
  mapping report with source/listable counts, duplicate identities, missing required values,
  exclusions and representative rendered samples. The installation owner verifies those
  against the source application, including sold, blocked and open-order stock. Qualify at
  least two independently configured synthetic yards with different schema names and store
  policies to detect assumptions specific to the original installation. Currency, quantity
  semantics, weight units and source timestamp/timezone interpretation must be explicit;
  incompatible store settings fail before publication rather than silently converting data.
- **DATA-07 — Source freshness.** A successful read does not establish that an export is
  current. Define a maximum source age and observable freshness signal for each supported
  source, including CSV/SQLite exports. Test a stale export, truncated/replaced file and
  missing modification signal. Unknown/stale source state must block automated availability
  increases and absence-based retirement, preserve pending work and alert the operator.
  Document the remaining exposure of already-live stock and its emergency procedure.

## 5. Sync, retirement and concurrency

- **SYNC-01 — Convergence.** In a test store, run create, price/quantity/copy/photo update,
  retirement and revival scenarios. After each completed reconciliation, all managed fields
  match the expected source/policy fixture. A second unchanged run produces zero unnecessary
  product/media mutations. Verify CSV import behavior separately from API output.
- **SYNC-02 — Partial failure.** Inject failure before request, after remote success but before
  local checkpoint, during a batch and during checkpoint persistence. Restart converges
  without duplicate products or forgotten updates. Only successfully published data advances
  its state; one channel's success never hides another channel's failure.
- **SYNC-03 — Retirement guards.** Exercise limited, explicit-R#, scoped, full, delta, empty
  and suspiciously reduced extracts. Partial views cannot infer retirement from absence.
  Fraction/cap checks run before the guarded writes. Failed retirements remain pending;
  retirement archives and preserves revival status instead of deleting products.
- **SYNC-04 — Time and contention.** Test overlapping schedules, manual runs against scheduled
  jobs, SQLite contention, clock skew, delta overlap and writes committed during extraction.
  No lost changes or corruption occur. Busy locks, timeouts and interrupted runs are visible
  and distinct from completed success. Bulk and incremental paths have a documented, tested
  handoff that preserves pending work despite their different progress stores.
- **SYNC-05 — Sale versus publication race.** Exercise an order arriving after extraction
  but before publication, while a bulk job is running and while another channel is updating.
  A stale in-flight snapshot must not restore quantity or revive a part already retired by
  the order pipeline. Also test a concurrent source-counter sale: recheck or otherwise guard
  availability at the write boundary and measure any remaining distributed-system race.
  Document the cross-channel oversell exposure and conflict procedure; do not promise atomic
  reservation across systems that do not share a transaction. Conflicts remain visible and
  never produce a negative source quantity or an invented successful booking.

## 6. Orders and marketplace operations

- **ORD-01 — Authenticated paid orders.** Invalid/missing webhook signatures, unpaid orders,
  malformed/oversized requests and wrong-store deliveries cannot print, book or retire parts.
  Duplicate delivery IDs and repeated order identities across polling/webhooks produce one
  booking and bounded repeat-safe effects. A crash between stages retries only pending work.
- **ORD-02 — Database transaction.** Test explicit enablement and mapping gates, parameter/value
  escaping, atomic allocation, concurrent bookings, unused-ID checks, inventory floors,
  exactly-one-row enforcement, rollback and stored-reference idempotence. No second source
  write path or DELETE is introduced. Qualify actual SQL behavior on a disposable mapped
  database; SQL-string unit tests alone do not establish transaction safety.
- **ORD-03 — Lifecycle accuracy.** Verify tax, pickup, freight, mixed orders and shipping-app
  handoff against configured policy. Unknown line identity is held visibly. Fulfillment
  never closes work a shipping integration still owns. Cancellation, refunds and source
  inventory reversal have a clear, tested operator procedure wherever manual.
- **ORD-04 — Durable receipt and bounded retries.** A webhook success response occurs only
  after durable queue commit. Test process termination immediately after acknowledgement,
  disk-full before commit and polling-cursor advancement during partial ingestion. Every
  acknowledged event remains recoverable; rejected ingestion remains eligible for upstream
  retry. One permanently failing order cannot starve later orders. Exhausted retries are
  visible, bounded by the retention policy and accompanied by a recovery/escalation path
  before payload expiry. Payload removal must not erase the non-PII evidence needed to
  prevent duplicate booking on later replay.
- **MKT-01 — Listing identity and guards.** Test ambiguity, stale plans, incomplete grids,
  title budgets, aspect vocabulary, price/shipping calculations and per-listing caps. An
  uncertain match is held; no guessed identity is written. Recheck eligibility immediately
  before mutation and verify read-back before checkpointing success.

## 7. Security and privacy

- **SEC-01 — Review and scans.** Complete a threat review covering untrusted source text,
  webhook input, mapping/config trust, remote URLs, shell/SQL boundaries and credential
  storage. Scan the release tree, artifacts and public history for secrets/private data.
  Fix exposures and rotate affected credentials; ignoring a file is not remediation.
- **SEC-02 — Least privilege and isolation.** Document permissions/scopes per enabled
  capability and test operation with those permissions. Credentials/cookies are owner-only,
  excluded from process arguments and redacted from errors/support exports. TLS verification
  stays enabled for HTTPS. Services bind to documented safe interfaces; deployment guidance
  includes webhook HTTPS, signature verification and request/resource limits.
- **SEC-03 — Payload lifecycle.** Test PII retention, failure expiry, successful-payload
  removal and file modes, including SQLite journals/WAL, logs, temp files and backups.
  Document storage-level deletion limits accurately; do not promise forensic secure erasure
  from an application delete alone. Support bundles contain no payloads or secrets by default.
- **SEC-04 — Response ownership.** Verify the private vulnerability-reporting channel works.
  Publish supported versions, an acknowledgement target of three business days and how
  customers receive security fixes. Name a release/security owner and test a redacted report.

## 8. Operations and recovery

- **OPS-01 — Truthful status.** Exit code, run history and status agree for returned nonzero
  codes, exceptions, partial failures, timeouts and interrupted processes. Specifically
  regress the observed `exit=2` / `ok=1` defect. Report last successful full/delta runs,
  observation age, pending/failed counts and queue age. Unreachable services are unknown or
  failed, never healthy. Diagnostics respect the selected source and enabled capabilities.
- **OPS-02 — Unattended service.** Scheduler/service installation and removal are idempotent,
  preserve unrelated jobs and survive reboot. Test restart, log rotation, bounded history,
  queue retention, rate limiting, retry/backoff, disk-full and permission errors. Document
  which jobs share locks and ensure the configured deadlines permit their actual workload.
- **OPS-03 — Alerts.** Provide a documented, tested way to notify the operator when a full
  sync is stale for two scheduled intervals, deltas are stale for 15 minutes, an order stage
  remains failed/pending for 10 minutes, or storage/credentials prevent progress. Alert
  delivery and recovery notification must be exercised, not merely described in a log.
- **OPS-04 — Backup and restore.** Provide a consistent backup procedure for configuration,
  sync/retirement/channel state, pending orders and marketplace intent. Distinguish rebuildable
  caches. Restore onto a clean host and verify integrity before enabling schedules. With a
  backup at most 24 hours old, reconcile/replay newer work without duplicate booking, lost
  acknowledged orders, unsafe revival or silently skipped updates. Record external retention
  prerequisites; a SQLite backup alone does not recover events received after that backup.
- **OPS-05 — Upgrade and rollback.** Upgrade a copy of the current installation and every
  explicitly supported prior state format. Preserve handles, credentials, pending work,
  fingerprints and archive memory. Demonstrate rollback using compatible code/state or
  snapshot restoration plus replay. Do not restore stale state blindly after external writes.
  A trained operator completes the recovery drill within 60 minutes, excluding a separately
  measured full media rebuild, whose degraded operation and completion time are documented.
- **OPS-06 — Emergency stop and bounded rollout.** A documented procedure stops all outbound
  mutation workers, including manually launched and optional channel workers, while preserving
  durable inbound order receipt or relying on a verified upstream retry mechanism. Exercise
  it during active work: no new mutation starts after the stop takes effect; already-sent
  requests are accounted for by reconciliation. Resume from preserved pending state. Before
  enabling a customer's full schedule, publish a bounded representative batch, review actual
  remote output and reconcile it. Record the rollout's stop conditions and rollback owner.

## 9. Capacity and production qualification

The following are release-test targets, not claims already established or a customer SLA.

- **PERF-01 — Reference workload.** Publish hardware, network/API constraints, software versions
  and fixtures. Minimum qualification host: 4 CPU cores and 8 GiB RAM. Dataset: 30,000 parts,
  10,000 donor records and 180,000 photo references, with representative fitment and shipping
  policies. Peak process-tree memory stays below 4 GiB. Publish disk needs and distinguish
  reference scans from actual image downloads/uploads.
- **PERF-02 — Steady state.** Across at least 100 measured runs with dependencies healthy,
  p95 unchanged full sync completes within 45 minutes; p95 delta of 100 price/quantity
  changes completes within 4 minutes. With the documented five-minute delta schedule, p95
  source-commit-to-store availability update is within 10 minutes, including scheduler wait
  and lock contention. Measure image/copy workloads
  separately. Source-independent local status responds within 5 seconds; live diagnostics
  have bounded, documented per-service deadlines.
- **PERF-03 — Order latency.** At 10 paid orders per minute over 30 minutes, webhook queue
  acknowledgement p95 is below 2 seconds and booking/retirement p95 below 60 seconds with
  dependencies healthy. Polling latency additionally includes its configured interval.
  All accepted orders remain accounted for under injected restart and dependency outage.
- **PERF-04 — Initial import.** Measure complete initial catalog/media import, request volume,
  failures and resource use. Publish the measured duration and supported limits. Interrupt
  and resume it successfully; never claim steady-state timings as initial-import performance.
- **PERF-05 — Measured efficiency.** Profile representative unchanged, delta, media, order and
  marketplace workloads; record source queries, remote requests, transferred bytes, cache
  behavior, CPU, peak memory and wall time. Use bounded batching/concurrency and shared
  lookups where measured to help. Prove unchanged work avoids unnecessary mutations and
  repeated full scans within a run. Cache invalidation follows source/configuration changes;
  stale or failed cache reads cannot hide pending work. Compare each optimization with its
  baseline and rerun relevant correctness/failure scenarios. Meet PERF-01 through PERF-04
  with documented defaults, without customer tuning or weakened verification, retirement,
  durability or retry safeguards. Explain unavoidable expensive operations in run output.
- **QUAL-01 — Soak.** Complete 14 consecutive days on a controlled representative installation
  with the advertised schedules and all optional features under test, across controlled
  installations where different capability combinations require it. Exercise additions,
  changes, sales, revival, reboot, expired credentials, throttling and a 30-minute dependency
  outage. Require zero unexplained divergence, duplicate bookings, lost accepted orders or
  unresolved high/critical defects. After recovery, queued work converges within 60 minutes
  for the reference change workload; report media backlogs separately. A material change
  affecting the qualified behavior requires a new relevant qualification window.
- **QUAL-02 — External contracts and buyer-visible output.** Record and exercise the actual
  supported Shopify API version and required scopes; set an owner
  and review date before any known compatibility deadline. Contract drift fails visibly
  without advancing state. In a controlled storefront, inspect actual desktop/mobile product
  pages and complete test checkouts for parcel, pickup, freight and sold-out cases where
  supported. Verify price/currency, stock denial, fitment, photo identity/alt text, shipping
  classification and order handoff. Separate CoreYard defects from theme/store configuration
  defects, but resolve both before accepting that installation. API payload assertions alone
  do not prove that buyers can purchase the right part under the intended shipping policy.

## 10. Public documentation, support and evidence

- **DOC-01 — Complete operator path.** Public docs cover install, prerequisites, source mapping,
  policies, preview, first publish, scheduling, monitoring, backups, upgrades, rollback,
  troubleshooting and uninstall. Uninstall stops services without deleting business state
  or remote listings by default. Examples use synthetic data and run as written.
- **DOC-02 — Honest positioning.** Describe CoreYard as a self-hosted integration engine.
  State that source mapping and external accounts are prerequisites, identify supported
  integrations and single-variant assumptions, and avoid implying universal source
  compatibility. A hosted service, GUI, multi-tenant platform and automatic refunds are not
  requirements of this release unless separately added to scope.
- **DOC-03 — Supportability.** Publish a monitored support channel, issue templates, supported
  configurations and troubleshooting decision paths. A redacted diagnostic bundle identifies
  version, capability/config validation, recent outcomes and correlation IDs. Someone other
  than the author resolves a simulated auth failure, stuck queue and stale sync from it and
  the runbooks without database surgery.
- **QA-01 — Continuous gates.** The complete offline suite passes with no `.env`, credentials
  or network on every supported Python version. CI also validates generic example configs,
  CLI help, installed-artifact demo, dependency metadata, neutrality and whitespace. New tests
  exercise real failure boundaries rather than only mirroring implementation. Test count
  or line coverage is not a substitute for the scenario evidence above.
- **QA-02 — Evidence ledger.** Maintain one row per criterion with release SHA/artifact digest,
  owner, environment, procedure/test reference, redacted result link, date and PASS/FAIL/
  NOT VERIFIED status. A criterion may be inapplicable to a particular source or installation
  only where the capability matrix explains why; this never excludes an existing feature
  from release-wide qualification. Keep private operational evidence access-controlled;
  publish a redacted
  qualification summary. No live customer mutation is authorized by this specification.
- **QA-03 — Final acceptance.** The release owner reviews the ledger, open defects, artifact
  contents, migration notes and support readiness. Tag and publish only the reviewed commit.
  Any later artifact/code change invalidates affected evidence and requires re-verification.
- **QA-04 — Artifact provenance and withdrawal.** Trace each distributed artifact to its
  reviewed commit, CI build and tested dependency set using verifiable provenance or a signed
  release manifest. Test verification from the customer's documented install path; a checksum
  on the same download page alone establishes integrity, not publisher identity. Document
  who can publish, how a defective release is marked withdrawn, how customers are notified
  and how they obtain the last qualified version. Never silently replace an existing release
  artifact with different bytes under the same version.

## Initial work identified by the repository review

These are starting items, not an exhaustive claim that all other criteria pass:

1. Fix nonzero command results being recorded as successful runs (OPS-01).
2. Resolve `1.0.0` metadata versus the intended `v0.1.0` release (REL-03).
3. Make diagnostics honor tabular sources and optional capabilities (UX-02, OPS-01).
4. Qualify clean distributed-artifact installs and runtime paths (DIST-03, DIST-04).
5. Create and exercise backup, restore, upgrade and rollback procedures (OPS-04, OPS-05).
6. Establish the complete functionality inventory, capability matrix and evidence ledger;
   qualify every existing feature rather than selecting a smaller release subset.

The baseline review passed 1,057 offline tests on the installed Python 3.14.7 and the
vendor-neutrality/whitespace checks. That is useful development evidence, but does not
establish clean customer installation, live integration behavior, recovery, capacity or
the soak gates. Those remain NOT VERIFIED until evidence is collected.
