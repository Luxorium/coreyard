# CoreYard capability matrix

What CoreYard supports, per source and per feature: the prerequisites, the operations, the
limitations, and what evidence exists today. Required by REL-01.

Two things this document is not. It is not a claim that a supported combination has been
qualified — the **Evidence** column says what has actually been collected, and most of it is
`NOT VERIFIED` (see [the evidence ledger](EVIDENCE_LEDGER.md)). And it is not the authority
on what any *command* needs: that is generated from the command tree into
[`docs/INVENTORY.md`](INVENTORY.md), and enforced at run time.

Capability names below are the ones `coreyard doctor` prints and `coreyard/capabilities.py`
resolves. Run `coreyard doctor --install-only --no-network` to see which of them a given
installation has.

## The capabilities

Each is `on` when configured, `off` when this installation simply does not use it, and
`missing` when something else that *is* on depends on it. Only `missing` is a failure.

| Capability | On when | Used by |
|---|---|---|
| `source` | The configured source is reachable-in-principle: SMB credentials for `database`, an existing file for `tabular` | Everything that reads the yard |
| `database` | The source is specifically the source database | `coreyard schema`, `coreyard images` |
| `schema` | `schema.json` is present and loads | The `database` source; `off` for a tabular source, which is its own mapping |
| `photos` | `SMB_IMAGES_SHARE`, or a readable `COREYARD_SOURCE_IMAGES` directory | `sync photos`, `coreyard images` |
| `donor_photos` | A `donor_images` query is mapped | The donor-vehicle photo fallback |
| `shopify` | `SHOPIFY_STORE` **and** `SHOPIFY_ADMIN_TOKEN` | The Admin API sink, reconcile, repair, audit, counts, bulk, order lifecycle |
| `publications` | `STORE_PUBLICATIONS` names at least one sales channel | Publication; without it a new product is ACTIVE on no channel and returns 404 |
| `orders` | `SHOPIFY_WEBHOOK_SECRET` or `SHOPIFY_CLIENT_SECRET` | Webhook receipt only — polling deliberately needs neither |
| `order_booking` | `YMS_WRITE_ORDERS=1` **and** a complete `order_write` mapping | The one write path into the source database |
| `delta` | A modified-at column is mapped | `sync delta` |
| `alerting` | `COREYARD_ALERT_COMMAND` names a notifier | `coreyard alert` delivery; without it alerts are evaluated and printed but nobody is told |

## Sources

| Source | `COREYARD_SOURCE` | Prerequisites | Supported | Not supported | Evidence |
|---|---|---|---|---|---|
| Source database over SMB | `database` (default) | `SMB_HOST`, `SMB_USER`, `SMB_PASSWORD`; a mapped `schema.json`; SQL Server reachable on the `\\sql\query` named pipe | Everything below that names `database` | — | Offline: mapping, query building and coercion (`tests/test_inventory.py`). Live behaviour on a customer database: `NOT VERIFIED` |
| CSV export | `tabular:/path/parts.csv` | A readable file whose columns are named after `Part` fields | Extract, render, CSV and Shopify output, full and scoped sync, reconcile, repair, audit, counts | Database deltas, `coreyard schema`, `coreyard images`, donor photos, database order booking | Offline: `tests/test_source.py`, `tests/test_capabilities.py`, `tests/test_clean_install.py` (the documented quickstart, on a data root with nothing in it). Live: `NOT VERIFIED` |
| SQLite export | `tabular:/path/parts.sqlite3` | As CSV; rows come from the `parts` table | As CSV | As CSV | As CSV |

A tabular source's "clock" is the file's modification time, which is what `server_now()`
returns. It has no row-level change cursor, so `coreyard sync delta` is refused rather than
approximated — an approximated cursor silently drops rows, and dropping rows is how a sold
part stays on sale.

**Unsupported combinations refuse before any side effect.** `cli.unmet` compares the
command's declared requirements against the resolved capabilities and exits 2 with the
capability, the reason, and the configured source, before the lock is taken and before the
command runs. Refusals are recorded in the run history, so a scheduled job that starts
refusing every tick does not read as a job that stopped being scheduled.

## Outputs

| Output | Prerequisites | Supported | Limitations | Evidence |
|---|---|---|---|---|
| CSV / import file | none | Preview and import-file generation for any source | Cannot retire products; cannot read back what the store holds; no media upload | Offline: `tests/test_transform.py`. Live import: `NOT VERIFIED` |
| Shopify Admin API | `SHOPIFY_STORE` + `SHOPIFY_ADMIN_TOKEN`; `write_products`, `write_inventory`, `write_files`; `STORE_PUBLICATIONS` for sales-channel publication | Create, update, activate, publish, retire, media attach/replace, alt text, metafields | Single variant per product; `productSet` set semantics mean an omitted field is reset, so publishing always sends the whole product | Offline: `tests/test_shopify_api.py`, `tests/test_shopify_client.py`. Live store convergence: `NOT VERIFIED` (SYNC-01) |

## Photographs

| Feature | Capability | Prerequisites | Limitations | Evidence |
|---|---|---|---|---|
| Part photos over SMB | `photos` | `database` source, `SMB_IMAGES_SHARE`, `smbclient` on PATH | Anchored `<R#>_` match; whole-share listing once per full run | Offline: `tests/test_images.py`, `tests/test_images_manifest.py`. Live share: `NOT VERIFIED` |
| Part photos from a directory | `photos` | `COREYARD_SOURCE_IMAGES` pointing at a readable directory | Same naming convention; no manifest stamping equivalence tested against SMB | Offline: `tests/test_source.py`. `NOT VERIFIED` beyond that |
| Donor-vehicle fallback | `donor_photos` | `database` source and a `donor_images` query in `schema.json` | Opt-in and inert by default; a part with its own photographs never consults the donor folder; frames trimmed to `STORE_DONOR_PHOTO_LIMIT` in both resolver and publisher | Offline: `tests/test_donor_photos.py`. Live: `NOT VERIFIED` |

## Sync

| Mode | Command | Needs | Retires? | Limitations | Evidence |
|---|---|---|---|---|---|
| Full | `sync` | `source` | Yes, under the fraction guard | `--limit` blocks retirement entirely | Offline: `tests/test_sync_plan.py`, `tests/test_retire.py`. Live: `NOT VERIFIED` |
| Scoped | `sync inventory` / `photos` / `catalog` | `source` (+`photos` for the photo scope) | Only `inventory` | A scoped run never commits the whole snapshot | Offline: `tests/test_commit_discipline.py` |
| Delta | `sync delta` | `source`, `delta` | Only from evidence, never from absence | Database sources only; subset view, so `state.commit` is forbidden | Offline: `tests/test_delta.py` |
| Bulk | `bulk` | `source`, `shopify` | No | Separate progress file (`out/shopify_bulk_results.jsonl`); does not share sync state | Offline: partial. Handoff with incremental sync: `NOT VERIFIED` (SYNC-04) |
| Counts | `counts` | `shopify` | No | Writes a storefront counter only | Offline: partial |

## Reading back and repairing what is already published

| Feature | Command | Needs | Writes | Evidence |
|---|---|---|---|---|
| Reconciliation | `reconcile` | `source`, `shopify` | Shopify, with `--apply` | Offline: `tests/test_reconcile.py`. Live: `NOT VERIFIED` |
| Copy repair | `repair <aspect>` | `source`, `shopify` | Shopify, with `--apply` | Offline: `tests/test_repair.py` |
| Listing audit | `audit catalog` | `shopify` | Nothing | Offline: `tests/test_audit.py` |

## Setup, diagnostics and scheduling

| Feature | Command | Needs | Notes | Evidence |
|---|---|---|---|---|
| Guided setup | `init` | none | Writes `.env` and `store.json`; ends at a preview, never an automatic live publish | Offline: `tests/test_setup.py`. Independent onboarding trial: `NOT VERIFIED` (UX-01) |
| Config validation | `validate` | none | Offline schema checks for the four external config files | Offline: `tests/test_validate.py` |
| Diagnostics | `doctor` | none | Capability-driven; probes only what is configured; exits 0/1/2 | Offline: `tests/test_doctor.py` |
| Pipeline state | `status` | none | Read-only and always exits 0, by design | Offline: `tests/test_ops.py` |
| Scheduling | `schedule install` / `uninstall` / `status` | none | systemd-user or cron | Offline: `tests/test_schedule.py`. Reboot survival, idempotence: `NOT VERIFIED` (OPS-02) |

## Orders

Order handling is opt-in at every stage, and the stages gate independently: a site can
receive orders without booking them, and can book without letting CoreYard fulfil.

| Stage | Capability | Prerequisites | Limitations | Evidence |
|---|---|---|---|---|
| Webhook receipt | `orders` | `SHOPIFY_WEBHOOK_SECRET` or `SHOPIFY_CLIENT_SECRET`; an inbound HTTPS route | Raw-body HMAC verified in constant time; payloads are never logged | Offline: `tests/test_webhook.py`. Deployment: `NOT VERIFIED` (SEC-02) |
| Polling | — (needs `shopify` only) | Admin API credentials | Latency includes the configured interval; deliberately needs no webhook secret | Offline: `tests/test_orders_poll.py` |
| Work-order booking | `order_booking` | `database` source, a complete `order_write` mapping, **and** `YMS_WRITE_ORDERS=1` or `--write-orders` | The only write path into the source database; one transaction, `SET XACT_ABORT ON`, no `DELETE` | Offline: `tests/test_orders.py`. Actual SQL behaviour on a disposable mapped database: `NOT VERIFIED` (ORD-02) |
| Lifecycle updates | — (needs `shopify`) | `STORE_ORDER_POLICY_FILE` to do more than tag and note | Fulfilment is created only where site policy says nothing else will close the order | Offline: `tests/test_order_lifecycle.py` |

A tabular source cannot book a work order: there is no database to write to. The capability
reports `off` with that reason, and `--write-orders` on such an installation is a
configuration error rather than a silent no-op.


## Alerting

`coreyard alert` evaluates the OPS-03 conditions and notifies the operator once per
condition, again after `COREYARD_ALERT_REPEAT`, and once more when it clears. Schedule it
alongside the other jobs.

| Condition | Key | Threshold | Notes |
|---|---|---|---|
| No full sync has finished | `sync.full.stale` | two of that job's own scheduled intervals | Read from the host's crontab or systemd timer; nothing scheduled means nothing is late |
| The last full sync hit its deadline | `sync.full.timeout` | immediate | The failure a staleness check cannot see, because the job *is* running |
| The last full sync failed | `sync.full.failed` | immediate | Reports the exit code |
| The delta cursor has stopped advancing | `sync.delta.stale` | 15 min (`COREYARD_ALERT_DELTA_STALE`) | Suppressed while a full sync runs, but only for one full cycle past the threshold |
| An order stage is pending or failed | `orders.stuck` | 10 min (`COREYARD_ALERT_ORDERS_STUCK`) | Names orders, never payloads |
| Storage will stop the next run | `storage.unwritable`, `storage.full` | immediate | |
| A required capability is not configured | `config.missing` | immediate | `missing` only, never `off` |

Delivery is a command (`COREYARD_ALERT_COMMAND`) receiving the alert on stdin with
`COREYARD_ALERT_KEY`, `_LEVEL`, `_STATE` and `_SUMMARY` in its environment — no transport
dependency and no vendor. `coreyard alert --test` proves the channel before an outage needs
it. `out/alerts.jsonl` records what was sent; it is an audit trail, not the delivery.

| Property | Status | Evidence |
|---|---|---|
| Delivery exercised through a real command | Supported | `tests/test_alerts.py::DeliveryIsRealAndSafe` |
| Recovery notification exercised | Supported | `tests/test_alerts.py::NotifyOnceThenRemind` |
| A failed delivery is retried rather than recorded as sent | Supported | `tests/test_alerts.py` |
| Alerts carry no customer payload or secret | Supported | `tests/test_alerts.py::AlertsCarryNoCustomerData` |
| Delivery to a real paging service on a customer host | `NOT VERIFIED` | OPS-03 |

## Where an installation keeps its files

CoreYard writes only under its **data root**, resolved once per process: `COREYARD_HOME` if
set, else the checkout when running from one and it is writable, else
`$XDG_DATA_HOME/coreyard` (or `~/.local/share/coreyard`). It holds `.env`, `store.json`,
`schema.json`, `coreyard_sync_state.sqlite3`,
`coreyard_webhook_queue.sqlite3` and `out/` — previews, logs, locks, caches and progress
files. The installation directory holds only code and is never written to.

| Property | Status | Evidence |
|---|---|---|
| A source checkout resolves to itself, so an upgrade moves no existing state | Supported | `tests/test_data_root.py::ASourceCheckoutIsUnchanged` |
| Runs from an arbitrary working directory | Supported | `tests/test_data_root.py` |
| Never writes into the package installation | Supported | `tests/test_data_root.py::CodeAndDataAreSeparate` |
| Two installations on one host keep identity, locks, credentials and state separate | Supported | `tests/test_data_root.py::TwoInstallationsOnOneHost` |
| Paths containing spaces | Supported | `tests/test_data_root.py` |
| Installation into a genuinely read-only tree, on every advertised OS/Python pair | `NOT VERIFIED` | DIST-03 |

## What no combination supports

- More than one variant per product.
- Any model, LLM or inference service. Titles come from the renderer and prices from the
  source database, so the same part produces the same listing on every run.
- Automatic refunds, a hosted service, a GUI, or multi-tenancy (DOC-02).
- Atomic reservation across Shopify and the source database. They share no
  transaction; the cross-channel oversell exposure is real and is documented rather than
  promised away (SYNC-05).
