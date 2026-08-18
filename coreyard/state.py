"""Incremental-sync state: what did we last publish, and what changed since?

A tiny SQLite table maps ``R# -> content fingerprint``. Each run fingerprints
the parts it would publish and diffs against the stored snapshot to classify every part
as added / changed / unchanged, and to detect removals (R#s we published
before that are no longer priced-and-available — i.e. sold). The fingerprint is taken
over the exact primary Shopify row plus the full ordered image list, so it moves only
when something a shopper would see changes.
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
from coreyard.transform.shopify_product import primary_row

# Named here rather than in the sync orchestrator because the order webhook also needs to
# reach the retirement memory, and importing the whole sync path to learn a filename would
# drag the extract layer into a service that must start without a database.
DEFAULT_STATE_DB = REPO_ROOT / "coreyard_sync_state.sqlite3"


def product_fingerprint(part: Part, image_urls: list[str], store: StoreProfile) -> str:
    """Stable SHA-256 over the published content (primary row + image list)."""
    first = image_urls[0] if image_urls else None
    payload = {
        "row": primary_row(part, first, store),
        "images": image_urls,
        # The fitment key drives the whole fitment section but never reaches primary_row,
        # so re-keying a part to a different interchange would otherwise go undetected.
        "interchange_code": part.interchange_code,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def image_fingerprint(image_urls: list[str]) -> str:
    """Fingerprint of just the photo set.

    Tracked next to the content fingerprint so a run can tell *why* a part changed. A text
    edit is a cheap re-upsert; a photo change means the media on Shopify has to be torn down
    and re-uploaded, which is expensive and must not be done to every part that merely had
    its price moved.
    """
    blob = json.dumps(list(image_urls), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass
class DiffResult:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # R# was published, now gone/sold
    image_changed: list[str] = field(default_factory=list)  # subset of added+changed

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
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(parts)")}
        if "stock" in columns and "r_number" not in columns:
            # Older snapshots already stored the R# in a misleadingly named column.
            self.conn.execute("ALTER TABLE parts RENAME COLUMN stock TO r_number")
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

    def diff(self, current: dict[str, str], images: dict[str, str] | None = None) -> DiffResult:
        """Classify current fingerprints against the stored snapshot.

        ``images`` is the parallel photo-set fingerprint map. When given, parts whose photos
        moved are reported in ``image_changed`` so the caller can re-upload only those.
        """
        previous = self.load()
        result = DiffResult()
        for r_number, fp in current.items():
            if r_number not in previous:
                result.added.append(r_number)
            elif previous[r_number] != fp:
                result.changed.append(r_number)
            else:
                result.unchanged.append(r_number)
        for r_number in previous:
            if r_number not in current:
                result.removed.append(r_number)
        if images:
            previous_images = self.load_images()
            result.image_changed = [
                r_number
                for r_number, fp in images.items()
                # An R# with no recorded photo fingerprint predates this column; treating it
                # as changed would re-upload the entire catalogue on the upgrade run.
                if r_number in previous_images and previous_images[r_number] != fp
            ]
        for lst in (result.added, result.changed, result.unchanged,
                    result.removed, result.image_changed):
            lst.sort()
        return result

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

    def commit(self, current: dict[str, str], images: dict[str, str] | None = None) -> None:
        """Replace the snapshot with the current set (removes vanished R#s)."""
        now = datetime.now(timezone.utc).isoformat()
        images = images or {}
        with self.conn:
            self.conn.execute("DELETE FROM parts")
            self.conn.executemany(
                "INSERT INTO parts(r_number, fingerprint, image_fingerprint, last_seen)"
                " VALUES (?,?,?,?)",
                [(r_number, fp, images.get(r_number, ""), now)
                 for r_number, fp in current.items()],
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
    """Both fingerprint maps in one pass, so the resolver is called once per part."""
    content: dict[str, str] = {}
    images: dict[str, str] = {}
    for part in parts:
        if not part.is_listable():
            continue
        urls = resolver(part)
        content[part.uid()] = product_fingerprint(part, urls, store)
        images[part.uid()] = image_fingerprint(urls)
    return content, images
