"""Inference through a locally-installed AI CLI, rather than a metered API.

This is a *transport*, in the same sense as ``yms/db.py`` (TDS over a named pipe) and
``sink/shopify_api.py`` (HTTPS): it owns one way of reaching an outside system and knows
nothing about what the caller wants from it. It lives at the top level rather than inside
the channel that first needed it, because ``doctor`` has to report whether inference is
available on a broken installation, and it must be able to do that without importing a
whole publishing channel — and its dependencies — to find out.

Two properties of this transport shape every caller:

  * **One invocation carries roughly 13k tokens of system-prompt overhead.** Sending forty
    rows in one call and sending forty calls of one row differ by more than an order of
    magnitude in cost and wall time. Callers batch; :func:`batches` is here for that.
  * **A rate-limited call fails instantly and uninformatively** — no exception, no message,
    just a result envelope with ``is_error`` set, no turns taken and no tokens spent. That
    exact shape is what :func:`_looks_rate_limited` recognises, because nothing else
    distinguishes it from a genuine failure.

There is no provider SDK here and no API key. The binary is whatever ``COREYARD_AI_BIN``
names, and it authenticates itself. CoreYard therefore cannot pin its version: a flag
renamed upstream surfaces here as an :class:`AIError`, which is why ``doctor`` reports the
binary's presence and why every caller must survive inference being unavailable.

Nothing in this module is required by the Shopify pipeline. An installation that never sets
``COREYARD_AI_ENABLED`` never reaches it.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import time

from coreyard.config import _get, flag

# The binary is a CLI alias ("sonnet"), not a pinned model id: the CLI resolves it to
# whatever the current model of that name is, which is what a subscription-backed tool
# should do. Pinning here would mean editing CoreYard every time the alias moves.
DEFAULT_BIN = "claude"
DEFAULT_MODEL = "sonnet"

# Generous: a batch of forty titles with a web search behind it is not a fast call.
DEFAULT_TIMEOUT = 900

# Rate limits reset over minutes, so the first backoff is deliberately long. Retrying in
# seconds does nothing but burn an attempt against the same closed window.
DEFAULT_RETRIES = 5
BASE_DELAY = 60.0

# Jitter, so several batches that hit the limit together do not all wake at once.
_JITTER = 15.0


class AIError(RuntimeError):
    """Inference did not produce a usable answer."""


class RateLimited(AIError):
    """The subscription's rate limit was hit and outlasted the retry budget."""


class Unavailable(AIError):
    """Inference is switched off, or the binary is not installed."""


def enabled() -> bool:
    """Whether this installation has opted in to inference.

    An explicit gate rather than "is the binary present", so a scheduled job on a host
    where the CLI was never set up fails immediately and says so, instead of discovering it
    thirty minutes into a backoff with a checkpoint half-written.
    """
    return flag("COREYARD_AI_ENABLED", False)


def binary() -> str:
    return (_get("COREYARD_AI_BIN", DEFAULT_BIN) or DEFAULT_BIN).strip()


def installed() -> bool:
    """Whether the configured binary is actually on PATH. Read-only; used by ``doctor``."""
    return shutil.which(binary()) is not None


def model() -> str:
    return (_get("COREYARD_AI_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL).strip()


def _int(key: str, default: int) -> int:
    raw = (_get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise AIError(f"{key} must be a whole number of seconds, not {raw!r}") from exc


def call_timeout() -> int:
    return _int("COREYARD_AI_TIMEOUT", DEFAULT_TIMEOUT)


def deadline_now() -> float | None:
    """An absolute :func:`time.monotonic` deadline for this run, or None for unbounded.

    Derived from ``COREYARD_AI_DEADLINE`` and computed *once*, at the start of a run, so a
    loop of many calls shares one budget rather than granting each call a fresh one.
    """
    budget = _int("COREYARD_AI_DEADLINE", 0)
    return time.monotonic() + budget if budget > 0 else None


def batches(seq, size: int):
    """Yield ``seq`` in lists of at most ``size``.

    Lives beside the transport rather than in a general-purpose utility module because the
    reason to batch is a property of the transport — see this module's docstring — and a
    caller reaching for it should land on that explanation.
    """
    if size < 1:
        raise ValueError("batch size must be at least 1")
    items = list(seq)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _looks_rate_limited(env: dict) -> bool:
    """Whether this envelope is the CLI's silent "rate limited" shape.

    A limited call reports an error having taken no API time, no turns and no tokens. Any
    one of those could occur innocently; together they occur for nothing else.
    """
    usage = env.get("usage") or {}
    spent = (
        usage.get("input_tokens", 0)
        + usage.get("output_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    return (
        bool(env.get("is_error"))
        and env.get("duration_api_ms", 0) == 0
        and env.get("num_turns", 0) <= 1
        and spent == 0
    )


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise Unavailable(
            f"{cmd[0]!r} is not installed or not on PATH; set COREYARD_AI_BIN "
            f"or turn COREYARD_AI_ENABLED off"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AIError(f"inference timed out after {timeout}s") from exc
    return proc.returncode, proc.stdout, proc.stderr


def ask_json(
    prompt: str,
    schema: dict,
    *,
    model_name: str | None = None,
    system: str | None = None,
    allow_web: bool = False,
    timeout: int | None = None,
    retries: int = DEFAULT_RETRIES,
    base_delay: float = BASE_DELAY,
    deadline: float | None = None,
    log=None,
):
    """Ask for one JSON answer matching ``schema``; return ``(answer, usage)``.

    ``deadline`` is an absolute :func:`time.monotonic` value — from :func:`deadline_now`,
    or from the caller's own ``--timeout`` — past which no further backoff is started. It
    exists because the retry budget used to be counted in attempts alone: five retries at a
    doubling sixty-second base is about half an hour, so a call could consume most of a
    scheduled run's window and then die *inside* ``time.sleep``, banking nothing. Stopping
    at the deadline instead lets the caller write its checkpoint and exit cleanly, which is
    the difference between a run that made partial progress and a run that made none.
    """
    if not enabled():
        raise Unavailable(
            "inference is off; set COREYARD_AI_ENABLED=true to use the AI-backed commands"
        )

    limit = call_timeout() if timeout is None else timeout
    cmd = [
        binary(), "-p", prompt,
        "--model", model_name or model(),
        "--output-format", "json",
        "--json-schema", json.dumps(schema, separators=(",", ":")),
    ]
    if system:
        cmd += ["--append-system-prompt", system]
    if allow_web:
        cmd += ["--allowedTools", "WebSearch", "WebFetch"]

    last = ""
    for attempt in range(retries + 1):
        code, stdout, stderr = _run(cmd, limit)
        env: dict = {}
        if stdout.strip():
            try:
                env = json.loads(stdout)
            except json.JSONDecodeError:
                env = {}

        if code == 0 and env and not env.get("is_error"):
            answer = env.get("structured_output")
            if answer is not None:
                return answer, env.get("usage", {})
            last = f"no structured output (stop_reason={env.get('stop_reason')})"
        elif env and _looks_rate_limited(env):
            last = "rate limited"
            delay = base_delay * (2 ** attempt) + random.uniform(0, _JITTER)
            if not _may_wait(attempt, retries, delay, deadline):
                break
            if log:
                log(f"    rate limited; waiting {delay:.0f}s "
                    f"(attempt {attempt + 1}/{retries})")
            time.sleep(delay)
            continue
        else:
            detail = str(env.get("result") or stderr or stdout)[:600]
            last = f"exit {code}: {detail}"
            # A call that did real work and then failed — ran out of turns mid-search, say
            # — is worth one cheap retry. It is not worth a rate-limit backoff, because
            # nothing suggests the window is closed.
            if env.get("num_turns", 0) > 1 and _may_wait(attempt, retries, 10, deadline):
                if log:
                    log(f"    failed after {env.get('num_turns')} turns; retrying")
                time.sleep(10)
                continue

        if attempt >= retries:
            break

    if last == "rate limited":
        raise RateLimited(f"rate limited after {retries + 1} attempt(s)")
    raise AIError(f"inference failed after {retries + 1} attempt(s) — {last}")


def _may_wait(attempt: int, retries: int, delay: float, deadline: float | None) -> bool:
    """Whether there is both an attempt and enough time left to wait ``delay`` seconds."""
    if attempt >= retries:
        return False
    return deadline is None or time.monotonic() + delay < deadline
