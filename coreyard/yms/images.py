"""Fetch part photos from the configured SMB share by R#.

Photos live in one flat folder keyed by R# with a two-digit sequence:
``<share>\\<subdir>\\{r_number}_{NN}.jpg`` (e.g. ``10002_01.jpg``,
``10002_02.jpg``). We list/fetch only the files for a given R# using an
``smbclient`` wildcard mask, so we never enumerate the whole (tens-of-thousands-of-files)
directory. Shelling out to ``smbclient`` keeps this dependency-free and matches the tool
already validated against the server.

The trailing underscore in the ``{r_number}_*`` mask anchors the match, so R# ``1000``
does not pick up ``10001_01.jpg`` and R# ``10002`` does not pick up ``100020_01.jpg``.

A second folder, keyed by the source system's own donor-vehicle identifier, holds photographs
of the car a part came off. It is read only for parts the yard never photographed
individually, and every read goes through the same listing, stamping and ordering code as the
inventory folder — a donor photo replaced under its own filename has to be as visible as a
part photo replaced under its own filename.
"""

from __future__ import annotations

import argparse
import atexit
import os
import re
import subprocess
import tempfile
from pathlib import Path

from coreyard.config import SmbConfig, load_smb_config

_IMAGE_RE = re.compile(r".+\.(jpg|jpeg|png)$", re.IGNORECASE)

# A donor shoot documents a whole car — a median of 16 frames and up to 99 — because it was
# taken to record a vehicle, not to sell one bracket off it. Publishing all of them would put
# dozens of near-identical car pictures on a listing, so a part inherits only the opening
# frames, which are the general views. The resolver and the publisher both trim here, because
# a publisher that attached a different set from the one the fingerprint covered would
# republish the part forever.
DONOR_PHOTO_LIMIT = 6


def donor_photo_limit() -> int:
    """How many donor frames one part may inherit. ``STORE_DONOR_PHOTO_LIMIT`` overrides."""
    from coreyard.config import _get
    try:
        return max(1, int(_get("STORE_DONOR_PHOTO_LIMIT", "") or DONOR_PHOTO_LIMIT))
    except (TypeError, ValueError):
        return DONOR_PHOTO_LIMIT

# One `smbclient ls` row: name, DOS attribute letters, size in bytes, modification time.
# The size and time are the point. Listing names alone could not see a photo *replaced*
# under its own filename — the commonest correction a yard makes, since a re-shoot is saved
# over the bad frame — so the catalogue kept showing the picture that was wrong.
_LS_ROW = re.compile(
    r"^\s*(?P<name>\S+)\s+(?P<attrs>[A-Za-z]*)\s+(?P<size>\d+)\s+"
    r"(?P<mtime>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s*$"
)
_NOISE = ("Domain=", "OS=", "smb:", "blocks of size", "Total")


def _stamp(name: str, size: str, mtime: str) -> str:
    """The manifest entry for one photo: what it is called, how big, and when it changed.

    Produced by one function so the whole-share listing and the per-part listing spell an
    unchanged photo identically. If they disagreed, every delta run would re-fingerprint
    the parts it touched and re-upload their media.
    """
    return f"{name}|{size}|{mtime}"


class SmbError(RuntimeError):
    pass


class SmbImageStore:
    """Thin wrapper over the ``smbclient`` CLI for the configured photo share."""

    def __init__(self, cfg: SmbConfig | None = None) -> None:
        self.cfg = cfg or load_smb_config()
        self._auth_path: str | None = None

    # -- auth ---------------------------------------------------------------
    def _auth_file(self) -> str:
        """Write a 0600 smbclient auth file once, so the password never appears in
        the process argument list. Cleaned up at interpreter exit."""
        if self._auth_path and os.path.exists(self._auth_path):
            return self._auth_path
        fd, path = tempfile.mkstemp(prefix="coreyard-smb-", suffix=".auth")
        with os.fdopen(fd, "w") as fh:
            fh.write(f"username = {self.cfg.user}\n")
            fh.write(f"password = {self.cfg.password}\n")
        os.chmod(path, 0o600)
        self._auth_path = path
        atexit.register(lambda p=path: os.path.exists(p) and os.remove(p))
        return path

    def _unc(self) -> str:
        return f"//{self.cfg.host}/{self.cfg.images_share}"

    def _run(self, smb_command: str, cwd: str | None = None) -> str:
        """Run a single ``smbclient -c`` command string against the images share."""
        argv = [
            "smbclient", self._unc(),
            "-A", self._auth_file(),
            "-c", smb_command,
        ]
        try:
            proc = subprocess.run(
                argv, cwd=cwd, capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError as exc:  # smbclient missing
            raise SmbError("smbclient not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise SmbError(f"smbclient timed out: {smb_command!r}") from exc
        # smbclient exits non-zero on real errors; a "NT_STATUS_NO_SUCH_FILE" from an
        # empty wildcard listing is normal and must not raise.
        out = proc.stdout or ""
        err = proc.stderr or ""
        if proc.returncode != 0 and "NT_STATUS_NO_SUCH_FILE" not in (out + err):
            raise SmbError(f"smbclient failed ({proc.returncode}): {err.strip() or out.strip()}")
        return out

    # -- listing / fetching -------------------------------------------------
    def list_inventory_images(self, r_number: str) -> list[str]:
        """Return the image filenames for an R#, ordered by sequence."""
        return [name for name, _stamp_text in self.list_inventory_manifest(r_number)]

    def list_inventory_manifest(self, r_number: str) -> list[tuple[str, str]]:
        """(filename, stamp) for one R#'s photos, ordered by sequence."""
        return self._list_manifest(self.cfg.inventory_subdir, r_number)

    def list_vehicle_images(self, donor_key: str) -> list[str]:
        """Return the donor vehicle's image filenames, ordered by sequence."""
        return [name for name, _stamp_text in self.list_vehicle_manifest(donor_key)]

    def list_vehicle_manifest(self, donor_key: str) -> list[tuple[str, str]]:
        """(filename, stamp) for one donor vehicle's photos, ordered by sequence.

        The donor folder is keyed by the source system's own vehicle identifier, not by
        stock number and not by R#. Those key spaces overlap numerically, which is why a
        donor photo is namespaced before it reaches a fingerprint (see
        ``run_sync._make_resolver``): ``1001_01.jpg`` is a different picture depending on
        which folder it came out of.
        """
        return self._list_manifest(self.cfg.vehicle_subdir, donor_key)

    def _list_manifest(self, subdir: str, key: str) -> list[tuple[str, str]]:
        """(filename, stamp) for one key's photos in one folder, ordered by sequence."""
        out = self._run(f'cd "{subdir}"; ls "{key}_*"')
        return self._parse_ls(out, key)

    def list_all_inventory_images(self) -> dict[str, list[str]]:
        """Map every R# on the share to its ordered photo filenames, in one listing."""
        return {r: [name for name, _ in entries]
                for r, entries in self.list_all_inventory_manifest().items()}

    def list_all_vehicle_manifest(self) -> dict[str, list[tuple[str, str]]]:
        """Map every donor vehicle on the share to its ordered photos, in one listing.

        Same round-trip argument as the inventory folder: a run asks about thousands of
        donors at once, and one directory listing answers all of them.
        """
        return self._list_all_manifest(self.cfg.vehicle_subdir)

    def list_all_inventory_manifest(self) -> dict[str, list[tuple[str, str]]]:
        """Map every R# on the share to its ordered photo filenames, in one listing.

        Per-part listing is the right call when you want one part's photos: it is anchored
        and cheap. It is the wrong call when you want to know *which of 26,000 parts had a
        photo added or removed* — that is 26,000 round trips at ~180 ms each. A single
        directory listing answers the same question in one, and is the only reason this
        module ever enumerates the folder.

        Files that do not match ``{R#}_{NN}.ext`` are ignored, so stray uploads in the
        folder cannot invent an R#.
        """
        return self._list_all_manifest(self.cfg.inventory_subdir)

    def _list_all_manifest(self, subdir: str) -> dict[str, list[tuple[str, str]]]:
        """Map every key in one folder to its ordered (filename, stamp) pairs."""
        out = self._run(f'cd "{subdir}"; ls')
        by_part: dict[str, list[tuple[str, str]]] = {}
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith(_NOISE):
                continue
            row = _LS_ROW.match(line)
            token = row.group("name") if row else line.split()[0]
            m = re.fullmatch(r"(\d+)_(\d+)\.(?:jpg|jpeg|png)", token, re.IGNORECASE)
            if not m:
                continue
            stamp = (_stamp(token, row.group("size"), row.group("mtime")) if row
                     else _stamp(token, "", ""))
            by_part.setdefault(m.group(1), []).append((token, stamp))
        for entries in by_part.values():
            entries.sort(key=lambda e: int(re.search(r"_(\d+)\.", e[0]).group(1)))
        return by_part

    @staticmethod
    def _parse_ls(out: str, r_number: str) -> list[tuple[str, str]]:
        """(filename, stamp) pairs from an `ls` for one R#, ordered by sequence."""
        found: dict[str, str] = {}
        prefix = f"{r_number}_"
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith(_NOISE):
                continue
            row = _LS_ROW.match(line)
            token = row.group("name") if row else line.split()[0]
            if not (token.startswith(prefix) and _IMAGE_RE.match(token)):
                continue
            found[token] = (_stamp(token, row.group("size"), row.group("mtime")) if row
                            else _stamp(token, "", ""))

        def seq(name: str) -> int:
            m = re.search(r"_(\d+)\.", name)
            return int(m.group(1)) if m else 0

        return [(name, found[name]) for name in sorted(found, key=seq)]

    def fetch(self, r_number: str, dest_dir: Path) -> list[Path]:
        """Download all images for an R# into ``dest_dir``.

        Skips files already present with a non-zero size, so re-runs are cheap. All
        ``get`` commands are batched into a single smbclient connection. Returns the
        ordered list of local paths that now exist.
        """
        return self._fetch(self.cfg.inventory_subdir,
                           self.list_inventory_images(r_number), dest_dir)

    def fetch_vehicle(self, donor_key: str, dest_dir: Path) -> list[Path]:
        """Download a donor vehicle's images into ``dest_dir``.

        Used only for a part the yard has not photographed itself; a part with its own
        photos never reaches here, so its published media are untouched by this path.
        """
        return self._fetch(self.cfg.vehicle_subdir,
                           self.list_vehicle_images(donor_key), dest_dir)

    def _fetch(self, subdir: str, names: list[str], dest_dir: Path) -> list[Path]:
        """Download ``names`` from one folder into ``dest_dir``, batched into one session."""
        if not names:
            return []
        dest_dir.mkdir(parents=True, exist_ok=True)
        to_get = [n for n in names if not (dest_dir / n).exists() or (dest_dir / n).stat().st_size == 0]
        if to_get:
            gets = "; ".join(f'get "{n}"' for n in to_get)
            self._run(f'lcd "{dest_dir}"; cd "{subdir}"; {gets}', cwd=str(dest_dir))
        return [dest_dir / n for n in names if (dest_dir / n).exists()]


def add_arguments(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Populate a parser with the per-part photo lookup flags."""
    ap.add_argument("r_numbers", metavar="R#", nargs="+",
                    help="one or more R# to look up on the photo share")
    ap.add_argument("--fetch", action="store_true",
                    help="also download the photos to out/images/<R#>/")
    ap.add_argument("--dest", type=Path, default=Path("out/images"),
                    help="where --fetch writes (default out/images)")
    ap.set_defaults(func=run)
    return ap


def run(args) -> int:
    store = SmbImageStore()
    for r_number in args.r_numbers:
        names = store.list_inventory_images(r_number)
        print(f"R#{r_number}: {len(names)} image(s) -> {names}")
        if args.fetch and names:
            for path in store.fetch(r_number, args.dest / r_number):
                print(f"   fetched {path} ({path.stat().st_size} bytes)")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="coreyard images",
                                 description=__doc__.splitlines()[0])
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
