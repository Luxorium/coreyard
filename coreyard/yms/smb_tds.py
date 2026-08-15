"""Reach SQL Server over an SMB2/3 named pipe from Linux.

Such hosts often expose SQL Server ONLY via named pipes over SMB (139/445); there is no TCP
SQL port, and SMB1 is disabled (so jTDS can't help). Standard Linux SQL drivers are
TCP-only. This module bridges the gap: it opens the SQL named pipe (``\\sql\\query``) with
impacket's SMB2/3 client authenticated as the existing service account, then runs impacket's
real TDS engine over that pipe by swapping the TDS client's TCP socket for a pipe-backed
shim. Windows Authentication (NTLM) happens at the TDS layer, exactly as the installed
"ODBC + Windows Auth" method intends — just from Linux.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from impacket import tds
from impacket.smbconnection import SMBConnection

DEFAULT_PIPE = r"sql\query"          # default instance; named instance = MSSQL$NAME\sql\query
_READ_CHUNK = 65535                   # one SMB2 READ returns one whole TDS packet/message
_DEBUG = bool(os.environ.get("COREYARD_SMB_DEBUG"))


class _PipeSocket:
    """Minimal socket-like transport over an SMB2 named-pipe handle.

    impacket's TDS engine only ever calls ``sendall``/``recv`` (even for its memory-BIO
    TLS handshake), so these two methods are all that's required. Reads pull one pipe
    message at a time into a buffer and hand back exactly what TDS asks for.
    """

    def __init__(self, conn: SMBConnection, tid: int, fid: Any) -> None:
        self._conn, self._tid, self._fid = conn, tid, fid
        self._buf = b""       # leftover bytes already read from the pipe
        self._pending = b""   # buffered request bytes, flushed on the next read

    def sendall(self, data: bytes) -> None:
        # Buffer; TDS writes a full message then reads, so we flush as one transceive.
        self._pending += data

    def send(self, data: bytes) -> int:
        self._pending += data
        return len(data)

    def _fill(self) -> bytes:
        """Pull the next pipe message. SQL's message-mode pipe answers a request only
        via FSCTL_PIPE_TRANSCEIVE (write+read atomically); continuation messages of a
        multi-packet response are then read with a single SMB2 read."""
        if self._pending:
            data, self._pending = self._pending, b""
            resp = self._conn.transactNamedPipe(self._tid, self._fid, data, waitAnswer=True) or b""
            if _DEBUG:
                print(f"[pipe] TRANSCEIVE out={len(data)} in={len(resp)}", file=sys.stderr)
            return resp
        resp = self._conn.readFile(
            self._tid, self._fid, 0, bytesToRead=_READ_CHUNK, singleCall=True
        ) or b""
        if _DEBUG:
            print(f"[pipe] READ in={len(resp)}", file=sys.stderr)
        return resp

    def recv(self, n: int) -> bytes:
        # Return UP TO n bytes (real-socket semantics). Blocking until exactly n was the
        # bug: impacket calls recv(4096) and only needs the 37-byte packet already read.
        if not self._buf:
            self._buf = self._fill()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def settimeout(self, _t) -> None:  # TDS sets a timeout; irrelevant for the pipe
        pass

    def setsockopt(self, *a) -> None:
        pass

    def getpeername(self):
        return (self._conn.getRemoteHost(), 0)

    def close(self) -> None:
        try:
            self._conn.closeFile(self._tid, self._fid)
        except Exception:
            pass


class SmbTds(tds.MSSQL):
    """impacket TDS client whose transport is an SMB named pipe instead of TCP."""

    def __init__(
        self,
        host: str,
        server_name: str,
        smb_user: str,
        smb_password: str,
        smb_domain: str = "",
        pipe: str = DEFAULT_PIPE,
    ) -> None:
        super().__init__(host, 1433)
        self._smb = (host, server_name, smb_user, smb_password, smb_domain, pipe)
        self._smb_conn: SMBConnection | None = None

    def connect(self):  # override: build the pipe transport, not a TCP socket
        host, name, user, pwd, domain, pipe = self._smb
        conn = SMBConnection(name, host, sess_port=445, timeout=20)
        conn.login(user, pwd, domain=domain)
        tid = conn.connectTree("IPC$")
        fid = conn.openFile(tid, pipe)
        self._smb_conn = conn
        self.socket = _PipeSocket(conn, tid, fid)
        return self.socket

    def disconnect(self) -> None:
        try:
            if self.socket:
                self.socket.close()
        finally:
            if self._smb_conn:
                try:
                    self._smb_conn.logoff()
                except Exception:
                    pass


def query(sql: str, host: str, server_name: str, user: str, password: str,
          domain: str = "", database: str = "master") -> list[dict[str, Any]]:
    """Connect over the named pipe with Windows Auth and run one query -> list of dict rows."""
    ms = SmbTds(host, server_name, user, password, smb_domain=domain)
    ms.connect()
    if not ms.login(database, user, password, domain, None, True):  # useWindowsAuth=True
        ms.printReplies()
        raise RuntimeError("TDS Windows-auth login failed")
    ms.sql_query(sql)
    rows = list(ms.rows)
    ms.disconnect()
    return rows
