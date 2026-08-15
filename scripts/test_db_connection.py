"""One-off connectivity test: reach the source SQL database over the SMB named pipe.

Run from the repo root with the venv Python:
    .venv/bin/python scripts/test_db_connection.py

Uses the service account from ``.env`` via Windows Authentication over the SMB
named pipe — the access method supported by the installed system. Prints @@VERSION and the
user databases on success so we can proceed to schema discovery.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.getLogger().setLevel(logging.CRITICAL)

from coreyard.config import load_smb_config  # noqa: E402
from coreyard.yms.smb_tds import SmbTds  # noqa: E402


def main() -> int:
    cfg = load_smb_config()
    last_err = None
    # The SMB session always authenticates locally; only the TDS login's NTLM domain varies.
    for dom in ("", cfg.server_name):
        print(f"\n=== TDS Windows-auth login (NTLM domain={dom!r}) ===")
        try:
            ms = SmbTds(cfg.host, cfg.server_name, cfg.user, cfg.password, smb_domain="")
            ms.connect()
            ok = ms.login("master", cfg.user, cfg.password, dom, None, True)  # useWindowsAuth
            if ok is not True:
                print("  login failed:")
                ms.printReplies()
                ms.disconnect()
                continue
            print("  LOGIN OK")
            ms.sql_query("SELECT @@VERSION AS v")
            print("  @@VERSION:", str(ms.rows[0]["v"]).splitlines()[0])
            ms.sql_query("SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY name")
            print("  User databases:", [r["name"] for r in ms.rows])
            ms.disconnect()
            print("\nSUCCESS — the source database is reachable from Linux over the named pipe.")
            return 0
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            last_err = exc
    print("\nFAILED to connect:", last_err)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
