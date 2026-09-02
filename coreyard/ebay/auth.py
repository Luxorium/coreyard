"""Acquire listing-portal session cookies.

An explicit cookie header may be supplied in ``EBAY_PORTAL_COOKIES``.  Otherwise the
most recently used Firefox profiles are inspected read-only: persistent cookies come from
a temporary copy of ``cookies.sqlite`` and session cookies from Firefox's compressed
session-restore snapshot.  A successful live jar is cached owner-only under ``out/`` so a
valid server session survives closing the browser tab.

Borrowing the browser's session was originally the whole design, on the grounds that it
needs no password on disk.  It does not survive an unattended run that outlives the
session, though, which is what a multi-hour research pass is, so ``sign_in`` will
establish one from ``EBAY_PORTAL_USER``/``EBAY_PORTAL_PASSWORD`` when they are set.  Those
are read through ``config._get`` like every other setting and belong in the gitignored
``.env``; the form's own field names are portal vocabulary and live in ``portal.json``.
A password is never printed, logged, or included in an error message.
"""

from __future__ import annotations

import configparser
import json
import shutil
import sqlite3
import tempfile
from html.parser import HTMLParser
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


class _Hidden(HTMLParser):
    """Pull one hidden input's value out of a form, without a parser dependency."""

    def __init__(self, field: str) -> None:
        super().__init__()
        self.field = field
        self.value: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "input" or self.value is not None:
            return
        found = dict(attrs)
        if found.get("name") == self.field:
            self.value = found.get("value") or ""


def can_sign_in(portal: PortalMap) -> bool:
    """Whether this installation has both credentials and a mapped sign-in form."""
    return bool(_get("EBAY_PORTAL_USER", "") and _get("EBAY_PORTAL_PASSWORD", "")
                and portal.login_fields)


def sign_in(portal: PortalMap, session=None) -> dict[str, str]:
    """Establish a fresh session by posting the sign-in form, and return its cookies.

    The anti-forgery token is read from the form itself on every attempt rather than
    stored: it is bound to the page that issued it, so a cached one is worse than none.

    Raises :class:`AuthError` when the post completes but the portal did not hand back
    every cookie it is supposed to — a wrong password answers with the login page again
    and HTTP 200, which is otherwise indistinguishable from success.
    """
    import requests

    user = _get("EBAY_PORTAL_USER", "") or ""
    password = _get("EBAY_PORTAL_PASSWORD", "") or ""
    if not (user and password):
        raise AuthError(
            "no EBAY_PORTAL_USER/EBAY_PORTAL_PASSWORD is configured, so CoreYard cannot "
            "establish a portal session of its own."
        )
    if not portal.login_fields:
        raise AuthError(
            "this portal map has no auth.login_fields, so CoreYard cannot tell the "
            "sign-in form which input is the user and which is the password."
        )
    session = session or requests.Session()
    url = portal.base_url + portal.login_path
    headers = dict(portal.headers)
    form = {portal.login_fields["username"]: user,
            portal.login_fields["password"]: password}
    token_field = portal.login_fields.get("token")
    if token_field:
        page = session.get(url, headers=headers, timeout=60)
        parser = _Hidden(token_field)
        parser.feed(page.text)
        if parser.value:
            form[token_field] = parser.value
    session.post(url, data=form, headers=headers, timeout=60, allow_redirects=True)
    jar = dict(session.cookies.get_dict())
    missing = [name for name in portal.cookie_names if name not in jar]
    if missing:
        # Deliberately says which cookie is absent and nothing about the credentials.
        raise AuthError(
            f"portal sign-in did not establish a session (no {', '.join(missing)} "
            f"cookie). Check EBAY_PORTAL_USER and EBAY_PORTAL_PASSWORD."
        )
    try:
        save_cookies(jar)
    except OSError:
        pass
    return jar


def get_cookies(portal: PortalMap) -> dict[str, str]:
    """Explicit jar, then the live browser, then the cache, then a sign-in of our own."""
    explicit = parse_cookie_header(_get("EBAY_PORTAL_COOKIES", "") or "")
    if explicit:
        return explicit
    try:
        jar = cookies_from_firefox(portal)
    except AuthError:
        cached = cached_cookies()
        if cached and all(name in cached for name in portal.cookie_names):
            return cached
        if can_sign_in(portal):
            return sign_in(portal)
        raise
    try:
        save_cookies(jar)
    except OSError:
        pass
    return jar

