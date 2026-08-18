"""Read-only access to the source SQL database, over the SMB named pipe.

The source host commonly exposes SQL Server only via named pipes (no TCP), so we connect through
:mod:`coreyard.yms.smb_tds` — impacket's SMB2/3 opening the ``\\sql\\query`` pipe as the
existing service account and running TDS over it with Windows
Authentication. This is the vendor-endorsed "ODBC + Windows Auth" path, just from Linux.
Auth uses the SMB credentials; no separate SQL login exists on the installed system.

Everything here issues SELECT only, and ``query`` must not be reused for a write: impacket
reports server errors as reply tokens rather than raising, so a failed statement is
indistinguishable from one that returned no rows. The single write path, ``yms/orders.py``,
carries its own strict executor for that reason.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from coreyard.config import _get, load_env, load_smb_config


def _db_name(database: str | None = None) -> str:
    if database:
        return database
    load_env()
    # Read through config._get, not os.environ, so the legacy key fallback applies here too.
    name = (_get("YMS_DB_NAME", "") or "").strip()
    if not name:
        raise RuntimeError("YMS_DB_NAME is not set in .env — set it to your source database name.")
    return name


@contextmanager
def connect(database: str | None = None) -> Iterator[Any]:
    """Yield a live, logged-in TDS connection to ``database`` (default YMS_DB_NAME)."""
    from coreyard.yms.smb_tds import SmbTds

    smb = load_smb_config()
    db = _db_name(database)
    ms = SmbTds(smb.host, smb.server_name, smb.user, smb.password)
    ms.connect()
    if ms.login(db, smb.user, smb.password, "", None, True) is not True:  # useWindowsAuth
        ms.printReplies()
        ms.disconnect()
        raise RuntimeError(f"Windows-auth TDS login to database {db!r} failed.")
    try:
        yield ms
    finally:
        ms.disconnect()


def query(conn: Any, sql: str) -> list[dict[str, Any]]:
    """Run a SELECT and return rows as dicts (impacket already yields dict rows)."""
    conn.sql_query(sql)
    return [dict(r) for r in conn.rows]


def scalar(conn: Any, sql: str) -> Any:
    rows = query(conn, sql)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def ping() -> str:
    """Smoke test: connect and return ``@@VERSION`` (README verification step 1)."""
    with connect() as conn:
        return str(scalar(conn, "SELECT @@VERSION"))


if __name__ == "__main__":
    print(ping())
