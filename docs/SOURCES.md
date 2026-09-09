# Adding a yard system

CoreYard was written against one yard's SQL Server, reached over an SMB named pipe, with the
table and column names supplied at runtime by `schema.json`. That made the *schema* portable
and left the *transport* welded in place. This document is the seam that fixes that: what a
source has to do, what it may decline to do, and how to add one without forking anything.

[← back to the README](../README.md) · [architecture](ARCHITECTURE.md) · [capability
matrix](CAPABILITY_MATRIX.md)

## First: you may not need an adapter

A new adapter is for a new **transport**. If the yard system you are adding keeps its
inventory in SQL Server and you can reach it over the same named pipe, it is not a new
source — it is a new `schema.json`, and everything below is unnecessary. The mapping is
expressed as SQL fragments precisely so that a different vendor's tables are a configuration
change.

Write an adapter when the answer to "how do I get a part out of this system" is different in
kind: a REST API, a different database engine, a nightly export in a format `tabular` cannot
read, a file drop on an FTP server.

Two ship today. `database` is the original: SQL Server over the named pipe, mapped by
`schema.json`. `tabular` reads a CSV or SQLite export whose columns are named after `Part`
fields — the offline demo, and the path for a yard on a system nobody has written an adapter
for yet.

## What a source is

A source answers a handful of questions about parts. It knows nothing about Shopify,
listings, fingerprints or state, and everything downstream consumes
[`Part`](../coreyard/models.py) and cannot tell implementations apart.

```python
class Source(Protocol):
    def parts(self, limit=None, images_only=None) -> list[Part]: ...
    def parts_by_r_number(self, r_numbers: list[str]) -> dict[str, Part]: ...
    def listable_r_numbers(self, images_only=None) -> set[str]: ...
    def server_now(self) -> datetime: ...
    def ping(self) -> str: ...

    @classmethod
    def probe(cls, argument: str) -> tuple[str, str, tuple[str, ...]]: ...
```

The semantics that are not obvious from the signatures, each of which the conformance battery
checks:

**`parts` and `listable_r_numbers` must answer the same question.** The second is the cheap
half of reconciliation — it reads identities instead of rendering a catalogue — and when the
two disagree, sync publishes a part and reconciliation archives it on the next tick, forever.
This is the most expensive way for an adapter to be subtly wrong, because a flapping
catalogue looks like a Shopify problem for a long time before it looks like this.

**`parts_by_r_number` is not filtered by listability.** It exists to answer "where is this
part and what is it" for a part that has just sold, which is exactly when it stops being
listable. An identity it cannot find is simply absent from the result — not an exception, and
not a blank `Part`.

**The listable policy is one definition, and every source applies all of it.** A part
reaches the storefront only if it has an identity, a positive price, and available stock —
and, where the site sets `STORE_REQUIRE_IMAGES`, at least one photograph. `Part.is_listable()`
enforces the first two for everyone. The other two are the source's job, because the original
adapter enforces them in its SQL `WHERE` clause and an export has no `WHERE` clause: a source
that does not apply them itself applies them nowhere. Both halves of that were wrong until
2026-09-08 — a CSV yard with `STORE_REQUIRE_IMAGES=true` published every part unphotographed,
and a CSV row with quantity `0` was published as an ACTIVE product that reconciliation then
kept active forever, because the source went on calling it listable.

Absent is not zero. An export that names no quantity column is a list of parts the yard has,
and both shipped adapters read it that way.

**`parts` and `listable_r_numbers` must not disagree about any of that either** — see above.
The `ListablePolicyContract` mixin in `tests/test_source_contract.py` checks the whole policy,
including the two views agreeing on every edge case; mix it in alongside `SourceContract` and
implement `source_from_rows`.

**Every part needs an `r_number`.** It is the Shopify SKU, the handle key, the photo stem and
the state key at once. A part without one cannot be published, retired or looked up again.

**`server_now` is the *source's* clock, never this host's.** Delta runs anchor their cursor to
it, and two machines that disagree by a few minutes would otherwise skip every row written
during the offset. It must round-trip through `isoformat()`/`fromisoformat()`, because that is
literally how the cursor is stored. Timezone awareness is your choice — SQL Server's
`GETDATE()` has no zone, a file's mtime is local, and `doctor` handles either — but do not
change flavour between calls.

**`probe` is offline and cheap.** It runs before every command to decide whether the command
can run at all, so it may read configuration and stat a file, but must not connect,
authenticate or query. Reaching the server is `ping`'s job. It returns
`(state, detail, keys)`: one of capabilities' `ON`/`OFF`/`MISSING`, a line explaining the
verdict, and the settings that decide it — so a diagnostic can tell an operator what to fix.

## What a source may decline to do

Not everything is required, and a source says so itself by declaring `SourceTraits`. Before
this existed, the rest of CoreYard compared `COREYARD_SOURCE` against the string `"database"`
wherever a decision depended on the answer — about twenty-five comparisons across five
modules, every one phrased as a binary. A yard system that is neither the original nor a flat
file had to be correct in all of them, which is another way of saying it had to be a fork.

| Trait | Answering `False` means |
|---|---|
| `needs_schema_mapping` | No `schema.json` is asked for. Your columns are already `Part` fields. `coreyard schema` and the `database` capability are off. |
| `needs_database_config` | `YMS_DB_HOST` and `YMS_DB_NAME` are not required configuration. |
| `needs_smb` | The `SMB_*` block is not required. Set it `True` only if you genuinely reach an SMB server. |
| `carries_own_photos` (`True`) | You attach `part.images` yourself. Nothing lists a photo share on your behalf — and nothing shells out to `smbclient` for a yard that has no share. |
| `supports_delta` | No changed-since cursor. `sync delta` is unavailable rather than broken, and the cursor read is skipped silently rather than reported as a failure. |
| `supports_fitment` | No interchange catalogue to resolve against; titles are built from what the part itself carries. |
| `supports_order_booking` | A storefront sale is never written back. The order still reaches Shopify's side of the pipeline; nothing is written to your system. |

Declaring a trait `False` is not a defect. It is how an unsupported combination fails
*before* side effects with a useful explanation, which REL-01 requires and which is the whole
reason `cli.SUPPORT` can refuse a command up front instead of half-way through a sync.

## Adding one

1. **Write the adapter** in `coreyard/source/<name>.py`: the six methods above, plus a
   `TRAITS = SourceTraits(...)` declaring what it can do.
2. **Register it** in `_ADAPTERS` in [`coreyard/source/__init__.py`](../coreyard/source/__init__.py).
   One line, and it is the only place: capabilities, diagnostics and configuration all reach
   the adapter through it.
3. **Add a conformance subclass** in `tests/test_source_contract.py`:

   ```python
   class MyYardSystemContract(SourceContract, unittest.TestCase):
       def source(self):
           return MyYardSystem(fixture_path)
   ```

   That is the whole test file. The battery it inherits covers identity, uniqueness, the
   `parts`/`listable_r_numbers` agreement, lookup semantics, limits and the clock. Every
   registered adapter additionally gets the static checks — registration, declared traits,
   signatures against the Protocol, and an offline probe — with no work at all.
4. **Add a row** to [the capability matrix](CAPABILITY_MATRIX.md) saying what your source
   supports, what it does not, and what evidence exists.

If your source cannot be constructed offline — the `database` adapter cannot, since it needs
a yard — write what can be checked without one. `TheDatabaseAdapterForwards` in the same file
is the pattern: it holds no logic of its own, so what is pinned is that it passes every
argument through unchanged, because dropping `images_only` there would quietly publish
unphotographed parts on a site that had turned that off.

## What the seam does not cover yet

Honest limits, so nobody discovers them half-way through an adapter:

- **Fitment, donor photographs and order booking are reached outside the seam.** They live in
  `coreyard/yms/` and are written against the source database directly. A new adapter can
  declare `supports_fitment=False` and `supports_order_booking=False` and everything behaves,
  but there is no seam to implement them *through* yet. Providing them means extending the
  protocol, not just writing an adapter.
- **The dispatch still lives in `coreyard/yms/inventory.py`.** `fetch_parts` and its three
  neighbours check for a non-database source and delegate, which is why every existing caller
  kept working when the seam was introduced. It means the call path for a CSV source runs
  through a module whose docstring is about SQL Server. It works; it reads oddly.
- **Only `Part` crosses the boundary.** Anything your system knows that `Part` has no field
  for is not reachable downstream without changing `Part` — which changes the fingerprint,
  and so republishes every product on the next run.
