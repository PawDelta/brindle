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

import json
import logging
import os
import platform
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


def enabled() -> bool:
    return config.user_settings().get(SETTING) is True


# -- scrubbing --------------------------------------------------------------------------------


def scrub(value):
    """``value`` (a string, or JSON-shaped data) with secrets redacted and the
    home directory written as ``~``."""
    if isinstance(value, str):
        from brindle import kb

        home = str(Path.home())
        text = value.replace(home, "~") if home not in ("", "/") else value
        return kb.redact(text)[:MAX_TEXT]
    if isinstance(value, dict):
        return {str(scrub(k)): scrub(v) for k, v in value.items()}
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
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        path = log_path()
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.exists() and path.stat().st_size + len(line) > MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)
        return True
    except Exception:  # noqa: BLE001 - debug logging must never break what it logs
        log.debug("brindle: couldn't write a debug log entry", exc_info=True)
        return False


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


def staff(refresh: bool = False) -> bool | None:
    """Whether the account is brindle staff: True, False (the server said no,
    or no account), or None when it can't be asked (offline). Kept for
    :data:`STAFF_TTL_S`."""
    from brindle.pro import auth

    now = time.time()
    if not refresh and _staff and now - _staff["at"] < STAFF_TTL_S:
        return _staff["value"]
    try:
        client, store = _account()
        status, body = auth.authed(client, store, "GET", "/me")
    except auth.TransportError:
        return None
    except Exception:  # noqa: BLE001 - not signed in, or no credential store: not staff
        status, body = 401, {}
    value = None if status >= 500 else (status == 200 and body.get("staff") is True)
    _staff.update(value=value, at=now)
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


def _offset_path() -> Path:
    return _dir() / "uploaded.offset"


def _pending() -> tuple[list[dict], int]:
    """The entries written since the last upload (at most :data:`MAX_BATCH`) and
    the offset after them. A rotated log starts again from its top."""
    path = log_path()
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0
    try:
        offset = int(_offset_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        offset = 0
    if offset > size:
        offset = 0
    entries = []
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
    return entries, offset


def upload_once() -> int:
    """Post what's pending; the number of entries the server took."""
    from brindle.pro import auth

    if not checked()["enabled"]:
        return 0
    entries, offset = _pending()
    if not entries:
        return 0
    try:
        client, store = _account()
        status, _ = auth.authed(client, store, "POST", "/debug/logs",
                                auth.JSONBody({"client": _client_info(), "entries": entries}))
    except Exception:  # noqa: BLE001 - offline or air-gapped: try at the next interval
        log.debug("brindle: debug log upload failed", exc_info=True)
        return 0
    if not 200 <= status < 300:
        log.debug("brindle: debug log upload refused (HTTP %s)", status)
        return 0
    _offset_path().write_text(str(offset), encoding="utf-8")
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
