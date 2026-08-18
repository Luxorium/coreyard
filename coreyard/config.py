"""Configuration loading.

Reads a local ``.env`` file (never committed) plus real environment variables, with a
tiny dependency-free parser so the pipeline runs on a bare Python install. Secrets and
site-specific settings live only in ``.env``/the environment.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"


def load_env(path: Path = ENV_PATH) -> None:
    """Load KEY=VALUE lines from ``.env`` into ``os.environ`` (without overriding
    variables already set in the real environment). Supports ``#`` comments, blank
    lines, optional surrounding quotes, and an optional leading ``export ``."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Keys that have been renamed. An installation that predates the rename keeps working — a
# config key is part of the interface, and quietly dropping one turns an upgrade into an
# outage at the next timer tick, with only "missing required config" to explain it.
_LEGACY_KEYS = {
    "YMS_DB_HOST": "PL_DB_HOST",
    "YMS_DB_PORT": "PL_DB_PORT",
    "YMS_DB_NAME": "PL_DB_NAME",
    "YMS_DB_USER": "PL_DB_USER",
    "YMS_DB_PASSWORD": "PL_DB_PASSWORD",
}
_warned_legacy: set[str] = set()


def _get(key: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(key)
    if not val and key in _LEGACY_KEYS:
        legacy = _LEGACY_KEYS[key]
        val = os.environ.get(legacy)
        if val and legacy not in _warned_legacy:
            _warned_legacy.add(legacy)
            print(f"note: '{legacy}' is the old name for '{key}'; rename it in {ENV_PATH}.",
                  file=sys.stderr)
    if val is None:
        val = default
    if required and not val:
        raise RuntimeError(
            f"Missing required config '{key}'. Set it in {ENV_PATH} (see .env.example)."
        )
    return val


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    database: str
    user: str
    password: str


@dataclass(frozen=True)
class SmbConfig:
    host: str
    server_name: str             # the server's NetBIOS name (needed for SMB session setup)
    user: str
    password: str
    images_share: str            # SMB share holding part photos
    inventory_subdir: str        # subdirectory for part photos
    vehicle_subdir: str          # subdirectory for vehicle photos


@dataclass(frozen=True)
class StoreProfile:
    """The yard's own identity, as it appears to shoppers.

    Every customer-facing string this tool generates is built from these three values, so
    no business name, city, or policy is hardcoded anywhere in the render path. All three
    default to empty and each clause is omitted when its value is blank, which keeps the
    generated copy valid for an installation that has only filled in some of them.
    """

    vendor: str = ""        # business name written to Shopify's Vendor field
    city: str = ""          # location shown in descriptions, e.g. "Springfield, IL"
    warranty: str = ""      # warranty phrase, e.g. "90-day warranty"

    # Prefix for the Shopify product handle (``<prefix>-<R#>``). This is the storefront's
    # primary key: every product URL and every idempotent upsert depends on it, so it must
    # be chosen once, before the first publish, and never changed afterwards. Changing it
    # on a live store makes every product look new and duplicates the whole catalog.
    handle_prefix: str = "coreyard"

    def origin(self) -> str:
        """"Vendor, City" with missing pieces dropped."""
        return ", ".join(part for part in (self.vendor, self.city) if part)


@dataclass(frozen=True)
class Settings:
    db: DbConfig
    smb: SmbConfig
    store: StoreProfile
    out_dir: Path


def _smb_from_env() -> SmbConfig:
    """Build SmbConfig from the environment.

    Host, server name, credentials, and share name are deliberately **required** with no
    fallbacks — no site's network details or vendor paths are baked into this repo. Only the
    generic subdirectory names keep defaults.
    """
    return SmbConfig(
        host=_get("SMB_HOST", required=True),
        server_name=_get("SMB_SERVER_NAME", required=True),
        user=_get("SMB_USER", required=True),
        password=_get("SMB_PASSWORD", required=True),
        images_share=_get("SMB_IMAGES_SHARE", required=True),
        inventory_subdir=_get("SMB_INVENTORY_SUBDIR", "Inventory"),
        vehicle_subdir=_get("SMB_VEHICLE_SUBDIR", "Vehicle"),
    )


def load_settings() -> Settings:
    """Assemble Settings from the environment. Everything site-specific comes from ``.env``;
    a missing key raises rather than silently falling back to someone else's server."""
    load_env()
    db = DbConfig(
        host=_get("YMS_DB_HOST", required=True),
        port=int(_get("YMS_DB_PORT", "1433")),
        database=_get("YMS_DB_NAME", required=True),
        user=_get("YMS_DB_USER", ""),
        password=_get("YMS_DB_PASSWORD", ""),  # unused: DB auth is Windows/NTLM via SMB creds
    )
    return Settings(
        db=db,
        smb=_smb_from_env(),
        store=load_store(),
        out_dir=REPO_ROOT / "out",
    )


def load_smb_config() -> SmbConfig:
    """SMB-only settings (image work needs no database config present)."""
    load_env()
    return _smb_from_env()


def load_store() -> StoreProfile:
    """The yard's storefront identity. ``SHOPIFY_VENDOR`` is required because every product
    carries it; the descriptive extras are optional and simply drop out of the copy."""
    load_env()
    return StoreProfile(
        vendor=_get("SHOPIFY_VENDOR", required=True) or "",
        city=_get("STORE_CITY", "") or "",
        warranty=_get("STORE_WARRANTY", "") or "",
        handle_prefix=_get("SHOPIFY_HANDLE_PREFIX", "coreyard") or "coreyard",
    )
