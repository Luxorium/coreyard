"""Opening the SQL named pipe, and surviving the times it is momentarily not there.

62 scheduled runs died between 2026-09-03 and 2026-09-08 on STATUS_PIPE_NOT_AVAILABLE —
"an instance of a named pipe cannot be found in the listening state". SQL Server allows a
bounded number of concurrent pipe instances and this installation points five schedules at
the same pipe, so they collide; the condition clears in milliseconds. What made it
expensive was that one failed open killed the entire run, taking the catalogue sync with it.

The tests that matter most here are the ones about what is *not* retried. Repeating a
credential failure against a domain account is how a service account gets locked out, which
turns a wrong password into a simultaneous outage for every job on the box.
"""

import contextlib
import io
import unittest
from unittest import mock

from impacket import nt_errors, smb3

from coreyard.yms import smb_tds


def pipe_unavailable():
    return smb3.SessionError(nt_errors.STATUS_PIPE_NOT_AVAILABLE)


def logon_failure():
    return smb3.SessionError(nt_errors.STATUS_LOGON_FAILURE)


class FakeSMB:
    """An SMBConnection whose openFile fails a set number of times before working."""

    def __init__(self, failures=0, error=None, fail_on="openFile"):
        self.remaining = failures
        self.error = error or pipe_unavailable()
        self.fail_on = fail_on
        self.logoffs = 0
        self.opened = 0

    def login(self, *a, **k):
        if self.fail_on == "login" and self.remaining:
            self.remaining -= 1
            raise self.error

    def connectTree(self, _share):
        return 7

    def openFile(self, _tid, _pipe):
        if self.fail_on == "openFile" and self.remaining:
            self.remaining -= 1
            raise self.error
        self.opened += 1
        return 42

    def logoff(self):
        self.logoffs += 1

    def getRemoteHost(self):
        return "10.0.0.5"


class Classification(unittest.TestCase):
    def test_a_busy_pipe_is_worth_another_attempt(self):
        self.assertTrue(smb_tds._is_transient(pipe_unavailable()))
        self.assertTrue(smb_tds._is_transient(
            smb3.SessionError(nt_errors.STATUS_PIPE_BUSY)))

    def test_a_credential_failure_is_not(self):
        """The lockout guarantee. Four attempts at a wrong password is an outage, not a
        recovery."""
        self.assertFalse(smb_tds._is_transient(logon_failure()))
        self.assertFalse(smb_tds._is_transient(
            smb3.SessionError(nt_errors.STATUS_ACCOUNT_LOCKED_OUT)))

    def test_a_socket_failure_needs_no_status_to_be_retryable(self):
        """It happened before authentication, so it cannot be a credential problem."""
        self.assertTrue(smb_tds._is_transient(ConnectionResetError("reset")))
        self.assertTrue(smb_tds._is_transient(TimeoutError("timed out")))

    def test_an_unrecognised_server_status_is_left_alone(self):
        self.assertFalse(smb_tds._is_transient(
            smb3.SessionError(nt_errors.STATUS_ACCESS_DENIED)))


class Reconnecting(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(smb_tds.time, "sleep")
        self.sleep = patch.start()
        self.addCleanup(patch.stop)
        # The retry notice goes to stderr for a human watching a cron log; a test run is
        # not that human.
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def client(self):
        return smb_tds.SmbTds("10.0.0.5", "SRV", "svc", "pw")

    def connect_with(self, fake, **env):
        settings = {"COREYARD_SMB_CONNECT_ATTEMPTS": "4",
                    "COREYARD_SMB_CONNECT_BACKOFF": "1", **env}
        with mock.patch.object(smb_tds, "SMBConnection", return_value=fake), \
                mock.patch.object(smb_tds, "_get", lambda k, d="": settings.get(k, d)):
            return self.client().connect()

    def test_a_pipe_that_frees_up_on_the_second_try_connects(self):
        fake = FakeSMB(failures=1)
        socket = self.connect_with(fake)
        self.assertIsNotNone(socket)
        self.assertEqual(fake.opened, 1)
        self.assertEqual(self.sleep.call_count, 1)

    def test_it_gives_up_after_the_configured_number_of_attempts(self):
        fake = FakeSMB(failures=99)
        with self.assertRaises(smb3.SessionError):
            self.connect_with(fake)
        self.assertEqual(self.sleep.call_count, 3)      # 4 attempts, 3 waits

    def test_a_credential_failure_is_attempted_exactly_once(self):
        """The whole point of classifying: no lockout, and no four-second wait before
        reporting a password that was wrong the first time."""
        fake = FakeSMB(failures=99, error=logon_failure(), fail_on="login")
        with self.assertRaises(smb3.SessionError):
            self.connect_with(fake)
        self.assertEqual(self.sleep.call_count, 0)

    def test_a_half_built_connection_is_closed_before_retrying(self):
        """login and connectTree have already succeeded when openFile fails, so the session
        is live. Looping without closing it leaks one per attempt against a server whose
        pipe instances are the thing that ran out."""
        fake = FakeSMB(failures=2)
        self.connect_with(fake)
        self.assertEqual(fake.logoffs, 2)

    def test_the_backoff_grows_and_is_jittered(self):
        fake = FakeSMB(failures=3)
        self.connect_with(fake)
        waits = [call.args[0] for call in self.sleep.call_args_list]
        self.assertEqual(len(waits), 3)
        self.assertTrue(all(b > a for a, b in zip(waits, waits[1:])), waits)
        # Jitter adds up to half the base, so each wait sits inside its own band.
        for i, wait in enumerate(waits):
            base = 2 ** i
            self.assertGreaterEqual(wait, base)
            self.assertLessEqual(wait, base * 1.5)

    def test_retrying_can_be_switched_off(self):
        fake = FakeSMB(failures=1)
        with self.assertRaises(smb3.SessionError):
            self.connect_with(fake, COREYARD_SMB_CONNECT_ATTEMPTS="1")
        self.assertEqual(self.sleep.call_count, 0)

    def test_a_nonsense_setting_falls_back_instead_of_crashing(self):
        fake = FakeSMB(failures=1)
        self.connect_with(fake, COREYARD_SMB_CONNECT_ATTEMPTS="lots",
                          COREYARD_SMB_CONNECT_BACKOFF="soon")
        self.assertEqual(fake.opened, 1)
