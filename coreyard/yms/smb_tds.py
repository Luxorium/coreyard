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

import random
import sys
import time
from typing import Any

from impacket import nt_errors, tds
from impacket.smbconnection import SMBConnection

from coreyard.config import _get

DEFAULT_PIPE = r"sql\query"          # default instance; named instance = MSSQL$NAME\sql\query
_READ_CHUNK = 65535                   # one SMB2 READ returns one whole TDS packet/message
_DEBUG = (_get("COREYARD_SMB_DEBUG", "") or "").strip().lower() in {
    "1", "true", "yes", "on",
}

# Opening the pipe fails transiently, and often enough to matter: 62 scheduled runs died
# between 2026-09-03 and 2026-09-08 with STATUS_PIPE_NOT_AVAILABLE ("an instance of a named
# pipe cannot be found in the listening state"), spread evenly across every hour of the day.
# SQL Server accepts a bounded number of concurrent pipe instances, and this installation
# points five schedules at the same pipe — `counts` every minute, eBay every five, plus the
# sync, delta and order jobs — so they collide. The condition clears in milliseconds; what
# made it expensive was that a single failed open killed the whole run.
#
# Only statuses that can clear on their own are listed. A credential problem must fail on
# the first attempt and stay failed: retrying STATUS_LOGON_FAILURE four times against a
# domain account is how a service account gets locked out, which converts a wrong password
# into an outage for every job at once.
_TRANSIENT_STATUSES = frozenset({
    nt_errors.STATUS_PIPE_NOT_AVAILABLE,      # no pipe instance listening right now
    nt_errors.STATUS_PIPE_BUSY,               # every instance is in use
    nt_errors.STATUS_INSTANCE_NOT_AVAILABLE,  # same, reported from the other layer
    nt_errors.STATUS_IO_TIMEOUT,
    nt_errors.STATUS_CONNECTION_DISCONNECTED,
    nt_errors.STATUS_CONNECTION_RESET,
    nt_errors.STATUS_VIRTUAL_CIRCUIT_CLOSED,
})

DEFAULT_CONNECT_ATTEMPTS = 4
DEFAULT_CONNECT_BACKOFF = 1.0


def _status_of(exc: BaseException) -> int | None:
    """The NT status behind an impacket exception, or None if it carries none.

    Every SMB layer in impacket spells this differently — ``smb3.SessionError`` exposes
    ``error`` and ``get_error_code()``, ``smbconnection.SessionError`` exposes
    ``getErrorCode()``, ``smb.SessionError`` exposes ``get_error_class()`` — and which one
    surfaces depends on the dialect negotiated at runtime. Asking for each in turn is
    duplication that earns its keep: a transport error misread as unclassifiable would be
    re-raised on the first attempt, which is the behaviour being fixed.
    """
    for name in ("get_error_code", "getErrorCode"):
        getter = getattr(exc, name, None)
        if callable(getter):
            try:
                code = getter()
            except Exception:
                continue
            if isinstance(code, int):
                return code
    code = getattr(exc, "error", None)
    return code if isinstance(code, int) else None


def _is_transient(exc: BaseException) -> bool:
    """Is another attempt worth making, or is this the same answer every time?

    Socket-level failures are retried without consulting a status: they happen before
    authentication, so they cannot be a credential problem, and a refused or reset
    connection to a Windows host that is mid-restart is the textbook case for waiting.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    status = _status_of(exc)
    if status is not None:
        return status in _TRANSIENT_STATUSES
    # NetBIOSError and friends carry no status and are transport-level by nature. OSError
    # covers the rest of the socket surface.
    return isinstance(exc, OSError) or type(exc).__name__ == "NetBIOSError"


def _positive_int(key: str, default: int) -> int:
    raw = (_get(key, str(default)) or "").strip()
    try:
        return max(1, int(raw or default))
    except ValueError:
        return default


def _positive_float(key: str, default: float) -> float:
    raw = (_get(key, str(default)) or "").strip()
    try:
        return max(0.0, float(raw or default))
    except ValueError:
        return default


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
        """Open the pipe, retrying the transient refusals that used to kill whole runs.

        Retried *here and only here*. Establishing the connection is the one step that can
        be repeated with no consequence, because nothing has been sent yet. A pipe that
        breaks mid-statement is not retried at any layer above this: the single write path
        (``yms/orders.py``) is a money transaction, and re-running a batch whose outcome is
        unknown could book a work order twice. It already handles that its own way, by
        making the write idempotent at the source.
        """
        attempts = _positive_int("COREYARD_SMB_CONNECT_ATTEMPTS", DEFAULT_CONNECT_ATTEMPTS)
        backoff = _positive_float("COREYARD_SMB_CONNECT_BACKOFF", DEFAULT_CONNECT_BACKOFF)
        for attempt in range(1, attempts + 1):
            try:
                return self._connect_once()
            except Exception as exc:
                if attempt == attempts or not _is_transient(exc):
                    raise
                # Jittered, so that jobs which collided on the first attempt — `counts`
                # runs every minute and eBay every five, against this same pipe — do not
                # line up and collide again on the retry.
                pause = backoff * (2 ** (attempt - 1))
                pause += random.uniform(0, pause / 2)
                print(f"  . source pipe unavailable ({type(exc).__name__}); "
                      f"retrying in {pause:.1f}s ({attempt}/{attempts - 1})",
                      file=sys.stderr)
                time.sleep(pause)
        raise AssertionError("unreachable: the loop returns or raises")

    def _connect_once(self):
        """One full attempt, leaving nothing behind if it fails part-way.

        The four steps below fail at different points and the later ones hold a live socket
        and an authenticated session. Dropping that on the floor and looping would leak a
        session per attempt against a server whose pipe instances are already exhausted —
        making the next attempt likelier to fail for the same reason.
        """
        host, name, user, pwd, domain, pipe = self._smb
        conn = SMBConnection(name, host, sess_port=445, timeout=20)
        try:
            conn.login(user, pwd, domain=domain)
            tid = conn.connectTree("IPC$")
            fid = conn.openFile(tid, pipe)
        except Exception:
            try:
                conn.logoff()
            except Exception:
                pass        # already dead; the original failure is the one worth raising
            raise
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
