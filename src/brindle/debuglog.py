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
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def _file_id(path: Path) -> list[int] | None:
    """The file's (device, inode): rotation renames debug.log, so it keeps them."""
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_dev, st.st_ino]


def _pending() -> tuple[list[dict], dict | None]:
    """The entries written since the last upload (at most :data:`MAX_BATCH`),
    oldest first, and the cursor after them: ``{file, offset}`` naming the file
    by its identity, so it still points at the right place after a rotation."""
    try:
        cursor = json.loads(_cursor_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cursor = None
    files = [(p, _file_id(p)) for p in (rotated_path(), log_path())]
    files = [(p, fid) for p, fid in files if fid is not None]
    start = 0
    if isinstance(cursor, dict):
        # Past the file the cursor is in (a file it doesn't name is all new).
        for i, (_, fid) in enumerate(files):
            if fid == cursor.get("file"):
                files, start = files[i:], int(cursor.get("offset") or 0)
                break
    entries, last = [], None
    for i, (path, fid) in enumerate(files):
        offset = start if i == 0 else 0
        with path.open("rb") as f:
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
    return entries, last


def upload_once() -> int:
    """Post what's pending, checking just before that the signed-in account is
    still staff; the number of entries the server took."""
    from brindle.pro import auth

    if not enabled():
        return 0
    try:
        client, store = _account()
    except Exception:  # noqa: BLE001 - no credential store: nothing to upload with
        return 0
    who = _identity(client, store)
    is_staff = staff(refresh=True)
    if is_staff is False:
        config.set_user(SETTING, False)
    if is_staff is not True or _identity(client, store) != who:
        return 0
    entries, cursor = _pending()
    if not entries:
        return 0
    try:
        status, _ = auth.authed(client, store, "POST", "/debug/logs",
                                auth.JSONBody({"client": _client_info(), "entries": entries}))
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
