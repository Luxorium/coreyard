# Running CoreYard

The day-to-day of a live installation: the four commands, what a sync does and how to scope
it, reconciling and repairing a catalogue that has drifted, how shipping and photographs are
decided, what happens when a part sells, and what to put in cron.

[← back to the README](../README.md)

## Running

The day-to-day loop is four commands:

```bash
bin/coreyard doctor          # is this installation wired up, and are the jobs running?
bin/coreyard status          # what does the pipeline believe right now?
bin/coreyard sync --dry-run  # what would a sync do?
bin/coreyard sync            # do it
```

`bin/coreyard --help` lists every command; each one's `--help` describes its own flags and
says whether it writes. `python -m coreyard` is the same CLI, and `coreyard` is on your PATH
after `pip install -e .`.

CoreYard writes only under its **data root**: `.env`, `store.json`, `schema.json`,
the state and queue databases, and `out/`. Run from a checkout that is the
checkout, so nothing an existing installation has ever used moves. Installed as a package it
is `$XDG_DATA_HOME/coreyard` (or `~/.local/share/coreyard`), because a package must not
write into its own installation directory — that fails on a read-only tree, and the next
upgrade would replace the directory holding your sync state. `COREYARD_HOME` overrides it,
which is also how two installations share one host without sharing store identity,
credentials, locks or state. The code location (`bin/coreyard`, the virtualenv, the bundled
examples) is separate and stays with the installation.

A command that cannot run on this installation says so before it starts, naming the
capability it needs and the source you configured, and exits 2 without taking a lock or
changing anything — `coreyard sync delta` on a CSV export, for instance, because a file has
no row-level change cursor. [`docs/CAPABILITY_MATRIX.md`](docs/CAPABILITY_MATRIX.md) says
which combinations are supported.

`coreyard alert` is the piece that pushes rather than waits: it evaluates the same
conditions on a schedule and notifies you once when something breaks, again if it is still
broken much later, and once more when it recovers. Delivery is whatever command you put in
`COREYARD_ALERT_COMMAND` — `mail`, `curl` to a webhook, a paging CLI — so CoreYard carries
no transport dependency. Prove the channel with `coreyard alert --test` before you need it.

`doctor` reports in three sections: what this installation is *configured* to do, whether it
can reach what it uses, and whether the scheduled work is still happening. The first section
is why the rest is short — a capability that is off is not probed, so a yard reading a CSV
export is never asked for SMB credentials and a site that has not enabled order booking is
never told its order log is missing. Only a capability that something enabled here *depends
on* is reported as a failure. `--json` adds the same capability map for a monitor to read.

```bash
# 0. Prove connectivity (whatever this installation is configured to use)
bin/coreyard doctor

# 1. Eyeball the extract
.venv/bin/python -m coreyard.yms.inventory 10   # first 10 priced-and-available parts

# 2. Preview: first 25 parts to a CSV you can eyeball / import to a test store
bin/coreyard sync --sink csv --limit 25 --dry-run

# 3. Incremental run (diffs against coreyard_sync_state.sqlite3)
bin/coreyard sync --dry-run      # report the diff, write nothing
bin/coreyard sync                # publish adds/changes, retire sold parts
bin/coreyard sync --deep         # ...and ask the live store, rather than the snapshot

# 3b. Catch-up run: only what changed since the last cursor (seconds, not minutes)
bin/coreyard sync delta --dry-run
bin/coreyard sync delta

# 3c. One kind of change at a time, or one part
bin/coreyard sync inventory      # availability: new, revived, repriced, restocked, retired
bin/coreyard sync photos         # only parts whose photo set moved on the share
bin/coreyard sync catalog        # only parts whose rendered copy moved
bin/coreyard sync --r-number 51 --dry-run

# 4. Bulk catalog load — photos, fitment, SEO text, weights, inventory; DRAFT products
bin/coreyard bulk --limit 50 --workers 4
bin/coreyard bulk                # everything the listable policy allows

# 5. Ask the store what it actually holds, rather than trusting the state file
bin/coreyard reconcile           # plan only
bin/coreyard audit catalog       # read-only listing-quality report
```

### Scopes, and what they are not

`sync inventory`, `sync photos` and `sync catalog` are *selectors over one plan*, not three
engines. There is one extract, one renderer and one diff; a scope only decides which of the
parts that moved this run is allowed to act on. Each scope owns a projection of the same
rendered product, so a repriced part is an inventory change and a retitled one is not.

Two consequences worth knowing:

* **Only `sync inventory` (and a full `sync`) may retire.** A photo run has no evidence
  about whether a part is still in the yard, so it never archives anything.
* **A scoped or `--r-number` run never replaces the snapshot**, it only records what it
  actually published. Committing would mark every part it *declined* to publish as up to
  date, and the change it skipped would never be published by anything.

### Long runs and overlap

```bash
bin/coreyard --lock sync --timeout 45m sync
```

`--lock NAME` holds `out/.NAME.lock` for the run and exits quietly (status 0) if another run
already has it — a skipped tick is the intended outcome when a full sync is still going.
`--timeout` stops a long run cleanly, exiting 124. Both are global flags, so they go before
the command.

A skipped tick is recorded, and `bin/coreyard status` counts them beside the last run that
actually ran: "Last full sync 4h ago  ok  47 tick(s) skipped since" is a job being starved by
whatever else holds that lock, which otherwise looks identical to a job nobody schedules any
more. The staleness alerts ignore skips deliberately, so being turned away never reads as
having run.

A run that is stopped this way **keeps the work it had already published**: progress is
banked into the snapshot every 100 products. Without that, a backlog larger than one timeout
window can never be worked off — each run publishes what it can, records nothing, and the
next starts again from the beginning.

### Reconciling, repairing, auditing

The incremental sync compares the yard against its own fingerprint snapshot. That is the
right trade for a run on a timer, and it is blind in one specific way: it never asks Shopify
what it actually holds. A part the snapshot calls "published" is invisible to every later
run, whether or not the product exists, is a draft, or ever reached a sales channel. Three
commands close that gap, and none of them writes without `--apply`.

```bash
bin/coreyard reconcile                       # plan: create / activate / revive / retire / publish
bin/coreyard reconcile --apply --activate    # promote listable drafts too
bin/coreyard reconcile --repair-state        # forget snapshot entries whose product is gone
```

Reconciliation compares the yard's listable parts with the store and reports five buckets —
listable parts with no product, listable drafts, archived parts that came back, ACTIVE
products no longer listable, and products on no sales channel — plus where the state file and
the store disagree. It keeps the sync's retirement guards: more than `--max-retire-fraction`
(default 10%) of the active catalogue is refused unless you pass `--force-retire`, because a
yard database that answered partially looks exactly like a yard that sold out.

```bash
bin/coreyard repair titles --dry-run         # what an older renderer left behind
bin/coreyard repair titles --apply
bin/coreyard repair tags   --dry-run         # e.g. vehicle tags with a doubled make
bin/coreyard repair seo --apply --limit 200
bin/coreyard repair weights --apply          # products published before weights were owned
bin/coreyard repair metafields --apply       # grade, mileage, condition, fitment
bin/coreyard repair all --dry-run
```

Repair asks the store what each product says, asks the canonical renderer what it should say,
and rewrites only the differences. It is idempotent, so a re-run finds only what still
differs and an interrupted run simply resumes — no progress file. Tag repair reports which
tags a rewrite would delete *before* it does, and never removes a tag another system owns.

```bash
bin/coreyard audit catalog                   # missing SKU, no photos, zero weight, …
bin/coreyard audit catalog --show 20 --json out/audit.json
```

### Finding the listing from the part

Everything above runs from the storefront's side. `links` runs the other way: it writes each
published part's storefront address back onto the part in the yard system, so whoever has the
record open — the counter, the phones — can reach the listing without searching the store for
a title they would have to guess.

```bash
bin/coreyard links                           # plan only
bin/coreyard links --apply                   # write the addresses into the yard
bin/coreyard links --apply --limit 25        # a cautious first pass
bin/coreyard links --apply --r-number 44004  # one part
bin/coreyard links --apply --show-sql        # print the batches instead of running them
```

It is a reconciliation, so re-running is how it stays true: newly listed parts are stamped,
addresses already right are left alone, and a part whose listing has gone has its address
cleared — nobody is sent to a page that 404s. Only a product a shopper can actually open
earns an address; the store is asked, rather than the address being assumed from the handle.

Two things it will not do. It will not overwrite a description somebody at the yard typed —
those rows are reported as `skipped` and left exactly as written — and what it writes never
reaches a shopper: the renderer drops a bare address on the way back out, so no listing is
republished to say nothing new.

Which field it writes is `backlink_write` in `schema.json`, and choosing it is the decision
worth thinking about. A plain text column the application only displays is the right kind of
target. A grid backed by another integration's table is not, however conveniently it renders
a hyperlink — those rows belong to that integration, which may act on one it did not create.

**Expect a burst on any outbound feed your system runs.** Applications of this kind log every
row they see change for their own inventory exports, and a first full pass changes tens of
thousands of rows at once. Nothing is wrong with what it sends — each part's data is
unchanged — but the queue drains for a while afterwards, so run the first pass when a burst
on that feed does not matter. Later runs stamp only what was newly listed, which is a handful.

### How a part ships, and the tag that says so

A storefront has to tell a shopper *before* checkout that a door is pickup-only, and it
reads that from a tag. Whoever writes that tag makes a promise: get it wrong and a
freight-only engine quotes free ground, or a shippable alternator refuses to ship.

Point `STORE_SHIPPING_POLICY_FILE` at a file describing your groups and CoreYard applies the
right tag during the publish that creates the product — not in a later pass, which is a
window in which a product is live, buyable, and wearing the wrong shipping or none:

```json
{
  "groups": {
    "PICKUP": {"tag": "ship:pickup-only", "price": null,     "label": "Pickup only",
               "match": ["door assembly", "glass"], "exclude": ["glass channel"],
               "fulfillment": "yard"},
    "A":      {"tag": "ship:freight-299", "price": "299.99", "label": "Freight",
               "match": ["engine assembly"],        "fulfillment": "yard"},
    "GROUND": {"tag": "ship:free",        "price": "0.00",   "label": "Free ground",
               "default": true,                     "fulfillment": "external"}
  },
  "match_order": ["PICKUP", "A"]
}
```

Groups are tried in `match_order`, first substring hit against the part type wins, and
exactly one group is the `default` that catches the rest. `exclude` names what a pattern would
otherwise swallow: substring matching cannot say *glass but not glass channel*, and the two
belong in different groups — one is a two-person lift, the other fits in an envelope. Ordering
cannot settle it either, because the group that should win is the `default`, and a default
carrying match patterns is refused for good reason. `fulfillment` says who ships it —
`external` means another system buys the label and will close the order out, which is what
stops `orders sync-status` fulfilling a parcel order before the shipping app has attached a
tracking number. Because the shipping policy already declares that, the order policy derives
its fulfillment rules from it rather than repeating them.

The tag is part of the rendered product, so it is fingerprinted like any other
shopper-visible field, and CoreYard then **owns that namespace**: a stale `ship:` tag from
whatever wrote them before is replaced rather than accumulated next to the new one. Two
shipping tags on one product and the storefront reads whichever it tests for first.

### Parts with no photographs of their own

A yard photographs the cars it buys and only some of the parts it pulls, so a large slice of
the catalogue is listable in every respect except that it has no picture. Map a `donor_images`
query in `schema.json` and those parts publish with photographs of the vehicle they came off.

It is opt-in and one-way. **A part that has its own photographs never consults the donor
folder**, so nothing already published moves; without the mapping an unphotographed part
publishes with no images exactly as before, and the extra share listing is skipped entirely.
A part inherits only the opening frames — the general views — because a donor shoot documents
a whole car (a median of 16 frames, sometimes 99) and was taken to record a vehicle, not to
sell one bracket off it. `STORE_DONOR_PHOTO_LIMIT` sets how many; the default is 6.
Set it to `0` to inherit every donor frame. Scheduled full photo scans detect newly added
frames as well as replacements; parts with their own photos continue to use those.

Each donor frame is uploaded to Shopify **once** and referenced by every part off that donor,
rather than staged per product — roughly seven parts come off each car, so the naive version
uploads the same photograph seven times.

The listing says whose picture it is. The profile's `donor_photo_note` renders above
everything else in the body — "Photographs show the donor vehicle (2016 Buick Encore (Stock
#251026)). This part was not photographed individually." — and the alt text describes the
vehicle rather than the part. That is a fact about the data rather than a claim about your
business, so CoreYard states it for you; reword it in your profile if you like, but a shopper
must not be left believing the picture is of the part.

### Structured product data

The catalogue is not only prose. Grade, mileage, condition and full fitment are structured
values a theme can render as a table or a spec row, so CoreYard publishes them as metafields
rather than leaving a storefront to parse them back out of a description:

| Metafield | Type | Value |
|---|---|---|
| `<ns>.grade` | `single_line_text_field` | the source system's condition grade |
| `<ns>.mileage` | `number_integer` | donor mileage |
| `<ns>.condition` | `single_line_text_field` | the profile's condition wording |
| `<ns>.fitment` | `json` | `[{"years","make","model","note","label"}, …]` |

`<ns>` is `metafield_namespace` from the profile, defaulting to `coreyard`. Each fitment row
carries a `label` that is exactly the vehicle tag CoreYard also emits, so a theme can link to
a tag-filtered collection without reverse-engineering one from a title.

A part with no catalogue fitment still fits the car it came off, so its donor vehicle is
emitted as a single row — otherwise anything built from fitment would silently lose every
part the interchange catalogue does not cover. Values the source did not supply are left out
rather than published empty, and a value that later disappears is deleted from the product
rather than left showing something untrue.

### Tags another system owns

`productSet` replaces the whole tag list, so publishing only CoreYard's tags would delete
whatever else is on the product. That matters because storefront systems really do own tags:
a theme that has to warn "this part is pickup only" before checkout reads it from one, and
losing it turns a warning into a dead checkout the shopper discovers at the end.

The rule is ownership. CoreYard owns what it generates, so a stale generated tag is replaced
rather than accumulated — that is what lets `repair tags` repair anything. Anything
*namespaced* (`ship:free`, `promo:winter` — any tag containing `:`) is by definition somebody
else's, because CoreYard never emits one, and it is carried through every update untouched.
Systems that write plain tags can be named with `STORE_PRESERVED_TAG_PREFIXES` or the
profile's `preserved_tag_prefixes`.

Photo alt text is written when a product's photos are first attached. To fill it in on photos
published before that existed:

```bash
bin/coreyard altfix --dry-run --limit 20   # report what would change
bin/coreyard altfix                        # apply to the whole catalogue
```

`write_files` is required for this (see [the scope
list](SETUP.md#4-shopify-admin-api-token)); without it Shopify rejects the
update with `ACCESS_DENIED`.

**Adding a scope does not upgrade an existing token.** Shopify grants scopes at install
time, so after changing the app's scope list you must re-authorize to mint a new token:

```bash
bin/coreyard oauth        # re-runs consent, rewrites SHOPIFY_ADMIN_TOKEN in .env
```

Check what a token actually carries with
`{ currentAppInstallation { accessScopes { handle } } }`.

### Online orders → a work order in the yard system

A paid storefront order has to reach the counter as something a puller can act on, and it has
to stop the part being sold again. CoreYard does both by booking the sale into the source
system — which already renders a work order in a printable form — and archiving the product.
It writes no document of its own: a second sheet of paper would be a copy of the customer's
details with its own retention problem, and a chance for the two to disagree about what sold.

```bash
bin/coreyard orders register --url https://yard.example.com/webhook  # ORDERS_PAID
bin/coreyard orders serve --write-orders
bin/coreyard orders status                      # recent deliveries
bin/coreyard orders retry --id <webhook-id>     # retry only failed stages
bin/coreyard orders replay out/test_order.json  # re-run, no store or signature needed
```

If the yard machine cannot accept an inbound connection — behind NAT, no port, no tunnel —
poll instead. It is a different transport onto the same pipeline, not a second one: every
order it finds goes through the same queue, the same worker, the same optional booking and
the same delisting, and the queue de-duplicates on order identity, so running both during a
migration cannot book anything twice.

```bash
bin/coreyard orders poll --check                # scopes and connectivity, writes nothing
bin/coreyard orders poll                        # since the stored cursor, else 2 days
bin/coreyard orders poll --since 2026-08-01
bin/coreyard orders poll --write-orders
```

Registration uses Shopify's `ORDERS_PAID` topic. The receiver also requires
`financial_status=paid` before printing, booking, or retiring anything. During migration an
older `ORDERS_CREATE` subscription is safe: unpaid deliveries are acknowledged and ignored,
and paid duplicates are de-duplicated by order identity as well as webhook delivery ID.
Remove the legacy subscription after confirming the paid subscription is active.

`serve` binds to `127.0.0.1:8787` by default — put TLS in front of it (a reverse proxy or a
tunnel); Shopify only delivers to `https://`. Every request is checked against the app's
signing secret with a constant-time compare, and an unsigned or mis-signed POST is refused
without explanation. Deliveries are queued by `X-Shopify-Webhook-Id`, so Shopify's
at-least-once retries can't print an order twice, and the receiver answers immediately rather
than holding the connection open while a printer warms up.

Sold parts are archived on Shopify as each paid order arrives, which closes the window where a
part stays buyable until the next timer tick. `--no-retire` leaves them on sale. Order payloads
carry a customer's name, phone and address, so no request body is logged and no copy is written
outside the queue. Successful payloads are securely erased immediately. Failed payloads remain
in the owner-only queue for seven days so only their failed booking or retirement stage can be
retried. Configure the window with `COREYARD_WEBHOOK_ERROR_RETENTION_DAYS`.

Run `orders serve` under a process supervisor with automatic restart, and keep it bound to
loopback behind an HTTPS reverse proxy or tunnel. Monitor `orders status` for error rows.
Cancellation/refund reversal is deliberately manual: restoring source inventory would be a
second database write operation whose invariants must be observed and reviewed separately.

### Booking the sale into the yard system

Optionally, the same delivery creates a real order in your source database, so the storefront
does not become a second set of books:

```bash
bin/coreyard orders serve --write-orders
.venv/bin/python -m coreyard.yms.orders out/test_order.json  # print SQL, write nothing
bin/coreyard orders status                           # deliveries and their order numbers
```

This is the **only** write CoreYard makes to your database. It requires an `order_write`
section in local `schema.json` plus explicit runtime enablement: `YMS_WRITE_ORDERS=1`,
`--write-orders`, or `--execute` for the manual command. Without the mapping and an explicit
opt-in it stays read-only, so an upgrade never starts writing to a system of record on its own.
Read the `_order_write` notes in `schema.example.json` before filling it in — most
systems of this kind have no identity columns and no foreign keys, so ids come from counter
tables the application is allocating from concurrently, and nothing implicit will fix a
mistake.

What CoreYard guarantees, whatever names you map: the order is one transaction under
`SET XACT_ABORT ON`, so it commits whole or changes nothing; ids are allocated in a single
`UPDATE ... OUTPUT` under `UPDLOCK` and checked unused before insert; the inventory decrement
carries a quantity floor and must affect exactly one row, so an oversell fails loudly instead
of going negative; and nothing is ever deleted. Because the write is atomic and idempotent,
retrying after a failure is safe.

A redelivery is refused by the database, not just by the queue: the storefront order reference
is stored on the order and a second attempt returns the existing number instead of booking the
sale twice. Booking and delisting fail independently: if the booking does not land, the part is
still taken off sale and the failure is recorded against the delivery in
`bin/coreyard orders status`. Retry it within the retention window with
`bin/coreyard orders retry --id <webhook-id>`, which reruns only the stage that failed.

**Tax is a decision you have to make explicitly.** If your storefront already collects and
remits sales tax, leave `line_items_taxable` false, or the same sale is taxed twice. Do not
assume your customer account can carry the exemption — check whether your customer table even
has such a column first. Booking online sales under the customer number your existing
e-commerce integration uses is worth doing for consistency, but it is not a tax mechanism.

### Telling the storefront what the yard did

The sale reaches the yard system as a work order, and what happens next — picked, invoiced,
shipped — happens there. The storefront never hears about it, so a buyer looking at their
account sees "unfulfilled" on an order that left the building yesterday.

```bash
bin/coreyard orders sync-status              # plan; writes nothing
bin/coreyard orders sync-status --apply
```

It reads an `order_status` query from your `schema.json` — which tables record an invoice,
and how a storefront order links back to one, are your system's business — tags the matching
Shopify order, and appends a note carrying the source reference so the two systems can be
reconciled by hand.

Whether it also **fulfills** the order is deliberately configuration, in
`STORE_ORDER_POLICY_FILE`. Shopify has no "picked but not shipped" state: creating a
fulfillment closes the fulfillment order. If a third-party shipping app is going to buy the
label and attach the tracking number, it needs that left open, and fulfilling early costs the
buyer their tracking email. If nothing else will ever touch the order — a freight shipment
booked by phone, a counter pickup — then leaving it open means it stays unfulfilled forever.
So the site describes its own arrangements, keyed on the product tags its storefront already
uses, and an order with any line another system will ship is left alone. With no policy file,
nothing is ever fulfilled and the command only tags and notes.

### Taking sold parts off sale

A part leaves the listing when it drops out of your `schema.json` `scope` predicate — sold,
unpriced, blocked, or on an open work order. `--sink api` then zeroes its inventory and sets it
to `ARCHIVED`. It is never deleted: the product, its photos and its URL stay put, so an old link
or a search result lands on a real page rather than a 404, and the part can be revived.

Revival is automatic. A part can come back — a voided work order returns it to the yard — so the
status a product held before it was archived is recorded in the sync state, and a later run puts
it back exactly as it was. Without that memory the status read-back that stops a sync from
un-publishing a live product would re-send `ARCHIVED`, quietly restoring the part's stock onto a
product no shopper can see. Only archived products are revived, so a status you set by hand
always stands.

Scope is where the delisting rule lives, so put the work-order exclusion there — a part being
pulled for a customer is still priced and still in stock, and without that clause it stays
buyable and can be sold twice. `schema.example.json` has the `NOT EXISTS` pattern under `_scope`.

Retirement is guarded, because "missing from the extract" is also what a half-finished run or a
broken join looks like:

| Guard | Behavior |
|---|---|
| `--limit` is set | never retires — the run only saw part of the yard |
| removals > `--max-retire-fraction` (default 10%) | refuses, and says so; override with `--force-retire` |
| a retire call fails | logged and skipped; the R# stays in the state file and is retried next run |

Anything not retired is carried forward in the snapshot at its old fingerprint, so a refused
batch is re-detected rather than silently forgotten.

A part that fails to *publish* is carried the same way, and the reason is remembered against
it. `bin/coreyard status` names the parts still failing, three at a time with the error the
storefront gave, and a part drops off that list as soon as one publish of it succeeds. The
run summary's `failed` count cannot answer the question that matters — three failures every
tick reads the same whether it is three different parts or the same three since Tuesday.

Photo changes are picked up the same way: the share is listed once per run and folded into the
fingerprint, so a part that gains or loses a photo is republished with its media rebuilt. Pass
`--no-image-scan` to skip that listing if you don't need it.

`shopify_bulk` is the path to use for the initial catalog load. It is resumable: every attempt
is appended to `out/shopify_bulk_results.jsonl`, and a re-run skips R#s already logged `ok`, so
transient upload failures simply retry on the next run. That log is what the bulk run resumes
*itself* from, and it is the only thing a `--no-resume` run ignores.

It also hands its work over to the incremental path. Each part it publishes is recorded in
`coreyard_sync_state.sqlite3` at the same fingerprint a sync would compute — banked in batches
as the run goes, and whatever happens to the run — so `bin/coreyard sync` straight after a
bulk load finds the catalogue unchanged instead of publishing all of it again through the slow
path. Only parts that actually published are recorded; a part that failed is still waiting for
a sync, which is exactly where you want it. If the state database cannot be written the load
carries on and says so: the cost is a sync that republishes, which is what happened before
there was a handoff at all.

Re-running is safe either way: products are keyed by the stable handle
`<SHOPIFY_HANDLE_PREFIX>-<R#>` and upserted with `productSet`, and photos are uploaded only when
the product has none.

> **Choose `SHOPIFY_HANDLE_PREFIX` once.** The handle is the storefront's primary key — it is how
> every later run finds the product it already published. Changing it on a live store makes every
> product look new and duplicates the whole catalog. If you are migrating an existing store, set
> the prefix to whatever your products already use.

Inspect any part's photos directly:
```bash
bin/coreyard images 51 --fetch
```
Set `COREYARD_SMB_DEBUG=1` to trace the SMB pipe traffic when a connection misbehaves.

### Catch-up runs (`--delta`)

A full sync reads every part to discover the few dozen that moved, which is why it can only
run hourly — and that hour is the window where a part sold at the counter (or on another
sales channel) stays buyable on the storefront. `--delta` asks the cheaper question instead:

```bash
bin/coreyard --sink api --delta --dry-run   # what would change
bin/coreyard --sink api --delta             # publish it
```

It needs `modified_at` in `schema.json` (plus `image_changes`, if photographing a part does
not touch the part row — it usually doesn't). Measured against a live 26k-part yard, a
24-hour window resolves in well under a second, so this is affordable every few minutes.

Two properties make it safe to run unattended:

* **Retirement is evidence-based.** A full run infers "sold" from absence, which is why it
  needs the fraction guard — a broken query also produces absence. A delta run never reasons
  from absence: it retires a part only when it has the row in hand and that row says
  out-of-scope. It also refuses to retire more than `--max-retire-fraction` in one pass.
* **The cursor is the source server's clock**, read before the query and stored with a
  deliberate overlap, so a write committed mid-run is re-examined next time rather than
  skipped. Re-examining costs nothing — publishing is idempotent.

A delta run is a catch-up, not a replacement. Only a full sync sets the cursor, because only
a full sync has just reconciled the whole yard; keep both scheduled.

It also cannot be narrowed: `sync delta --r-number ...` and `sync delta --limit ...` exit 2
without reading anything. The cursor advances past the whole window, so publishing a slice
of it would leave the rest unpublished — and, once the cursor has moved, no longer
detectable as changed. Use `sync --r-number` when you want specific parts.

## Ongoing sync (scheduled)

State in `coreyard_sync_state.sqlite3` makes the sync incremental: only new/changed parts are
pushed, and parts no longer priced-and-available are retired. Use the scheduling helper
(systemd when available, cron fallback):

```bash
bin/coreyard schedule install --every 1h                      # the full extract
bin/coreyard schedule install --every 5m --task "sync delta"  # the cheap catch-up
bin/coreyard schedule status
```

A typical installation runs three cadences, each stopping itself rather than relying on the
scheduler to do it:

| When | Command | Why |
|---|---|---|
| every 5 min | `coreyard --lock sync --timeout 4m sync delta` | closes the window where a part sold at the counter stays buyable online |
| hourly | `coreyard --lock sync --timeout 45m sync` | the full extract |
| daily | `coreyard --lock sync --timeout 45m sync --deep` | the only run that asks the store what it actually holds |
| every 1 min | `coreyard --lock counts counts --apply` | refreshes the exact source-inventory count shown by the theme; edits no products |

The delta job shares the full sync's lock on purpose: if a full run is in progress it already
supersedes the catch-up, so the tick is skipped rather than raced.

Whatever schedules them, the jobs should invoke `bin/coreyard`. The old `python -m
coreyard.<module>` entry points still work and are still tested, but the launcher is the
surface `coreyard --help` and the CLI's own tests both walk — so a renamed entry point breaks
a test rather than a production job.

`coreyard counts` is dry-run by default. It uses one `COUNT_BIG(DISTINCT ...)` query over the
configured saleable source scope and intentionally ignores the storefront-only photo gate;
`coreyard counts --apply` stores that result and its observation time as shop
metafields. Shopify Liquid supplies the separate listed-online count from the `all` collection
on each page render. The source database and its credentials are never exposed to a browser.
