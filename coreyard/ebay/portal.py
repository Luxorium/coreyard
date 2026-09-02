"""Load the installation-supplied listing-portal protocol map.

The intermediary that manages an installation's eBay listings exposes its own route
names, request fields, response fields, cookie names, status values, and bulk-action IDs.
Those identifiers are licensed-system schema in exactly the same sense as source database
column names: CoreYard must consume them, but must not publish them.  They therefore live
in the local, gitignored ``portal.json`` and are translated to the neutral names used by
the rest of this package here.

This loader does not call :func:`coreyard.config.load_env`.  Explicit-path validation and
unit tests must remain independent of the installation's real ``.env``; operator commands
load the environment before asking for the configured path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from coreyard.config import REPO_ROOT, _get


class PortalConfigError(ValueError):
    """The portal protocol map is absent or incomplete."""


_ROOT_KEYS = {
    "base_url", "auth", "headers", "statuses", "endpoints", "parameters",
    "responses", "row_fields", "detail_fields", "forms", "fields", "actions",
    "filters",
}
_ENDPOINTS = {"grid", "detail", "save_detail", "bulk_update", "submit", "end"}
_STATUSES = {"unlisted", "listed", "flagged", "sold"}
_PARAMETERS = {
    "grid": {
        "status", "page", "rows", "sort_index", "sort_order", "search",
        "timestamp", "part_type", "quick_search",
    },
    "detail": {"listing_id", "status"},
    "bulk_update": {
        "changes", "update_all", "listing_ids", "status", "session",
        "reset_titles", "overwrite_mpn", "item_specifics", "change_field",
        "change_action", "change_value",
    },
    "submit": {
        "update_all", "listing_ids", "status", "session", "list_as_new",
        "clear_errors",
    },
    "end": {"update_all", "listing_ids", "status", "session"},
}
_GRID_RESPONSE = {"pages", "page", "records", "rows", "session"}
_ROW_FIELDS = {"listing_id", "interchange_number", "title", "price", "part_type"}
_FIELDS = {"title", "fixed_price", "starting_price"}
_ACTIONS = {"change_to", "reset_to_retail"}


def _object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise PortalConfigError(f"{where} must be a JSON object")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortalConfigError(f"{where} must be a non-empty string")
    return value.strip()


def _string_map(value: Any, where: str, required=()) -> dict[str, str]:
    obj = _object(value, where)
    out = {str(key): _string(item, f"{where}.{key}") for key, item in obj.items()}
    missing = sorted(set(required) - set(out))
    if missing:
        raise PortalConfigError(f"{where} is missing {', '.join(missing)}")
    return out


@dataclass(frozen=True)
class PortalMap:
    """A complete translation from portal vocabulary to CoreYard vocabulary."""

    base_url: str
    cookie_names: tuple[str, ...]
    login_path: str
    login_fields: Mapping[str, str]
    headers: Mapping[str, str]
    statuses: Mapping[str, str]
    endpoints: Mapping[str, str]
    parameters: Mapping[str, Mapping[str, str]]
    responses: Mapping[str, Mapping[str, str]]
    row_fields: Mapping[str, str]
    detail_fields: Mapping[str, str]
    forms: Mapping[str, str]
    fields: Mapping[str, str]
    actions: Mapping[str, str]
    filters: Mapping[str, str]

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    def endpoint(self, name: str) -> str:
        path = self.endpoints[name]
        return path if path.startswith(("http://", "https://")) else self.base_url + path

    def parameter(self, operation: str, name: str) -> str:
        return self.parameters[operation][name]

    def response(self, operation: str, name: str) -> str:
        return self.responses[operation][name]

    def status(self, tab: str) -> str:
        try:
            return self.statuses[tab.lower()]
        except KeyError as exc:
            raise PortalConfigError(
                f"unknown portal tab {tab!r}; choose {', '.join(sorted(self.statuses))}"
            ) from exc

    def normalize_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Translate one grid row, dropping every unmapped vendor field."""
        out = {name: row.get(source) for name, source in self.row_fields.items()}
        if out.get("listing_id") is not None:
            out["listing_id"] = str(out["listing_id"])
        for name in ("interchange_number", "title", "part_type", "r_number",
                     "stock_number"):
            if name in out and out[name] is not None:
                out[name] = str(out[name])
        return out

    def normalize_detail(self, fields: list[tuple[str, str]]) -> dict[str, str]:
        """Translate a serialized edit form without leaking form-control names."""
        raw: dict[str, str] = {}
        for name, value in fields:
            raw.setdefault(name.split(".")[-1], value)
        return {name: raw.get(source, "") for name, source in self.detail_fields.items()}

    def part_type_filter(self, value: str) -> tuple[str, str]:
        """Return either a direct part-type parameter or a configured quick-search."""
        template = self.filters.get("part_type")
        return ("quick_search", template.format(value=value)) if template else (
            "part_type", value)


def load(path: str | Path | None = None) -> PortalMap:
    """Read and validate a portal map.

    When ``path`` is omitted, ``EBAY_PORTAL_FILE`` is consulted through ``config._get``
    and then falls back to the ignored repository-root ``portal.json``.
    """
    configured = path or _get("EBAY_PORTAL_FILE", str(REPO_ROOT / "portal.json"))
    source = Path(str(configured))
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PortalConfigError(
            f"portal map not found: {source}; set EBAY_PORTAL_FILE or create portal.json"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PortalConfigError(f"cannot read portal map {source}: {exc}") from exc
    data = _object(data, str(source))
    unknown = sorted(set(data) - _ROOT_KEYS)
    if unknown:
        raise PortalConfigError(f"{source}: unknown key(s): {', '.join(unknown)}")

    base_url = _string(data.get("base_url"), "base_url").rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PortalConfigError("base_url must be an absolute HTTP(S) URL")

    auth = _object(data.get("auth"), "auth")
    unknown_auth = sorted(set(auth) - {"cookies", "login_path", "login_fields"})
    if unknown_auth:
        raise PortalConfigError(f"auth: unknown key(s): {', '.join(unknown_auth)}")
    cookies = auth.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise PortalConfigError("auth.cookies must be a non-empty array")
    cookie_names = tuple(_string(item, "auth.cookies[]") for item in cookies)
    if len(set(cookie_names)) != len(cookie_names):
        raise PortalConfigError("auth.cookies contains a duplicate name")

    login_fields: dict[str, str] = {}
    if auth.get("login_fields") is not None:
        # Which input the sign-in form calls the user and which it calls the password is
        # the portal's vocabulary, exactly like a grid parameter or a tab status, so it is
        # mapped here rather than spelled into the client. ``token`` is optional: a form
        # with no anti-forgery field simply does not name one.
        login_fields = _string_map(auth.get("login_fields"), "auth.login_fields",
                                   {"username", "password"})

    parameters = _object(data.get("parameters"), "parameters")
    parameter_maps = {
        operation: _string_map(parameters.get(operation), f"parameters.{operation}", names)
        for operation, names in _PARAMETERS.items()
    }
    responses = _object(data.get("responses"), "responses")
    response_maps = {
        "grid": _string_map(responses.get("grid"), "responses.grid", _GRID_RESPONSE)
    }

    forms = _string_map(data.get("forms"), "forms", {"detail"})
    return PortalMap(
        base_url=base_url,
        cookie_names=cookie_names,
        login_path=_string(auth.get("login_path"), "auth.login_path"),
        login_fields=login_fields,
        headers=_string_map(data.get("headers", {}), "headers"),
        statuses=_string_map(data.get("statuses"), "statuses", _STATUSES),
        endpoints=_string_map(data.get("endpoints"), "endpoints", _ENDPOINTS),
        parameters=parameter_maps,
        responses=response_maps,
        row_fields=_string_map(data.get("row_fields"), "row_fields", _ROW_FIELDS),
        detail_fields=_string_map(data.get("detail_fields", {}), "detail_fields"),
        forms=forms,
        fields=_string_map(data.get("fields"), "fields", _FIELDS),
        actions=_string_map(data.get("actions"), "actions", _ACTIONS),
        filters=_string_map(data.get("filters", {}), "filters"),
    )
