"""Acquire listing-portal session cookies without storing a password.

An explicit cookie header may be supplied in ``EBAY_PORTAL_COOKIES``.  Otherwise the
most recently used Firefox profiles are inspected read-only: persistent cookies come from
a temporary copy of ``cookies.sqlite`` and session cookies from Firefox's compressed
session-restore snapshot.  A successful live jar is cached owner-only under ``out/`` so a
valid server session survives closing the browser tab.
"""

from __future__ import annotations

import configparser
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from coreyard.config import REPO_ROOT, _get
from coreyard.ebay.portal import PortalMap


class AuthError(RuntimeError):
    """No complete authenticated cookie jar could be found."""


def _firefox_root() -> Path:
    configured = _get("EBAY_PORTAL_FIREFOX_DIR", "") or ""
    return Path(configured) if configured else Path.home() / ".mozilla" / "firefox"


def _firefox_profiles(root: Path | None = None) -> list[Path]:
    root = root or _firefox_root()
    if not root.is_dir():
        return []
    found: list[Path] = []
    ini = root / "profiles.ini"
    if ini.is_file():
        parser = configparser.ConfigParser()
        parser.read(ini)
        for section in parser.sections():
            value = parser[section].get("Path")
            if not value:
                continue
            profile = Path(value)
            profile = profile if profile.is_absolute() else root / profile
            if ((profile / "cookies.sqlite").is_file()
                    or (profile / "sessionstore-backups").is_dir()):
                found.append(profile)
    for database in sorted(root.glob("*/cookies.sqlite")):
        if database.parent not in found:
            found.append(database.parent)

    def touched(profile: Path) -> float:
        candidates = (
            profile / "cookies.sqlite",
            profile / "sessionstore-backups" / "recovery.jsonlz4",
        )
        return max((item.stat().st_mtime for item in candidates if item.is_file()),
                   default=0.0)

    return sorted(found, key=touched, reverse=True)


def _sqlite_cookies(profile: Path, host: str, errors: list[str]) -> dict[str, str]:
    source = profile / "cookies.sqlite"
    if not source.is_file():
        return {}
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / "cookies.sqlite"
        try:
            shutil.copy2(source, copy)
            connection = sqlite3.connect(f"file:{copy}?immutable=1", uri=True)
            rows = connection.execute(
                "SELECT name, value FROM moz_cookies WHERE host LIKE ?", (f"%{host}",)
            ).fetchall()
            connection.close()
        except Exception as exc:  # a locked/corrupt/foreign Firefox database
            errors.append(f"{profile.name}/cookies.sqlite: {exc}")
            return {}
    return {name: value for name, value in rows}


def _lz4_block_decompress(source: bytes) -> bytes:
    """Decompress the raw LZ4 block stored after Firefox's session-file header."""
    out = bytearray()
    index = 0
    while index < len(source):
        token = source[index]
        index += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                value = source[index]
                index += 1
                literal_length += value
                if value != 255:
                    break
        out += source[index:index + literal_length]
        index += literal_length
        if index >= len(source):
            break
        offset = source[index] | (source[index + 1] << 8)
        index += 2
        if offset <= 0 or offset > len(out):
            raise ValueError("invalid LZ4 match offset")
        match_length = token & 0x0F
        if match_length == 15:
            while True:
                value = source[index]
                index += 1
                match_length += value
                if value != 255:
                    break
        start = len(out) - offset
        for position in range(match_length + 4):
            out.append(out[start + position])
    return bytes(out)


def _host_matches(cookie_host: str, host: str) -> bool:
    return cookie_host == host or cookie_host.endswith("." + host) or (
        cookie_host.startswith(".") and host.endswith(cookie_host)
    )


def _session_cookies(profile: Path, host: str, names: tuple[str, ...],
                     errors: list[str]) -> dict[str, str]:
    jar: dict[str, str] = {}
    directory = profile / "sessionstore-backups"
    for name in ("recovery.jsonlz4", "recovery.baklz4", "previous.jsonlz4"):
        source = directory / name
        if not source.is_file():
            continue
        try:
            raw = source.read_bytes()
            if raw[:8] != b"mozLz40\0":
                continue
            state = json.loads(_lz4_block_decompress(raw[12:]))
        except Exception as exc:
            errors.append(f"{profile.name}/{name}: {exc}")
            continue
        for cookie in state.get("cookies", []):
            if (_host_matches(str(cookie.get("host", "")), host)
                    and cookie.get("name")):
                jar.setdefault(str(cookie["name"]), str(cookie.get("value", "")))
        if all(name in jar for name in names):
            break
    return jar


def cookies_from_firefox(portal: PortalMap, root: Path | None = None) -> dict[str, str]:
    profiles = _firefox_profiles(root)
    if not profiles:
        raise AuthError(
            "no Firefox profile with a cookie store was found; set "
            "EBAY_PORTAL_FIREFOX_DIR or EBAY_PORTAL_COOKIES"
        )
    errors: list[str] = []
    for profile in profiles:
        jar = _sqlite_cookies(profile, portal.host, errors)
        jar.update(_session_cookies(profile, portal.host, portal.cookie_names, errors))
        if all(name in jar for name in portal.cookie_names):
            return jar
        if jar:
            errors.append(
                f"{profile.name}: found {sorted(jar)} but need "
                f"{list(portal.cookie_names)}"
            )
    detail = "\n  ".join(errors or ["no cookies for the configured host"])
    raise AuthError(
        f"no Firefox profile held a complete portal session\n  {detail}\n"
        f"Log in at {portal.base_url} in Firefox, then retry."
    )


def parse_cookie_header(raw: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in raw.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            jar[name.strip()] = value.strip()
    return jar


def cookie_file() -> Path:
    configured = _get("EBAY_PORTAL_COOKIE_FILE", "") or ""
    return Path(configured) if configured else REPO_ROOT / "out" / "ebay-portal.cookies"


def cached_cookies(path: Path | None = None) -> dict[str, str] | None:
    path = path or cookie_file()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return parse_cookie_header(raw) or None


def save_cookies(jar: dict[str, str], path: Path | None = None) -> Path:
    """Cache a jar mode 0600. Cookie values are never printed or logged."""
    path = path or cookie_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("; ".join(f"{key}={value}" for key, value in jar.items()),
                    encoding="utf-8")
    path.chmod(0o600)
    return path


def get_cookies(portal: PortalMap) -> dict[str, str]:
    """Prefer an explicit jar, then the live browser, then the owner-only cache."""
    explicit = parse_cookie_header(_get("EBAY_PORTAL_COOKIES", "") or "")
    if explicit:
        return explicit
    try:
        jar = cookies_from_firefox(portal)
    except AuthError:
        cached = cached_cookies()
        if cached and all(name in cached for name in portal.cookie_names):
            return cached
        raise
    try:
        save_cookies(jar)
    except OSError:
        pass
    return jar

