"""Shared resilience primitives for the market-data layer.

Why this exists
---------------
Every data-layer function in this package must keep its existing return type
(``pd.DataFrame`` / ``list[dict]`` / ``dict`` / ``float | None``) because other
modules call them and cannot be changed. That means a failure and a genuine
"nothing here" look identical to the caller.

This module fixes that *out of band*: fetch functions keep returning the same
shapes, but they also record structured health information here. Callers (the
API layer, the scanner, a /health endpoint) can consult:

    get_data_health()      -> dict snapshot of every data source
    get_last_error()       -> last structured error (any source, or one source)
    is_degraded()          -> bool, True if any source is currently unhealthy
    reset_health()         -> clear counters (tests / manual reset)

Nothing here raises into caller code paths.

Design constraints
------------------
* A full scan touches ~900 tickers. Retries must be cheap and must NOT be
  applied to genuinely-missing/delisted tickers. A circuit breaker caps the
  total added latency when the upstream feed is broadly down or rate-limiting:
  once the breaker opens, retries are skipped entirely until the cooldown
  expires, so a total outage costs ~1 attempt per ticker rather than N.
* Standard library only.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------

#: Failure is temporary — network blip, timeout, rate limit, 5xx. Worth a retry.
TRANSIENT = "transient"
#: Failure is permanent for this symbol — delisted, unknown ticker, no prices.
MISSING = "missing"
#: Could not be classified. Treated as non-retryable but logged loudly.
UNKNOWN = "unknown"

# Substrings that identify a transient upstream condition when the exception
# type itself is not specific enough (yfinance wraps a lot of things).
_TRANSIENT_MARKERS = (
    "rate limit",
    "too many requests",
    "429",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "connection error",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "500 server error",
    "502",
    "503",
    "504",
    "remote end closed",
    "max retries exceeded",
    "ssl",
    "unable to connect",
)

# Substrings that identify a permanently-missing symbol. Never retried.
_MISSING_MARKERS = (
    "no data found",
    "no price data found",
    "may be delisted",
    "delisted",
    "symbol may be delisted",
    "not found",
    "404",
    "no timezone found",
    "invalid ticker",
)


def _exception_types() -> tuple[tuple[type, ...], tuple[type, ...]]:
    """Resolve concrete exception classes lazily (imports are optional)."""
    transient: list[type] = [TimeoutError, ConnectionError, OSError]
    missing: list[type] = []

    try:  # requests is a hard dep, but stay defensive
        import requests.exceptions as rexc

        transient.extend(
            [
                rexc.Timeout,
                rexc.ConnectionError,
                rexc.ChunkedEncodingError,
                rexc.TooManyRedirects,
            ]
        )
    except Exception:  # pragma: no cover - requests always present in prod
        pass

    try:
        import yfinance.exceptions as yexc

        for name in ("YFRateLimitError",):
            cls = getattr(yexc, name, None)
            if isinstance(cls, type):
                transient.append(cls)
        for name in (
            "YFPricesMissingError",
            "YFTickerMissingError",
            "YFTzMissingError",
            "YFInvalidPeriodError",
        ):
            cls = getattr(yexc, name, None)
            if isinstance(cls, type):
                missing.append(cls)
    except Exception:
        pass

    return tuple(transient), tuple(missing)


_TRANSIENT_TYPES, _MISSING_TYPES = _exception_types()


def classify_error(exc: BaseException) -> str:
    """Return TRANSIENT / MISSING / UNKNOWN for an exception.

    Type-based checks first (precise), then message sniffing (yfinance often
    raises plain ``Exception`` with a descriptive string).
    """
    # MISSING types win over TRANSIENT types: several yfinance "missing"
    # exceptions subclass generic errors.
    if _MISSING_TYPES and isinstance(exc, _MISSING_TYPES):
        return MISSING

    msg = str(exc).lower()

    # An HTTP status is the most reliable signal — check it before any type or
    # message heuristic (requests' HTTPError subclasses OSError, so a 403 would
    # otherwise be misread as a transient socket error and retried pointlessly).
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        if status == 429 or status >= 500:
            return TRANSIENT
        if 400 <= status < 500:
            # 403 from Wikipedia is a *policy* block, not a blip: do not retry.
            return MISSING

    if isinstance(exc, _TRANSIENT_TYPES):
        # A requests HTTPError-ish OSError with a 404 in it is still missing.
        if any(m in msg for m in _MISSING_MARKERS):
            return MISSING
        return TRANSIENT

    if any(m in msg for m in _MISSING_MARKERS):
        return MISSING
    if any(m in msg for m in _TRANSIENT_MARKERS):
        return TRANSIENT
    return UNKNOWN


# --------------------------------------------------------------------------
# Health registry
# --------------------------------------------------------------------------


@dataclass
class SourceHealth:
    """Rolling health for one named data source (e.g. ``yfinance.ohlcv``)."""

    name: str
    ok_count: int = 0
    transient_failures: int = 0
    missing_count: int = 0
    unknown_failures: int = 0
    consecutive_failures: int = 0
    last_success_ts: float | None = None
    last_failure_ts: float | None = None
    last_error: dict[str, Any] | None = None
    degraded: bool = False

    def snapshot(self) -> dict[str, Any]:
        total = self.ok_count + self.transient_failures + self.unknown_failures
        return {
            "source": self.name,
            "ok": self.ok_count,
            "transient_failures": self.transient_failures,
            "missing": self.missing_count,
            "unknown_failures": self.unknown_failures,
            "consecutive_failures": self.consecutive_failures,
            "last_success_ts": self.last_success_ts,
            "last_failure_ts": self.last_failure_ts,
            "last_error": dict(self.last_error) if self.last_error else None,
            "degraded": self.degraded,
            "failure_rate": (
                round((self.transient_failures + self.unknown_failures) / total, 4)
                if total
                else 0.0
            ),
        }


# A source is flagged degraded after this many consecutive real failures.
DEGRADED_AFTER = 5

_lock = threading.RLock()
_sources: dict[str, SourceHealth] = {}
_last_error_global: dict[str, Any] | None = None


def _get(name: str) -> SourceHealth:
    sh = _sources.get(name)
    if sh is None:
        sh = SourceHealth(name=name)
        _sources[name] = sh
    return sh


def record_success(source: str) -> None:
    """Record a successful fetch from ``source``."""
    with _lock:
        sh = _get(source)
        sh.ok_count += 1
        sh.consecutive_failures = 0
        sh.last_success_ts = time.time()
        sh.degraded = False


def record_missing(source: str, subject: str | None = None) -> None:
    """Record a *legitimate* empty result (delisted / unknown symbol).

    This is NOT a failure — it does not degrade the source — but it is counted
    so callers can tell "500 symbols returned nothing" from "the feed is down".
    """
    with _lock:
        sh = _get(source)
        sh.missing_count += 1
        sh.consecutive_failures = 0
    logger.debug("[%s] no data for %s (treated as legitimately missing)", source, subject)


def record_failure(
    source: str,
    exc: BaseException | None = None,
    *,
    subject: str | None = None,
    kind: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Record a failed fetch. Returns the structured error dict recorded."""
    global _last_error_global
    kind = kind or (classify_error(exc) if exc is not None else UNKNOWN)
    err = {
        "source": source,
        "subject": subject,
        "kind": kind,
        "error_type": type(exc).__name__ if exc is not None else None,
        "message": (detail or (str(exc) if exc is not None else "") or "")[:500],
        "ts": time.time(),
    }
    with _lock:
        sh = _get(source)
        if kind == TRANSIENT:
            sh.transient_failures += 1
        else:
            sh.unknown_failures += 1
        sh.consecutive_failures += 1
        sh.last_failure_ts = err["ts"]
        sh.last_error = err
        if sh.consecutive_failures >= DEGRADED_AFTER:
            sh.degraded = True
        _last_error_global = err
    return err


def get_data_health() -> dict[str, Any]:
    """Snapshot of the whole data layer.

    Shape::

        {
          "healthy": bool,           # False if any source is degraded
          "degraded_sources": [str],
          "circuit_open": {name: seconds_remaining},
          "last_error": {...} | None,
          "sources": {name: {...}},
          "ts": float,
        }
    """
    with _lock:
        sources = {name: sh.snapshot() for name, sh in _sources.items()}
        degraded = [n for n, s in sources.items() if s["degraded"]]
        last = dict(_last_error_global) if _last_error_global else None
    return {
        "healthy": not degraded,
        "degraded_sources": degraded,
        "circuit_open": _open_circuits(),
        "last_error": last,
        "sources": sources,
        "ts": time.time(),
    }


def get_last_error(source: str | None = None) -> dict[str, Any] | None:
    """Last structured error overall, or for one source."""
    with _lock:
        if source is None:
            return dict(_last_error_global) if _last_error_global else None
        sh = _sources.get(source)
        return dict(sh.last_error) if sh and sh.last_error else None


def is_degraded(source: str | None = None) -> bool:
    """True when the data layer (or a named source) is currently unhealthy."""
    with _lock:
        if source is None:
            return any(sh.degraded for sh in _sources.values()) or bool(_open_circuits())
        sh = _sources.get(source)
        return bool(sh and sh.degraded) or source in _open_circuits()


def reset_health() -> None:
    """Clear all counters, errors and open circuits."""
    global _last_error_global
    with _lock:
        _sources.clear()
        _last_error_global = None
        _breakers.clear()


# --------------------------------------------------------------------------
# Circuit breaker — caps total added latency during a broad outage
# --------------------------------------------------------------------------


@dataclass
class _Breaker:
    fails: int = 0
    open_until: float = 0.0


#: Consecutive transient failures before we stop retrying for a while.
BREAKER_THRESHOLD = 8
#: How long the breaker stays open (seconds). Retries are skipped meanwhile.
BREAKER_COOLDOWN = 30.0

_breakers: dict[str, _Breaker] = {}


def _open_circuits() -> dict[str, float]:
    now = time.time()
    return {
        name: round(b.open_until - now, 1)
        for name, b in _breakers.items()
        if b.open_until > now
    }


def circuit_is_open(source: str) -> bool:
    with _lock:
        b = _breakers.get(source)
        return bool(b and b.open_until > time.time())


def _breaker_record(source: str, transient: bool) -> None:
    with _lock:
        b = _breakers.setdefault(source, _Breaker())
        if not transient:
            b.fails = 0
            return
        b.fails += 1
        if b.fails >= BREAKER_THRESHOLD and b.open_until <= time.time():
            b.open_until = time.time() + BREAKER_COOLDOWN
            b.fails = 0
            logger.error(
                "[%s] circuit breaker OPEN for %.0fs after %d consecutive transient "
                "failures — retries suspended, results may be incomplete",
                source,
                BREAKER_COOLDOWN,
                BREAKER_THRESHOLD,
            )


# --------------------------------------------------------------------------
# Retry driver
# --------------------------------------------------------------------------

#: Default attempt budget (1 initial try + 1 retry). Kept small on purpose:
#: a ~900-ticker scan cannot afford long retry ladders.
DEFAULT_ATTEMPTS = 2
DEFAULT_BASE_DELAY = 0.4
DEFAULT_MAX_DELAY = 2.0


class DataUnavailable(Exception):
    """Raised internally when a fetch failed (as opposed to returning nothing).

    Callers in this package catch it and convert to their legacy return type
    after recording health, so it never escapes the data layer.
    """

    def __init__(self, message: str, *, kind: str = UNKNOWN, cause: BaseException | None = None):
        super().__init__(message)
        self.kind = kind
        self.cause = cause


def call_with_retry(
    fn: Callable[[], T],
    *,
    source: str,
    subject: str | None = None,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    retry_kinds: Iterable[str] = (TRANSIENT,),
    deadline: float | None = None,
) -> T:
    """Call ``fn`` with bounded exponential backoff on *transient* errors only.

    * Permanently-missing symbols (delisted, unknown) are raised immediately —
      no retry, no health degradation beyond a ``missing`` counter bump by the
      caller.
    * When the circuit breaker for ``source`` is open, only a single attempt is
      made. This is what bounds total added latency during an outage.
    * ``deadline`` (seconds) caps the wall-clock time spent across attempts.

    Raises :class:`DataUnavailable` when every attempt failed.
    """
    retry_kinds = set(retry_kinds)
    started = time.monotonic()
    budget = 1 if circuit_is_open(source) else max(1, attempts)
    last_exc: BaseException | None = None
    last_kind = UNKNOWN

    for attempt in range(1, budget + 1):
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 - classified immediately below
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            last_exc = exc
            last_kind = classify_error(exc)
            _breaker_record(source, last_kind == TRANSIENT)

            if last_kind == MISSING:
                logger.debug(
                    "[%s] %s: permanently missing (%s: %s)",
                    source, subject, type(exc).__name__, exc,
                )
                raise DataUnavailable(str(exc), kind=MISSING, cause=exc) from exc

            if last_kind not in retry_kinds or attempt >= budget:
                break

            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay *= 0.5 + random.random()  # jitter, avoids thundering herd
            if deadline is not None and (time.monotonic() - started) + delay > deadline:
                logger.warning(
                    "[%s] %s: retry budget exhausted by deadline after %d attempt(s)",
                    source, subject, attempt,
                )
                break
            logger.warning(
                "[%s] %s: transient failure (%s: %s) — retry %d/%d in %.2fs",
                source, subject, type(exc).__name__, exc, attempt, budget - 1, delay,
            )
            time.sleep(delay)
            continue
        else:
            _breaker_record(source, False)
            return result

    assert last_exc is not None
    raise DataUnavailable(str(last_exc), kind=last_kind, cause=last_exc) from last_exc


# --------------------------------------------------------------------------
# Hard timeout for calls whose library gives us no timeout parameter
# --------------------------------------------------------------------------

_executor: Any = None
_executor_lock = threading.Lock()


def _get_executor():
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                from concurrent.futures import ThreadPoolExecutor

                _executor = ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="datafetch"
                )
    return _executor


def run_with_timeout(fn: Callable[[], T], timeout: float, *, what: str = "call") -> T:
    """Run ``fn`` on a worker thread and give up after ``timeout`` seconds.

    Used for yfinance endpoints (``Ticker.info``, ``fast_info``, ``news``) that
    expose no timeout parameter. The abandoned thread is a daemon and cannot
    block process shutdown; the caller gets a :class:`TimeoutError`, which
    :func:`classify_error` treats as transient.
    """
    from concurrent.futures import TimeoutError as _FTimeout

    fut = _get_executor().submit(fn)
    try:
        return fut.result(timeout=timeout)
    except _FTimeout as exc:
        fut.cancel()
        raise TimeoutError(f"{what} exceeded {timeout:.1f}s timeout") from exc
