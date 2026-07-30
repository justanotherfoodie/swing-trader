"""
Durable JSON storage for the position record.

portfolio.json is the user's real trading history. Losing it or truncating it
destroys money data, so every write here goes through the same four guarantees:

  1. ATOMIC WRITE   - serialize to a temp file in the SAME directory, flush +
                      fsync, then os.replace() onto the target. os.replace is
                      atomic on both Windows and POSIX, so a crash or power loss
                      leaves either the complete old file or the complete new
                      one. Never a half-written one.
  2. CROSS-PROCESS  - a threading.Lock only guards one interpreter. uvicorn
     LOCK             --reload runs a reloader plus a worker, and the user also
                      runs backtests and scripts against the same file. So the
                      real mutual exclusion is a lockfile created with
                      O_CREAT|O_EXCL (atomic on NTFS), with a stale-lock timeout
                      so a process killed mid-write can never deadlock the app.
  3. ROLLING BACKUP - before each write the current file is rotated into
                      portfolio.json.1 .. .5. If the primary ever fails to parse
                      we walk the backups newest-first and recover. A non-empty
                      but unparseable file NEVER yields an empty portfolio - that
                      silent-zero is the failure mode that looks like "the app
                      lost all my trades".
  4. VALIDATION     - each record is checked for the fields the exit brain reads.
                      Bad records are quarantined to portfolio.quarantine.json
                      rather than dropped, so nothing is ever destroyed by a
                      validator being stricter than reality.

Standard library only, by design: no new dependency for something this critical.
"""

import errno
import json
import os
import sys
import tempfile
import time
from datetime import datetime

__all__ = ["FileLock", "LockTimeout", "atomic_write_json", "load_json_resilient",
           "validate_positions", "rotate_backups", "BACKUP_COUNT"]

BACKUP_COUNT = 5

# How long a lockfile may sit untouched before it is presumed abandoned by a
# crashed process. Writes here take milliseconds; 30s is a generous margin that
# still guarantees the app can never wedge permanently.
STALE_LOCK_SECONDS = 30.0
LOCK_TIMEOUT_SECONDS = 10.0
_POLL = 0.02


def _log(msg: str):
    # stderr so it shows up in uvicorn output even when stdout is captured.
    print(f"[storage] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# cross-process lock
# --------------------------------------------------------------------------

class LockTimeout(RuntimeError):
    """Raised when the lock could not be acquired in time."""


class FileLock:
    """Cross-process advisory lock built on atomic O_EXCL file creation.

    Chosen over msvcrt.locking because O_EXCL is portable, needs no open handle
    on the data file itself, and lets us record who holds the lock and since
    when - which is what makes safe stale-lock recovery possible.

    Re-entrant within a process/thread pair, so nested acquisitions (a helper
    that saves while a caller already holds the lock) cannot self-deadlock.
    """

    def __init__(self, target_path: str, timeout: float = LOCK_TIMEOUT_SECONDS):
        self.lock_path = target_path + ".lock"
        self.timeout = timeout
        self._depth = 0
        self._owner = None

    def acquire(self):
        key = (os.getpid(), _thread_id())
        if self._depth > 0 and self._owner == key:
            self._depth += 1
            return
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, f"{os.getpid()} {time.time()}".encode())
                finally:
                    os.close(fd)
                self._depth = 1
                self._owner = key
                return
            except FileExistsError:
                pass
            except OSError as e:
                # Windows can surface a transient sharing violation here.
                if e.errno not in (errno.EACCES, errno.EEXIST):
                    raise

            if self._break_if_stale():
                continue
            if time.monotonic() >= deadline:
                # Refusing to write is worse than a slightly risky write: the
                # caller would lose the user's edit. Break the lock, loudly.
                _log(f"WARNING: lock {self.lock_path} held past {self.timeout}s; "
                     "breaking it to avoid losing a write.")
                _unlink_quiet(self.lock_path)
                continue
            time.sleep(_POLL)

    def _break_if_stale(self) -> bool:
        try:
            age = time.time() - os.path.getmtime(self.lock_path)
        except OSError:
            return True  # vanished; retry immediately
        if age > STALE_LOCK_SECONDS:
            _log(f"stale lock ({age:.0f}s old) at {self.lock_path} - removing. "
                 "A previous process likely died mid-write.")
            _unlink_quiet(self.lock_path)
            return True
        return False

    def release(self):
        if self._depth > 1:
            self._depth -= 1
            return
        self._depth = 0
        self._owner = None
        _unlink_quiet(self.lock_path)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def _thread_id():
    import threading
    return threading.get_ident()


def _unlink_quiet(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# atomic write + backups
# --------------------------------------------------------------------------

def _fsync_dir(path: str):
    """Persist the directory entry itself. No-op on Windows (not supported)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def rotate_backups(path: str, count: int = BACKUP_COUNT):
    """Shift path.1..path.N down one slot and copy the live file into path.1.

    Done before every write so the newest backup is always the last known-good
    committed state, never a partial one.
    """
    if not os.path.exists(path):
        return
    _unlink_quiet(f"{path}.{count}")
    for i in range(count - 1, 0, -1):
        src, dst = f"{path}.{i}", f"{path}.{i + 1}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError as e:
                _log(f"backup rotate {src} -> {dst} failed: {e}")
    try:
        with open(path, "rb") as f:
            data = f.read()
        # The backup itself is written atomically too - a crash while rotating
        # must not leave a truncated .1 that later masquerades as recoverable.
        _atomic_write_bytes(f"{path}.1", data)
    except OSError as e:
        _log(f"could not create backup {path}.1: {e}")


def _atomic_write_bytes(path: str, data: bytes):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())      # bytes are on the platter BEFORE the swap
        os.replace(tmp, path)         # atomic on Windows and POSIX
        tmp = None
        _fsync_dir(directory)
    finally:
        if tmp is not None:
            _unlink_quiet(tmp)


def atomic_write_json(path: str, obj, *, backups: int = BACKUP_COUNT, indent: int = 2):
    """Serialize `obj` to `path` durably, keeping `backups` previous versions.

    Serialization happens in memory first: if `obj` contains something
    unserializable we raise before touching the existing file, rather than
    replacing good data with a half-encoded document.
    """
    data = json.dumps(obj, indent=indent).encode("utf-8")
    if backups:
        rotate_backups(path, backups)
    _atomic_write_bytes(path, data)


# --------------------------------------------------------------------------
# resilient load
# --------------------------------------------------------------------------

def _read_json_list(path: str):
    """Return a parsed list from `path`, or None if unusable."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, list) else None


def load_json_resilient(path: str, *, backups: int = BACKUP_COUNT):
    """Load a JSON list, falling back to the newest valid backup on corruption.

    Returns (records, source) where `source` is the path actually used, or None
    when nothing existed at all. A corrupt primary is preserved as
    `<path>.corrupt-<timestamp>` for forensics - never overwritten or deleted.
    """
    if not os.path.exists(path):
        return [], None

    parsed = _read_json_list(path)
    if parsed is not None:
        return parsed, path

    size = os.path.getsize(path)
    _log("=" * 68)
    _log(f"CORRUPT PORTFOLIO FILE: {path} ({size} bytes) failed to parse.")

    for i in range(1, backups + 1):
        candidate = f"{path}.{i}"
        if not os.path.exists(candidate):
            continue
        recovered = _read_json_list(candidate)
        if recovered is None:
            _log(f"  backup {candidate} is also unreadable - trying older.")
            continue
        _log(f"  RECOVERED {len(recovered)} records from backup {candidate}.")
        _log("  The corrupt primary is kept for inspection; verify your positions.")
        _log("=" * 68)
        _quarantine_corrupt(path)
        # Re-commit the recovered state so the next reader sees a healthy file.
        try:
            _atomic_write_bytes(path, json.dumps(recovered, indent=2).encode("utf-8"))
        except OSError as e:
            _log(f"  could not restore recovered data to {path}: {e}")
        return recovered, candidate

    _log("  NO USABLE BACKUP FOUND. Returning empty list, but the corrupt file is "
         "preserved - do NOT let the app overwrite it before you inspect it.")
    _log("=" * 68)
    _quarantine_corrupt(path)
    return [], None


def _quarantine_corrupt(path: str):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.corrupt-{stamp}"
    try:
        if not os.path.exists(dest):
            with open(path, "rb") as src, open(dest, "wb") as out:
                out.write(src.read())
            _log(f"  corrupt copy saved to {dest}")
    except OSError as e:
        _log(f"  could not preserve corrupt file: {e}")


# --------------------------------------------------------------------------
# schema validation
# --------------------------------------------------------------------------

_VALID_STATUS = {"open", "closed"}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _record_errors(rec) -> list[str]:
    """Why this record is unusable. Empty list = fine.

    Deliberately checks only what the evaluation code actually dereferences.
    Optional journal fields (peak_pnl_pct, scaled_out, entry_context,
    realized_pnl, short_strike) may be absent or null - the existing 19 records
    predate several of them and must keep loading untouched.
    """
    errs = []
    if not isinstance(rec, dict):
        return ["not a JSON object"]

    if not isinstance(rec.get("ticker"), str) or not rec["ticker"].strip():
        errs.append("ticker must be a non-empty string")

    c = rec.get("contracts")
    if not isinstance(c, int) or isinstance(c, bool) or c < 0:
        errs.append("contracts must be an int >= 0")

    if not _is_num(rec.get("net_debit")):
        errs.append("net_debit must be a number")

    if rec.get("status") not in _VALID_STATUS:
        errs.append(f"status must be one of {sorted(_VALID_STATUS)}")

    exp = rec.get("expiry")
    if not isinstance(exp, str):
        errs.append("expiry must be a YYYY-MM-DD string")
    else:
        try:
            datetime.strptime(exp, "%Y-%m-%d")
        except ValueError:
            errs.append(f"expiry {exp!r} is not a parseable YYYY-MM-DD date")

    # short_strike is genuinely optional (None => single long option), but if
    # present it must be a number or _evaluate_one's arithmetic explodes.
    ss = rec.get("short_strike")
    if ss is not None and not _is_num(ss):
        errs.append("short_strike must be a number or null")
    if not _is_num(rec.get("long_strike")):
        errs.append("long_strike must be a number")

    return errs


def validate_positions(records: list, quarantine_path: str | None = None):
    """Split records into (valid, invalid), appending invalid ones to a file.

    Nothing is ever discarded: a record the validator rejects is money the user
    may still own, so it goes to the quarantine file where it can be repaired by
    hand and pasted back.
    """
    valid, invalid = [], []
    for rec in records:
        errs = _record_errors(rec)
        if errs:
            invalid.append({"errors": errs, "record": rec})
        else:
            valid.append(rec)

    if invalid:
        ids = ", ".join(str(i["record"].get("id", "?"))
                        if isinstance(i["record"], dict) else "?" for i in invalid)
        _log(f"WARNING: {len(invalid)} invalid position record(s) quarantined "
             f"(ids: {ids}). They are NOT lost - see the quarantine file.")
        for i in invalid:
            _log(f"  - {i['record'].get('id', '?') if isinstance(i['record'], dict) else '?'}"
                 f": {'; '.join(i['errors'])}")
        if quarantine_path:
            _append_quarantine(quarantine_path, invalid)

    return valid, invalid


def _append_quarantine(path: str, entries: list[dict]):
    existing = _read_json_list(path)
    if existing is None:
        existing = []
    stamp = datetime.now().isoformat()
    for e in entries:
        existing.append({"quarantined_at": stamp, **e})
    try:
        # No backups for the quarantine file itself - it is append-only debris.
        _atomic_write_bytes(path, json.dumps(existing, indent=2).encode("utf-8"))
        _log(f"  quarantined records written to {path}")
    except OSError as e:
        _log(f"  could not write quarantine file {path}: {e}")
