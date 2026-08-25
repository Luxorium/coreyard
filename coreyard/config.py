"""Configuration loading.

Reads a local ``.env`` file (never committed) plus real environment variables, with a
tiny dependency-free parser so the pipeline runs on a bare Python install. Secrets and
site-specific settings live only in ``.env``/the environment.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from coreyard.profile import CatalogProfile, DEFAULT_PROFILE
from coreyard.profile import load as load_profile
from coreyard.transform.shipping import EMPTY as NO_SHIPPING
from coreyard.transform.shipping import ShippingPolicy
from coreyard.transform.shipping import load as load_shipping_policy
from coreyard.transform.weights import EMPTY as NO_WEIGHTS
from coreyard.transform.weights import WeightRules
from coreyard.transform.weights import load as load_weight_rules

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


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def flag(key: str, default: bool = False) -> bool:
    """Read a boolean setting, rejecting anything that is neither yes nor no.

    ``STORE_REQUIRE_IMAGES=maybe`` silently reading as false is how a storefront policy
    stops applying without anybody noticing, so an unrecognised value is an error.
    """
    raw = (_get(key, "") or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise RuntimeError(
        f"{key} must be true or false (got {raw!r})."
    )


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
    """The yard's own identity and storefront policy, as they reach a shopper.

    Everything customer-facing this tool generates is built from this object, so no business
    name, city, claim, or weight estimate is hardcoded anywhere in the render path. The
    identity fields default to empty and each clause is omitted when its value is blank,
    which keeps the generated copy valid for an installation that has only filled in some of
    them; ``catalog`` and ``weights`` default to CoreYard's neutral policy and to no weight
    table at all.

    It is carried through rendering rather than read from the environment there on purpose:
    the render path stays a pure function of (part, images, store), which is what makes the
    fingerprint reproducible and the unit tests independent of any installation's ``.env``.
    """

    vendor: str = ""        # business name written to Shopify's Vendor field
    city: str = ""          # location shown in descriptions, e.g. "Springfield, IL"
    warranty: str = ""      # warranty phrase, e.g. "90-day warranty"

    # Prefix for the Shopify product handle (``<prefix>-<R#>``). This is the storefront's
    # primary key: every product URL and every idempotent upsert depends on it, so it must
    # be chosen once, before the first publish, and never changed afterwards. Changing it
    # on a live store makes every product look new and duplicates the whole catalog.
    handle_prefix: str = "coreyard"

    # What this site is willing to claim about its parts (STORE_PROFILE_FILE).
    catalog: CatalogProfile = field(default_factory=lambda: DEFAULT_PROFILE)

    # Estimated packed shipping weights by part type (STORE_WEIGHT_RULES_FILE).
    weights: WeightRules = field(default_factory=lambda: NO_WEIGHTS)

    # How each part ships, and the tag that tells the storefront so
    # (STORE_SHIPPING_POLICY_FILE). Empty means CoreYard classifies nothing.
    shipping: ShippingPolicy = field(default_factory=lambda: NO_SHIPPING)

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
    """The yard's storefront identity and policy.

    ``SHOPIFY_VENDOR`` is required because every product carries it; the descriptive extras
    are optional and simply drop out of the copy. The two file-backed settings are optional
    as well, but a path that is set and does not resolve is an error rather than a fallback:
    publishing CoreYard's neutral wording under a site that wrote its own, or a catalogue of
    weightless parts under a site that supplied a table, is invisible in the result.
    """
    load_env()
    return StoreProfile(
        vendor=_get("SHOPIFY_VENDOR", required=True) or "",
        city=_get("STORE_CITY", "") or "",
        warranty=_get("STORE_WARRANTY", "") or "",
        handle_prefix=_get("SHOPIFY_HANDLE_PREFIX", "coreyard") or "coreyard",
        catalog=load_profile(_get("STORE_PROFILE_FILE", "") or None),
        weights=load_weight_rules(_get("STORE_WEIGHT_RULES_FILE", "") or None),
        shipping=load_shipping_policy(_get("STORE_SHIPPING_POLICY_FILE", "") or None),
    )


def require_images(default: bool = False) -> bool:
    """Whether this storefront only lists parts that have at least one photo.

    A policy, not a data question: a site that promises "you buy the part in the picture"
    must not list an unphotographed part, and a site that sells sight-unseen must not have
    half its yard hidden. It is off by default so an upgrade never silently changes which
    parts an existing installation publishes.

    Read through here by every path that decides what is listable — sync, reconciliation,
    bulk publishing and audit — so the four cannot drift apart.
    """
    return flag("STORE_REQUIRE_IMAGES", default)


def publication_names() -> list[str]:
    """Sales channels a published product should be visible on.

    Status and channel publication are separate things in Shopify, and only the second makes
    a URL resolve: an ACTIVE, priced, photographed product that was never published to a
    channel returns 404 to every shopper and to Google. Empty (the default) means CoreYard
    leaves publication alone, which is the right behaviour for a store whose channels are
    managed elsewhere.
    """
    raw = _get("STORE_PUBLICATIONS", "") or ""
    return [name.strip() for name in raw.split(",") if name.strip()]


def preserved_tag_prefixes() -> tuple[str, ...]:
    """Extra tag prefixes owned by another system, from the environment.

    Merged with the profile's own list. See :mod:`coreyard.transform.tags` for why a
    namespaced tag is preserved even without being named here.
    """
    raw = _get("STORE_PRESERVED_TAG_PREFIXES", "") or ""
    return tuple(p.strip() for p in raw.split(",") if p.strip())
