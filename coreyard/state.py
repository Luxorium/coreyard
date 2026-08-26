"""Incremental-sync state: what did we last publish, and what changed since?

A tiny SQLite table maps ``R# -> content fingerprint``. Each run fingerprints
the parts it would publish and diffs against the stored snapshot to classify every part
as added / changed / unchanged, and to detect removals (R#s we published
before that are no longer listable — i.e. sold). The fingerprint is taken over the canonical
rendered product — the same object both sinks publish, photos and alt text included — so it
moves exactly when something a shopper would see changes, and never fails to.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from coreyard.config import REPO_ROOT, StoreProfile
from coreyard.models import Part
from coreyard.transform.render import render

# Named here rather than in the sync orchestrator because the order webhook also needs to
# reach the retirement memory, and importing the whole sync path to learn a filename would
# drag the extract layer into a service that must start without a database.
DEFAULT_STATE_DB = REPO_ROOT / "coreyard_sync_state.sqlite3"


def product_fingerprint(part: Part, image_urls: list[str], store: StoreProfile) -> str:
    """Stable SHA-256 over the canonical rendered product.

    It hashes the *same object both sinks publish*, which is the whole point. Fingerprinting
    one renderer while publishing through another meant a change to the published title,
    tags, description or SEO metadata left the stored hash untouched, so the next run called
    every stale product "unchanged" and the storefront kept output no current version of the
    code would produce. See :mod:`coreyard.transform.render`.
    """
    return render(part, image_urls, store).fingerprint()


def image_fingerprint(image_urls: list[str]) -> str:
    """Fingerprint of just the photo set.

    Tracked next to the content fingerprint so a run can tell *why* a part changed. A text
    edit is a cheap re-upsert; a photo change means the media on Shopify has to be torn down
    and re-uploaded, which is expensive and must not be done to every part that merely had
    its price moved.
    """
    blob = json.dumps(list(image_urls), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# The scope columns, and the render projection each one stores. Adding a scope here and in
# ``render.SCOPE_FIELDS`` is all it takes; the migration below adds the column.
SCOPES = ("inventory", "catalog")

# Bumped when the photo manifest changes shape. Version 1 hashed filenames alone, which
# could not see a photo replaced under its own name; version 2 hashes name, size and
# modification time.
#
# The upgrade cannot simply re-hash: every stored value would differ, every photographed
# part would read as "photos changed", and the next run would tear down and re-upload the
# media of the entire catalogue. So a run whose stored version is behind records the new
# manifest and reports *no* photo changes — it rebaselines. Only a full commit may declare
# the new version, because only a full commit rewrites every row; a scoped or delta run
# that claimed it would leave most rows at version 1 and the run after that would compare
# the two shapes against each other.
IMAGE_MANIFEST_VERSION = "2"
_MANIFEST_KEY = "image_manifest_version"


@dataclass
class Fingerprints:
    """Every fingerprint one run computed, keyed by R#.

    ``content`` is the canonical one the diff has always used and is unchanged. The rest are
    projections of the same rendered product (see ``RenderedProduct.scope_fingerprint``),
    carried alongside so a run can say *why* a part moved rather than only that it did —
    which is what ``coreyard sync inventory`` and ``coreyard sync photos`` select on.
    """

    content: dict[str, str] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=dict)
    scopes: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class DiffResult:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # R# was published, now gone/sold
    image_changed: list[str] = field(default_factory=list)  # subset of added+changed
    # Why the changed ones changed. Subsets of added+changed, and deliberately allowed to
    # overlap: a part can have been repriced *and* retitled in the same tick.
    scope_changed: dict[str, list[str]] = field(default_factory=dict)

    def changed_in(self, scope: str) -> list[str]:
        """Parts a scoped run should act on: this scope moved, or the part is brand new.

        A new part has no previous fingerprint in any scope, so every scope claims it —
        correct, because creating the product is what makes it right in all of them.
        """
        if scope == "photos":
            return sorted(set(self.image_changed) | set(self.added))
        return sorted(set(self.scope_changed.get(scope, ())) | set(self.added))

    def summary(self) -> str:
        return (
            f"added={len(self.added)} changed={len(self.changed)} "
            f"unchanged={len(self.unchanged)} removed={len(self.removed)} "
            f"photos_changed={len(self.image_changed)}"
        )


class SyncState:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # The order webhook records retirements too, and it runs as its own long-lived
        # service, so two processes can reach for this file at once. Wait for the lock
        # instead of failing the write that remembers how to bring a part back.
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS parts ("
            " r_number TEXT PRIMARY KEY,"
            " fingerprint TEXT NOT NULL,"
            " last_seen TEXT NOT NULL)"
        )
        # Retirement drops a part from `parts` entirely, so the status it had before being
        # archived has to be kept somewhere that survives that. Without it a part that
        # returns to the yard (a voided work order) is republished onto the archived
        # product, which re-sends ARCHIVED and leaves the part unbuyable forever.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS retired ("
            " r_number TEXT PRIMARY KEY,"
            " status TEXT NOT NULL,"
            " retired_at TEXT NOT NULL)"
        )
        # Where a delta run left off. Kept beside the fingerprints on purpose: a cursor that
        # outlived the snapshot it was taken against would make the next delta run skip every
        # change between them, and the two are only meaningful together.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS cursors ("
            " name TEXT PRIMARY KEY,"
            " value TEXT NOT NULL,"
            " updated_at TEXT NOT NULL)"
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(parts)")}
        if "stock" in columns and "r_number" not in columns:
            # Older snapshots already stored the R# in a misleadingly named column.
            self.conn.execute("ALTER TABLE parts RENAME COLUMN stock TO r_number")
        for scope in SCOPES:
            # Same contract as image_fingerprint below: an existing row gets '', which reads
            # as "this scope is unknown for this part", so the first run after upgrading
            # reports no scope changes rather than claiming the whole catalogue moved. The
            # canonical `fingerprint` column is never touched by this migration — rewriting
            # it would republish every product on the store.
            if f"{scope}_fingerprint" not in columns:
                self.conn.execute(
                    f"ALTER TABLE parts ADD COLUMN {scope}_fingerprint TEXT NOT NULL"
                    " DEFAULT ''"
                )
        if "image_fingerprint" not in columns:
            # Added later. Existing rows get '' — which reads as "photos unknown", so the
            # first run after upgrading reports no photo changes rather than claiming every
            # part needs its media re-uploaded.
            self.conn.execute(
                "ALTER TABLE parts ADD COLUMN image_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SyncState":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def load(self) -> dict[str, str]:
        return {
            r_number: fp
            for r_number, fp in self.conn.execute("SELECT r_number, fingerprint FROM parts")
        }

    def load_images(self) -> dict[str, str]:
        return {
            r_number: fp
            for r_number, fp in self.conn.execute(
                "SELECT r_number, image_fingerprint FROM parts"
            )
            if fp
        }

    def load_scope(self, scope: str) -> dict[str, str]:
        """One scope's stored fingerprints, skipping the rows that predate the column."""
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}")
        return {
            r_number: fp
            for r_number, fp in self.conn.execute(
                f"SELECT r_number, {scope}_fingerprint FROM parts"
            )
            if fp
        }

    def diff(
        self,
        current,
        images: dict[str, str] | None = None,
        detect_removals: bool = True,
    ) -> DiffResult:
        """Classify current fingerprints against the stored snapshot.

        ``images`` is the parallel photo-set fingerprint map. When given, parts whose photos
        moved are reported in ``image_changed`` so the caller can re-upload only those.

        ``detect_removals`` must be False when ``current`` is a *subset* of the yard, as it
        is for a delta run. Removal is inferred from absence, and every part the subset did
        not look at is absent — so leaving this on would report the entire catalogue as sold.

        ``current`` may be a :class:`Fingerprints` bundle instead of the bare content map,
        in which case the per-scope classification is filled in too and ``images`` is taken
        from the bundle.
        """
        bundle = current if isinstance(current, Fingerprints) else None
        if bundle is not None:
            current, images = bundle.content, bundle.images
        previous = self.load()
        result = DiffResult()
        for r_number, fp in current.items():
            if r_number not in previous:
                result.added.append(r_number)
            elif previous[r_number] != fp:
                result.changed.append(r_number)
            else:
                result.unchanged.append(r_number)
        if detect_removals:
            for r_number in previous:
                if r_number not in current:
                    result.removed.append(r_number)
        if images and self.get_cursor(_MANIFEST_KEY) != IMAGE_MANIFEST_VERSION:
            # Rebaselining. The stored fingerprints were taken with an older manifest, so
            # comparing them against these would flag every photographed part.
            result.image_changed = []
        elif images:
            previous_images = self.load_images()
            result.image_changed = [
                r_number
                for r_number, fp in images.items()
                # An R# with no recorded photo fingerprint predates this column; treating it
                # as changed would re-upload the entire catalogue on the upgrade run.
                if r_number in previous_images and previous_images[r_number] != fp
            ]
        if bundle is not None:
            for scope, fingerprints in bundle.scopes.items():
                stored = self.load_scope(scope)
                # An R# with no stored value for this scope predates the column. Treating it
                # as changed would make the upgrade run publish the whole catalogue once per
                # scope, which is exactly what the image column's default avoids.
                result.scope_changed[scope] = sorted(
                    r for r, fp in fingerprints.items()
                    if r in stored and stored[r] != fp
                )
        for lst in (result.added, result.changed, result.unchanged,
                    result.removed, result.image_changed):
            lst.sort()
        return result

    # -- delta cursors -------------------------------------------------------
    def get_cursor(self, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM cursors WHERE name = ?", (str(name),)
        ).fetchone()
        return row[0] if row else None

    def set_cursor(self, name: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO cursors(name, value, updated_at) VALUES (?,?,?)",
                (str(name), str(value), datetime.now(timezone.utc).isoformat()),
            )

    # -- retirement memory ---------------------------------------------------
    def record_retired(self, r_number: str, status: str) -> None:
        """Remember the status a product held just before it was archived.

        Called only on a real transition, never when a product is already archived, so a
        repeated retirement of the same part cannot overwrite the answer with ARCHIVED.
        """
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO retired(r_number, status, retired_at)"
                " VALUES (?,?,?)",
                (str(r_number), str(status), datetime.now(timezone.utc).isoformat()),
            )

    def retired_statuses(self) -> dict[str, str]:
        """Every remembered pre-retirement status, keyed by R#."""
        return {
            r_number: status
            for r_number, status in self.conn.execute(
                "SELECT r_number, status FROM retired"
            )
        }

    def clear_retired(self, r_numbers: Iterable[str]) -> None:
        """Forget parts that are back on sale; the memory is only for archived ones."""
        keys = [(str(r),) for r in r_numbers]
        if not keys:
            return
        with self.conn:
            self.conn.executemany("DELETE FROM retired WHERE r_number = ?", keys)

    @staticmethod
    def _rows(current, images):
        """(columns, rows) for a snapshot write, from either a bundle or a bare map."""
        bundle = current if isinstance(current, Fingerprints) else None
        if bundle is not None:
            current, images = bundle.content, bundle.images
        images = images or {}
        now = datetime.now(timezone.utc).isoformat()
        columns = ["r_number", "fingerprint", "image_fingerprint"]
        columns += [f"{scope}_fingerprint" for scope in SCOPES]
        columns.append("last_seen")
        rows = []
        for r_number, fp in current.items():
            row = [r_number, fp, images.get(r_number, "")]
            for scope in SCOPES:
                row.append(
                    bundle.scopes.get(scope, {}).get(r_number, "") if bundle else "")
            row.append(now)
            rows.append(tuple(row))
        return columns, rows

    def update(self, current, images: dict[str, str] | None = None) -> None:
        """Merge a *subset* of fingerprints into the snapshot, leaving the rest alone.

        The delta path's counterpart to :meth:`commit`. Calling ``commit`` with a delta's
        handful of parts would delete every part it did not look at, and the next full run
        would then republish the whole catalogue as new.
        """
        columns, rows = self._rows(current, images)
        if not rows:
            return
        placeholders = ",".join("?" * len(columns))
        with self.conn:
            self.conn.executemany(
                f"INSERT OR REPLACE INTO parts({','.join(columns)})"
                f" VALUES ({placeholders})", rows,
            )

    def forget(self, r_numbers: Iterable[str]) -> None:
        """Drop specific parts from the snapshot, as retirement does for a full run."""
        keys = [(str(r),) for r in r_numbers]
        if not keys:
            return
        with self.conn:
            self.conn.executemany("DELETE FROM parts WHERE r_number = ?", keys)

    def commit(self, current, images: dict[str, str] | None = None) -> None:
        """Replace the snapshot with the current set (removes vanished R#s)."""
        columns, rows = self._rows(current, images)
        placeholders = ",".join("?" * len(columns))
        with self.conn:
            self.conn.execute("DELETE FROM parts")
            self.conn.executemany(
                f"INSERT INTO parts({','.join(columns)}) VALUES ({placeholders})", rows,
            )
        # Every row was just rewritten with the current manifest shape, so — and only
        # here — the stored version is true of the whole snapshot. A run that carried no
        # photo fingerprints at all (`--no-image-scan`) has not established anything and
        # must leave the version where it was.
        carried_images = (current.images if isinstance(current, Fingerprints)
                          else (images or {}))
        if any(carried_images.values()):
            self.set_cursor(_MANIFEST_KEY, IMAGE_MANIFEST_VERSION)


def fingerprints_all(
    parts: Iterable[Part],
    resolver,
    store: StoreProfile,
    stamps=None,
) -> Fingerprints:
    """Every fingerprint for a run, rendering each part exactly once.

    One render per part is the point: the canonical hash and every scope projection come off
    the same :class:`RenderedProduct`, so they cannot describe different products.
    """
    from coreyard.transform.render import SCOPE_FIELDS, render

    result = Fingerprints(scopes={scope: {} for scope in SCOPES})
    for part in parts:
        if not part.is_listable():
            continue
        urls = resolver(part)
        rendered = render(part, urls, store)
        key = part.uid()
        result.content[key] = rendered.fingerprint()
        # The product hashes the photo *references* it publishes; the image fingerprint
        # hashes the share's manifest, which also carries size and modification time. They
        # answer different questions: "does the listing name different files" versus "did
        # any of those files change on disk".
        result.images[key] = image_fingerprint(stamps(part) if stamps else urls)
        for scope in SCOPES:
            if scope in SCOPE_FIELDS:
                result.scopes[scope][key] = rendered.scope_fingerprint(scope)
    return result


def subset(fingerprints: Fingerprints, keys: Iterable[str]) -> Fingerprints:
    """The bundle narrowed to ``keys`` — what a run records after publishing only some.

    A part that failed to publish must keep its *old* fingerprints, so the next run tries it
    again. Filtering here rather than at each call site keeps the four maps in step.
    """
    wanted = set(keys)
    return Fingerprints(
        content={k: v for k, v in fingerprints.content.items() if k in wanted},
        images={k: v for k, v in fingerprints.images.items() if k in wanted},
        scopes={scope: {k: v for k, v in values.items() if k in wanted}
                for scope, values in fingerprints.scopes.items()},
    )


def fingerprints_for(
    parts: Iterable[Part],
    resolver,
    store: StoreProfile,
) -> dict[str, str]:
    """Build the R#-to-fingerprint map for a run (listable parts only)."""
    return fingerprints_with_images(parts, resolver, store)[0]


def fingerprints_with_images(
    parts: Iterable[Part],
    resolver,
    store: StoreProfile,
) -> tuple[dict[str, str], dict[str, str]]:
    """The content and photo maps alone, for callers that want no scope classification.

    A view onto :func:`fingerprints_all` rather than a second pass over the parts: two
    implementations of "fingerprint this run" is exactly the shape of the bug that let a
    stale product read as unchanged forever.
    """
    both = fingerprints_all(parts, resolver, store)
    return both.content, both.images
