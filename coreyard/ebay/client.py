"""HTTP transport for the configured eBay listing portal.

The client owns protocol mechanics and the two catalogue-wide hazards: every mutation
requires explicit listing IDs, and the portal's inverted "update all" mode is refused at
the transport boundary.  Operator-level dry runs and batch caps are added by the channel
workflow; these lower-level guards remain even if a future caller gets that layer wrong.
"""

from __future__ import annotations

import json
import time
from html.parser import HTMLParser
from typing import Any, Iterator
from urllib.parse import quote

from coreyard.config import _get
from coreyard.ebay.auth import get_cookies
from coreyard.ebay.portal import PortalConfigError, PortalMap, load as load_portal


class PortalError(RuntimeError):
    """The portal rejected or could not understand an operation."""


class SessionExpired(PortalError):
    """The configured browser session is no longer authenticated."""


class _FormParser(HTMLParser):
    """Serialize one HTML form the same way browser form submission does."""

    def __init__(self, form_id: str):
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.fields: list[tuple[str, str]] = []
        self._in_form = False
        self._select: str | None = None
        self._selected = False
        self._textarea: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form":
            self._in_form = values.get("id") == self.form_id
            return
        if not self._in_form:
            return
        if tag == "input":
            name = values.get("name")
            kind = (values.get("type") or "text").lower()
            if not name or kind == "submit":
                return
            if kind not in {"checkbox", "radio"} or "checked" in values:
                self.fields.append((name, values.get("value", "on" if kind in {
                    "checkbox", "radio"} else "")))
        elif tag == "select":
            self._select, self._selected = values.get("name"), False
        elif tag == "option" and self._select and "selected" in values:
            self.fields.append((self._select, values.get("value", "")))
            self._selected = True
        elif tag == "textarea":
            self._textarea, self._text = values.get("name"), []

    def handle_data(self, data):
        if self._textarea is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False
        elif tag == "select" and self._select:
            if not self._selected:
                self.fields.append((self._select, ""))
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            self.fields.append((self._textarea, "".join(self._text)))
            self._textarea = None


def _number(key: str, default: float) -> float:
    raw = (_get(key, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise PortalError(f"{key} must be a number, not {raw!r}") from exc
    if value < 0:
        raise PortalError(f"{key} must not be negative")
    return value


# An expired session does not always arrive as a redirect. The portal answers one with
# HTTP 200, an empty grid and an all-zero session handle — so nothing in ``_request``
# fires, and the handle reaches ``bulk_update`` as a perfectly ordinary non-empty string.
# The portal then *accepts* every write made against it and discards them, returning no
# error, which is how a batch of 12,887 prices once reported complete success and changed
# nothing.
def _is_anonymous_session(session_id: str | None) -> bool:
    """True for a missing handle or the all-zero one a signed-out request comes back with."""
    return not session_id or not str(session_id).strip("0-")


class PortalClient:
    def __init__(
        self,
        portal: PortalMap | None = None,
        cookies: dict[str, str] | None = None,
        *,
        session=None,
        delay: float | None = None,
        timeout: float | None = None,
    ) -> None:
        self.portal = portal or load_portal()
        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        self.session.cookies.update(cookies or get_cookies(self.portal))
        self.session.headers.update(self.portal.headers)
        self.delay = _number("EBAY_PORTAL_REQUEST_DELAY", 0.4) if delay is None else delay
        self.timeout = _number("EBAY_PORTAL_TIMEOUT", 60.0) if timeout is None else timeout
        self.session_id: str | None = None

    def _request(self, method: str, endpoint: str, **kwargs):
        response = self.session.request(
            method, self.portal.endpoint(endpoint), timeout=self.timeout,
            allow_redirects=False, **kwargs
        )
        location = response.headers.get("Location", "")
        if response.status_code in {401, 403} or (
            response.status_code in {301, 302}
            and self.portal.login_path in location
        ):
            raise SessionExpired(
                f"listing-portal session expired (HTTP {response.status_code}); "
                f"log in at {self.portal.base_url} and retry"
            )
        if response.status_code >= 400:
            raise PortalError(
                f"{method} {endpoint} -> HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        if self.delay:
            time.sleep(self.delay)
        return response

    def grid(
        self,
        *,
        tab: str = "unlisted",
        page: int = 1,
        rows: int = 100,
        sort_index: str = "",
        sort_order: str = "asc",
        part_type: str | int | None = None,
        quick_search: str | None = None,
        search: dict | None = None,
    ) -> dict[str, Any]:
        """Read one grid page and return only neutral, mapped field names."""
        if page < 1 or rows < 1:
            raise ValueError("page and rows must both be at least 1")
        p = self.portal.parameter
        params: dict[str, Any] = {
            p("grid", "status"): self.portal.status(tab),
            p("grid", "page"): page,
            p("grid", "rows"): rows,
            p("grid", "sort_index"): sort_index,
            p("grid", "sort_order"): sort_order,
            p("grid", "search"): (
                json.dumps(search, separators=(",", ":")) if search else "false"
            ),
            p("grid", "timestamp"): int(time.time() * 1000),
        }
        if part_type is not None:
            params[p("grid", "part_type")] = part_type
        if quick_search:
            params[p("grid", "quick_search")] = quick_search
        raw = self._request("GET", "grid", params=params).json()
        r = self.portal.response
        session_id = raw.get(r("grid", "session"))
        if _is_anonymous_session(session_id):
            # Deliberately keyed on the handle and not on an empty grid: a filtered read
            # of a part type the yard has none of is legitimately empty, and treating that
            # as a dead session would refuse perfectly good reads.
            raise SessionExpired(
                "the listing portal returned an anonymous session handle, so this client "
                "is not signed in. Reads come back empty and writes are accepted and "
                f"discarded. Sign in at {self.portal.base_url} and retry"
            )
        self.session_id = str(session_id)
        source_rows = raw.get(r("grid", "rows")) or []
        if not isinstance(source_rows, list):
            raise PortalError("grid response rows are not an array")
        return {
            "pages": int(raw.get(r("grid", "pages")) or 1),
            "page": int(raw.get(r("grid", "page")) or page),
            "records": int(raw.get(r("grid", "records")) or len(source_rows)),
            "rows": [self.portal.normalize_row(item) for item in source_rows],
            "session": self.session_id,
        }

    def iter_listings(self, *, tab: str = "unlisted", rows: int = 100,
                      limit: int | None = None, part_type: str | None = None,
                      **kwargs) -> Iterator[dict[str, Any]]:
        """Page through a tab, optionally stopping after ``limit`` normalized rows."""
        if limit is not None and limit < 1:
            raise ValueError("limit must be at least 1")
        filter_args = {}
        if part_type is not None:
            kind, value = self.portal.part_type_filter(part_type)
            filter_args[kind] = value
        page, pages, yielded, records = 1, None, 0, None
        while pages is None or page <= pages:
            data = self.grid(tab=tab, page=page, rows=rows, **filter_args, **kwargs)
            pages = data["pages"]
            if records is None:
                records = data["records"]
            if not data["rows"]:
                break
            for item in data["rows"]:
                yield item
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            page += 1
        # The portal reports a page count it will not actually serve: asking for page 2
        # returns an empty result set however the sort is specified, and a row count large
        # enough to cover the tab in one request fails outright. So a wide read silently
        # stops partway, and every caller — a pull that looks complete, a preflight that
        # concludes a listing is gone, a retirement that infers a part left the yard —
        # would be reasoning about a slice while believing it had the whole tab.
        #
        # Narrowing the query is what actually works: a filtered read returns its whole
        # result set in one page. So this refuses rather than returning short.
        if limit is None and records is not None and yielded < records:
            raise PortalError(
                f"portal returned {yielded} of {records} {tab} listings and will not "
                f"serve the rest: narrow the query (for example by part type) or raise "
                f"rows above {rows}"
            )

    def listing_form(self, listing_id: str, *, tab: str = "unlisted") -> list[tuple[str, str]]:
        p = self.portal.parameter
        params = {
            p("detail", "listing_id"): listing_id,
            p("detail", "status"): self.portal.status(tab).upper(),
        }
        parser = _FormParser(self.portal.forms["detail"])
        parser.feed(self._request("GET", "detail", params=params).text)
        if not parser.fields:
            raise PortalError(f"no configured detail form found for listing {listing_id}")
        return parser.fields

    def listing_detail(self, listing_id: str, *, tab: str = "unlisted") -> dict[str, str]:
        return self.portal.normalize_detail(self.listing_form(listing_id, tab=tab))

    def save_detail(self, fields: list[tuple[str, str]]) -> str:
        if not fields:
            raise PortalError("save_detail needs a complete serialized form")
        return _response_text(self._request("POST", "save_detail", data=fields))

    def bulk_update(
        self,
        changes: list[dict[str, Any]],
        listing_ids: list[str],
        *,
        tab: str = "unlisted",
        session_id: str | None = None,
        update_all: bool = False,
        reset_titles: bool = False,
        overwrite_mpn: bool = False,
        item_specifics: dict | None = None,
    ) -> str:
        """Save portal-side changes without publishing them to eBay."""
        if update_all:
            raise PortalError(
                "update_all uses exclusion semantics and is never allowed; pass explicit IDs"
            )
        if not listing_ids:
            raise PortalError("bulk_update needs explicit listing IDs")
        token = session_id or self.session_id
        if not token:
            raise PortalError("bulk_update needs a live session token; call grid() first")
        if _is_anonymous_session(token):
            # Refused before the first write rather than reported afterwards: the portal
            # takes a write made against an anonymous handle, answers without an error and
            # changes nothing, so every guard downstream of here would call it a success.
            raise SessionExpired(
                "refusing to write with an anonymous session handle: the portal would "
                "accept these changes and discard them silently. Re-read the grid to "
                "establish a live session first"
            )
        p = self.portal.parameter
        encoded = []
        for change in changes:
            try:
                field_id = self.portal.fields[str(change["field"])]
                action_id = self.portal.actions[str(change["action"])]
            except KeyError as exc:
                raise PortalConfigError(f"unknown bulk field/action: {exc.args[0]}") from exc
            encoded.append({
                p("bulk_update", "change_field"): field_id,
                p("bulk_update", "change_action"): action_id,
                p("bulk_update", "change_value"): quote(
                    str(change.get("value", "")), safe="~!*()'"
                ),
            })
        payload = {
            p("bulk_update", "changes"): json.dumps(encoded, separators=(",", ":")),
            p("bulk_update", "update_all"): "false",
            p("bulk_update", "listing_ids"): ",".join(map(str, listing_ids)),
            p("bulk_update", "status"): self.portal.status(tab),
            p("bulk_update", "session"): token,
            p("bulk_update", "reset_titles"): str(reset_titles).lower(),
            p("bulk_update", "overwrite_mpn"): str(overwrite_mpn).lower(),
            p("bulk_update", "item_specifics"): (
                json.dumps(item_specifics, separators=(",", ":")) if item_specifics else ""
            ),
        }
        return _response_text(self._request("POST", "bulk_update", data=payload))

    def submit_listings(
        self,
        listing_ids: list[str],
        *,
        tab: str = "unlisted",
        session_id: str | None = None,
        list_as_new: bool = False,
        clear_errors: bool = False,
        batch_size: int = 100,
    ) -> str:
        """Publish explicit listings in bounded query-string batches."""
        token = self._write_args("submit", listing_ids, session_id, batch_size)
        p = self.portal.parameter
        messages = []
        for start in range(0, len(listing_ids), batch_size):
            batch = listing_ids[start:start + batch_size]
            params = {
                p("submit", "update_all"): "false",
                p("submit", "listing_ids"): ",".join(map(str, batch)),
                p("submit", "status"): self.portal.status(tab),
                p("submit", "session"): token,
                p("submit", "list_as_new"): str(list_as_new).lower(),
                p("submit", "clear_errors"): str(clear_errors).lower(),
            }
            message = _response_text(self._request("POST", "submit", params=params))
            if message:
                messages.append(message)
        return "\n".join(messages)

    def end_listings(
        self,
        listing_ids: list[str],
        *,
        tab: str = "listed",
        session_id: str | None = None,
        batch_size: int = 100,
    ) -> str:
        """End explicit live listings. This operation is irreversible on eBay."""
        token = self._write_args("end", listing_ids, session_id, batch_size)
        p = self.portal.parameter
        messages = []
        for start in range(0, len(listing_ids), batch_size):
            batch = listing_ids[start:start + batch_size]
            params = {
                p("end", "update_all"): "false",
                p("end", "listing_ids"): ",".join(map(str, batch)),
                p("end", "status"): self.portal.status(tab),
                p("end", "session"): token,
            }
            message = _response_text(self._request("POST", "end", params=params))
            if message:
                messages.append(message)
        return "\n".join(messages)

    def _write_args(self, operation: str, listing_ids: list[str],
                    session_id: str | None, batch_size: int) -> str:
        if not listing_ids:
            raise PortalError(f"{operation} needs explicit listing IDs")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        token = session_id or self.session_id
        if not token:
            raise PortalError(f"{operation} needs a live session token; call grid() first")
        return token


def _response_text(response) -> str:
    try:
        value = response.json()
    except ValueError:
        return response.text.strip()
    return "" if value in (None, "") else (
        value if isinstance(value, str) else json.dumps(value)
    )
