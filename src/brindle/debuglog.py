"""Debug logs a brindle staff account can turn on, uploaded to brindle.

* :func:`record` appends one JSON line to ``$BRINDLE_HOME/debug/debug.log``
  (rotated to ``debug.log.1`` past :data:`MAX_BYTES`), only while the
  ``debug_logs`` user setting is on. Secret-shaped values are redacted and the
  home directory becomes ``~`` before anything is written. It never raises.
* The setting turns on only for an account whose ``/me`` says ``staff: true``
  (missing is false); once the account isn't staff any more it is turned off.
* :class:`Uploader` runs in the daemon and posts what was written since its
  last upload to ``POST /debug/logs`` on the account's brindle Pro base URL,
  every :data:`INTERVAL_S`, with the account's auth.

Sidebar call site (watch.py, where it logs a caught draw error or a restart):
``debuglog.record("sidebar", "error", str(e), {"traceback": traceback.format_exc()})``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import platform
import re
import threading
import time
import uuid
from pathlib import Path

import brindle
from brindle import config

log = logging.getLogger(__name__)

SETTING = "debug_logs"          # ~/.brindle/config.json: True while debug logs are on
MAX_BYTES = 1024 * 1024         # debug.log is rotated past this (one old file is kept)
MAX_TEXT = 2000                 # a message or detail string is cut to this
MAX_ENTRY = 32 * 1024           # an entry's encoded size; a bigger detail is replaced by a marker
MAX_BATCH = 500                 # entries per upload
INTERVAL_S = 180.0
STAFF_TTL_S = 300.0
LEVELS = ("debug", "info", "warning", "error")

_lock = threading.Lock()
_staff: dict = {}               # {"value": bool | None, "at": float}
_app_version: str | None = None


def _dir() -> Path:
    return config.brindle_home() / "debug"


def log_path() -> Path:
    return _dir() / "debug.log"


def rotated_path() -> Path:
    return _dir() / "debug.log.1"


def enabled() -> bool:
    return config.user_settings().get(SETTING) is True


# -- scrubbing --------------------------------------------------------------------------------


_SENSITIVE_KEY = re.compile(r"(?i)token|secret|passw|authoriz|credential|api[_-]?key|cookie|"
                            r"session|private[_-]?key|signature")
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic|token)(\s+)[A-Za-z0-9._~+/=-]{8,}")


def scrub(value):
    """``value`` (a string, or JSON-shaped data) with secrets redacted and the
    home directory written as ``~``: secret-shaped text, an ``Authorization``
    style credential, and anything under a key that names a secret."""
    if isinstance(value, str):
        from brindle import kb

        home = str(Path.home())
        text = value.replace(home, "~") if home not in ("", "/") else value
        text = _AUTH_HEADER.sub(lambda m: m.group(1) + m.group(2) + kb.REDACTED, text)
        return kb.redact(text)[:MAX_TEXT]
    if isinstance(value, dict):
        from brindle import kb

        return {str(scrub(k)): kb.REDACTED if _SENSITIVE_KEY.search(str(k)) else scrub(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(str(value))


# -- writing ----------------------------------------------------------------------------------


def record(source: str, level: str, message: str, detail=None) -> bool:
    """Append one entry while debug logs are on; True if it was written."""
    try:
        if not enabled():
            return False
        entry = {"ts": time.time(), "source": scrub(str(source))[:64],
                 "level": level if level in LEVELS else "info", "message": scrub(str(message)),
                 "detail": scrub(detail)}
        line = _encode(entry)
        if len(line) > MAX_ENTRY:   # a detail too big to keep: say so instead
            entry["detail"] = {"truncated": True, "bytes": len(line)}
            line = _encode(entry)
        path = log_path()
        with _locked():
            if path.exists() and path.stat().st_size + len(line) > MAX_BYTES:
                os.replace(path, rotated_path())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        return True
    except Exception:  # noqa: BLE001 - debug logging must never break what it logs
        log.debug("brindle: couldn't write a debug log entry", exc_info=True)
        return False


@contextlib.contextmanager
def _locked():
    """Held around a rotation or a write, and while the uploader opens the
    files, in every process (the sidebar writes from its own)."""
    _dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock:
        fd = os.open(_dir() / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)     # closing releases the lock


def _encode(entry: dict) -> bytes:
    return (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")


def note_app_version(version) -> None:
    """The desktop app's version, as it reports it, for the uploads' ``client``."""
    global _app_version
    if isinstance(version, str) and version:
        _app_version = version[:64]


# -- the account --------------------------------------------------------------------------------


def _account():
    """(client, store) for the signed-in brindle Pro account."""
    from brindle.pro import auth, credentials

    store = credentials.default_store()
    base = (store.load() or {}).get("base_url")
    return auth.Client(base), store


def _identity(client, store) -> str:
    """Which signed-in account (and backend) the credentials are: a staff answer
    is only ever reused for the same one."""
    try:
        creds = store.load() or {}
    except Exception:  # noqa: BLE001 - no credential store: no account
        creds = {}
    who = creds.get("entitlement") or creds.get("refresh_token") or ""
    return hashlib.sha256(f"{client.base}\0{who}".encode()).hexdigest()


def staff(refresh: bool = False) -> bool | None:
    """Whether the signed-in account is brindle staff: True, False (the server
    said no, or no account), or None when it can't be asked (offline). Kept for
    :data:`STAFF_TTL_S` per account."""
    from brindle.pro import auth

    now = time.time()
    try:
        client, store = _account()
    except Exception:  # noqa: BLE001 - no credential store: not staff
        return False
    who = _identity(client, store)
    got = _staff.get(who)
    if not refresh and got and now - got[1] < STAFF_TTL_S:
        return got[0]
    try:
        status, body = auth.authed(client, store, "GET", "/me")
    except auth.TransportError:
        return None
    except Exception:  # noqa: BLE001 - not signed in: not staff
        status, body = 401, {}
    value = None if status >= 500 else (status == 200 and body.get("staff") is True)
    _staff.clear()      # only the current account's answer is kept
    _staff[who] = (value, now)
    return value


def checked() -> dict:
    """``{enabled, staff}``, turning debug logs off for an account that isn't staff."""
    is_staff = staff()
    on = enabled()
    if on and is_staff is False:
        config.set_user(SETTING, False)
        on = False
    return {"enabled": on, "staff": is_staff is True}


def set_enabled(on: bool) -> dict:
    """Turn debug logs on (staff only: PermissionError otherwise) or off."""
    if on and staff(refresh=True) is not True:
        raise PermissionError("debug logs are only for brindle staff accounts")
    config.set_user(SETTING, bool(on))
    return checked()


# -- uploading ----------------------------------------------------------------------------------


def install_id() -> str:
    path = _dir() / "install_id"
    try:
        got = path.read_text(encoding="utf-8").strip()
        if got:
            return got
    except OSError:
        pass
    new = uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(new, encoding="utf-8")
    return new


def _client_info() -> dict:
    return {"install_id": install_id(), "app_version": _app_version,
            "engine_version": brindle.__version__, "os": f"{platform.system()} {platform.release()}"}


def _cursor_path() -> Path:
    return _dir() / "uploaded.json"


def _open_logs() -> list:
    """debug.log.1 and debug.log, oldest first, as (open file, [device, inode]),
    opened together under the lock so no rotation falls between them. Each is
    identified by its open descriptor, so a rotation after this can't change
    which file an offset belongs to (rotation renames, so the inode moves with it)."""
    files = []
    with _locked():
        for path in (rotated_path(), log_path()):
            try:
                f = path.open("rb")
            except OSError:
                continue
            st = os.fstat(f.fileno())
            files.append((f, [st.st_dev, st.st_ino]))
    return files


def _pending() -> tuple[list[dict], dict | None]:
    """The entries written since the last upload (at most :data:`MAX_BATCH`),
    oldest first, and the cursor after them: ``{file, offset}`` naming the file
    by its identity, so it still points at the right place after a rotation."""
    try:
        cursor = json.loads(_cursor_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cursor = None
    opened = _open_logs()
    files, start = opened, 0
    if isinstance(cursor, dict):
        # Past the file the cursor is in (a file it doesn't name is all new).
        for i, (_, fid) in enumerate(opened):
            if fid == cursor.get("file"):
                files, start = opened[i:], int(cursor.get("offset") or 0)
                break
    entries, last = [], None
    try:
        for i, (f, fid) in enumerate(files):
            offset = start if i == 0 else 0
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n") or len(entries) >= MAX_BATCH:
                    break
                offset += len(raw)
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
            last = {"file": fid, "offset": offset}
            if len(entries) >= MAX_BATCH:
                break
    finally:
        for f, _ in opened:
            f.close()
    return entries, last


def upload_once() -> int:
    """Post what's pending as an account checked to be staff; the number of
    entries the server took.

    The staff check and the post use one snapshot of the credentials (one
    access token, one backend), never the store again: an account switch or a
    token refresh in between can't send the batch as an account that wasn't
    checked. ``staff`` first refreshes the token in the store if it's due."""
    from brindle.pro import auth

    if not enabled():
        return 0
    is_staff = staff(refresh=True)
    if is_staff is False:
        config.set_user(SETTING, False)
    if is_staff is not True:
        return 0
    try:
        client, store = _account()
        snap = store.load() or {}
        if snap.get("base_url") and auth.base_url(snap["base_url"]) != client.base:
            return 0
        token = snap.get("access_token")
        if not token:
            return 0
        status, me = client.call("GET", "/me", token=token)
        if status != 200 or me.get("staff") is not True:
            if status == 200:
                config.set_user(SETTING, False)
            return 0
        entries, cursor = _pending()
        if not entries:
            return 0
        status, _ = client.call("POST", "/debug/logs",
                                auth.JSONBody({"client": _client_info(), "entries": entries}),
                                token=token)
    except Exception:  # noqa: BLE001 - offline or air-gapped: try at the next interval
        log.debug("brindle: debug log upload failed", exc_info=True)
        return 0
    if not 200 <= status < 300:
        log.debug("brindle: debug log upload refused (HTTP %s)", status)
        return 0
    _cursor_path().write_text(json.dumps(cursor), encoding="utf-8")
    return len(entries)


class Uploader:
    """The daemon's background upload, every :data:`INTERVAL_S`."""

    def __init__(self, interval: float = INTERVAL_S) -> None:
        self.interval = interval
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="brindle-debuglog", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                upload_once()
            except Exception:  # noqa: BLE001 - the next interval tries again
                log.debug("brindle: debug log upload pass failed", exc_info=True)
