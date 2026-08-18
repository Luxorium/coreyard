"""One-time Admin API token grab (OAuth authorization-code, offline token).

The custom app created in the Shopify **dev dashboard** exposes a Client ID + Client
Secret instead of a direct ``shpat_`` token. This helper turns those into a permanent
offline Admin API access token: it runs a localhost callback server, opens the store's
OAuth consent screen, exchanges the returned ``code`` for a token, and writes it into
``.env`` as ``SHOPIFY_ADMIN_TOKEN``. Dependency-free (stdlib only).

Prerequisites
-------------
1. In the app config (dev dashboard → your app → Versions → Redirect URLs), add exactly:
       http://localhost:3456/callback
   and Release the new version.
2. In ``.env`` set:  SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET

Run
---
    python -m coreyard.sink.shopify_oauth
Then approve the install in the browser window that opens.
"""

from __future__ import annotations

import json
import os
import time
import secrets
import urllib.parse
import urllib.request
import webbrowser
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer


class _DualStackServer(HTTPServer):
    """Callback server that answers on both IPv4 and IPv6 loopback.

    An `ssh -L 3456:localhost:3456` forward resolves "localhost" on the remote host, which
    usually yields ::1 first. A plain IPv4 bind refuses that connection, so the browser
    never reaches the callback. Binding dual-stack makes either spelling work.
    """

    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        HTTPServer.server_bind(self)

from coreyard.config import ENV_PATH, REPO_ROOT, _get, load_env

DEFAULT_REDIRECT = "http://localhost:3456/callback"
# read/write_files lets the publisher attach photos and set their alt text; read_orders lets
# the app subscribe to paid-order webhooks. Without them Shopify answers ACCESS_DENIED.
# Changing this list has no effect on an existing token: re-authorize to mint a new one.
DEFAULT_SCOPES = (
    "read_products,write_products,"
    "read_inventory,write_inventory,"
    "read_locations,"
    "read_files,write_files,"
    "read_orders"
)

_result: dict[str, str] = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != urllib.parse.urlparse(_result["redirect"]).path:
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _result["code"] = params.get("code", [""])[0]
        _result["state"] = params.get("state", [""])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h2>CoreYard &mdash; authorization received.</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
        )

    def log_message(self, *args):  # silence default logging
        pass


def _upsert_env(key: str, value: str) -> None:
    """Set KEY=value in .env, replacing an existing line or appending."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    out, found = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)


def main() -> int:
    load_env()
    store = (_get("SHOPIFY_STORE", "") or "").strip()
    client_id = (_get("SHOPIFY_CLIENT_ID", "") or "").strip()
    client_secret = (_get("SHOPIFY_CLIENT_SECRET", "") or "").strip()
    scopes = (_get("SHOPIFY_SCOPES", DEFAULT_SCOPES) or DEFAULT_SCOPES).strip()
    redirect = (_get("SHOPIFY_OAUTH_REDIRECT", DEFAULT_REDIRECT) or DEFAULT_REDIRECT).strip()
    timeout = int(_get("SHOPIFY_OAUTH_TIMEOUT", "600") or "600")

    missing = [k for k, v in {
        "SHOPIFY_STORE": store,
        "SHOPIFY_CLIENT_ID": client_id,
        "SHOPIFY_CLIENT_SECRET": client_secret,
    }.items() if not v]
    if missing:
        print("Missing in .env: " + ", ".join(missing))
        return 2

    state = secrets.token_urlsafe(16)
    _result["redirect"] = redirect
    parsed_redirect = urllib.parse.urlparse(redirect)
    host = parsed_redirect.hostname or "localhost"
    port = parsed_redirect.port or 80

    authorize = f"https://{store}/admin/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect,
        "state": state,
    })

    # Expose the consent URL to a file so it can be relayed/clicked without the terminal.
    url_file = REPO_ROOT / "out" / "oauth_authorize_url.txt"
    url_file.parent.mkdir(parents=True, exist_ok=True)
    url_file.write_text(authorize + "\n", encoding="utf-8")

    try:
        server = _DualStackServer(("::", port), _Handler)
        bound = f"[::]:{port} (IPv4 + IPv6)"
    except OSError:                       # no IPv6 on this box
        server = HTTPServer((host, port), _Handler)
        bound = f"{host}:{port} (IPv4)"
    print(f"Listening on {redirect}  (bound to {bound})")
    print("\nOpen this URL and approve the install:\n")
    print("  " + authorize + "\n")
    if host in ("localhost", "127.0.0.1"):
        print("Over SSH? Any of these forwards work now:")
        print(f"  ssh -L {port}:localhost:{port} <user>@<host>")
        print(f"  ssh -L {port}:127.0.0.1:{port} <user>@<host>\n")
    try:
        webbrowser.open(authorize)
    except Exception:
        pass

    # Keep serving until the real callback arrives. A single handle_request() was a bug:
    # any stray hit (a browser probe, a favicon fetch, the webbrowser.open attempt) was
    # answered with a 404 and the server stopped before the user ever approved.
    print(f"Waiting up to {timeout}s for the callback ... (Ctrl-C to give up)")
    server.timeout = 5
    deadline = time.monotonic() + timeout
    while not _result.get("code") and time.monotonic() < deadline:
        server.handle_request()

    if not _result.get("code"):
        print("No authorization code received.")
        return 1
    if _result.get("state") != state:
        print("State mismatch — aborting for safety.")
        return 1

    # Exchange the code for a permanent offline access token.
    token_url = f"https://{store}/admin/oauth/access_token"
    body = json.dumps({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _result["code"],
    }).encode("utf-8")
    req = urllib.request.Request(
        token_url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    token = payload.get("access_token")
    if not token:
        print("No access_token in response:", payload)
        return 1

    _upsert_env("SHOPIFY_ADMIN_TOKEN", token)
    print(f"\nSuccess. Wrote SHOPIFY_ADMIN_TOKEN to {ENV_PATH} (token {token[:10]}…).")
    print("Granted scopes:", payload.get("scope", "?"))
    print("\nNext:  python -m coreyard.run_sync --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
