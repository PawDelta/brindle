"""Connect GitHub: brindle's own sign-in to github.com.

A person connects once, either with a copy of their GitHub CLI sign-in (taken at
their click, the one moment gh's keychain item is read) or a fine-grained token
they paste. brindle keeps that credential itself, and git signs in to
``https://github.com`` through brindle's credential helper alone (:func:`answer`,
wired in by :mod:`brindle.git`): in the fetches brindle starts on its own, a clone
or push it runs for the person, and agents' panes. git's keychain helper and gh's
keychain item are never asked on those paths, so no dialog can come of them.

Where the token is kept (``storage`` in :func:`status`):

* ``keychain``: under the desktop app, whose daemon runs as the engine's signed
  helper app. The token is a keychain item only that program made and reads
  (brindle.mac_keychain), so macOS asks before giving it to anything else
  (``security``, another app, a script on brindle's own python3). Every other
  brindle process (the CLI, a hook, git's helper) asks the daemon. Reads never
  show a dialog: a locked keychain just means no answer, until it is unlocked.
  Only connecting, which the person clicked, may ask them to unlock it.
* ``file``: everywhere else (a CLI install, a build without the helper, a daemon
  that isn't the helper), and by choice (``brindle github connect --file``, or
  ``BRINDLE_GITHUB_STORE=file``): a 0600 file in a 0700 folder under brindle's
  home, which no dialog can stand in front of.

Beside it, in that folder, is what isn't secret: who is connected, how, and until
when. So "is there a connection" never touches the keychain.

What none of this does is keep the credential from agents. They run as the
person: a command brindle's rules don't recognise can read the file, or ask the
daemon the way git's helper in their own pane must be able to. The rules are not
a sandbox. What a taken credential is worth is set by its kind: a fine-grained
token reaches the repositories and permissions it was made for until it expires;
a copy of gh's sign-in reaches everything gh does and doesn't expire.

Nothing here logs, prints or raises a token.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import http.client
import json
import os
import shlex
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from brindle import keystore, mac_keychain, spawn

SERVICE = "brindle-github"
ITEM_ACCOUNT = "connection"
# What git must be asking about for the helper to answer: never another host or scheme,
# whatever a repo's config has git ask.
ORIGIN = ("https", "github.com")
API = "https://api.github.com"
ROUTES = ("gh", "token")
STORE_ENV = "BRINDLE_GITHUB_STORE"
REFUSED_FILE = "refused"
CONNECTION_PATH = "/v1/github/connection"
CREDENTIAL_PATH = "/v1/github/credential"     # git's helper asking the daemon: see daemon.auth
HELPER_APP = "Library/LoginItems/brindle.app"   # in the app's Contents, beside the engine
MAX_TOKEN = 255
HTTP_SECONDS = 15.0
GH_COPY_SECONDS = 60.0       # the keychain may ask the person to unlock it: they clicked, so it may wait for them
ASK_DAEMON_SECONDS = 10.0
CONNECT_SECONDS = 90.0       # a connect sent on to the daemon, which may be waiting at that dialog
_EXPIRY_FORMATS = ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z")


class ConnectError(Exception):
    """Connecting didn't work. ``code`` is the service error's; the message never carries a token."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# -- which program this is --------------------------------------------------------------------------


@functools.lru_cache
def is_helper() -> bool:
    """Whether this process is the engine's signed helper app (the app's daemon): the one
    program that makes and reads the keychain item. ``sys.executable`` says python3 there
    too, so the process's own image is asked."""
    if sys.platform != "darwin":
        return False
    size = ctypes.c_uint32(4096)
    image = ctypes.create_string_buffer(size.value)
    if ctypes.CDLL(None)._NSGetExecutablePath(image, ctypes.byref(size)) != 0:
        return False
    return os.path.realpath(image.value.decode()).endswith(f"/{HELPER_APP}/Contents/MacOS/brindle")


def helper_installed() -> bool:
    """Whether this engine is the desktop app's, with its helper app: connections made
    here belong in that helper's keychain item, so they go through the daemon."""
    parents = Path(sys.executable).resolve().parents    # <app>/Contents/Resources/engine/python/bin/python3
    return (sys.platform == "darwin" and len(parents) > 4 and parents[4].name == "Contents"
            and (parents[4] / HELPER_APP).is_dir())


def _file_chosen(file: bool) -> bool:
    return file or os.environ.get(STORE_ENV, "").strip().lower() == "file"


# -- the store ----------------------------------------------------------------------------------


def _dir() -> Path:
    from brindle.config import brindle_home

    return brindle_home() / SERVICE


def _store():
    """What isn't secret about the connection, and with ``file`` storage the token too."""
    return keystore.FileStore(_dir(), "connection")


def _item() -> mac_keychain.Item:
    return mac_keychain.Item(SERVICE, ITEM_ACCOUNT)


def store_paths() -> list[str]:
    """What holds the credential on disk, for the lists of what agents may not read."""
    return [str(_dir())]


def _load() -> dict | None:
    try:
        record = _store().load()
    except (keystore.CredentialError, OSError):
        return None     # a store that can't be trusted is no connection
    return record if record and record.get("route") in ROUTES else None


def _refused() -> bool:
    return (_dir() / REFUSED_FILE).exists()


def note_refused() -> None:
    """The credential is no use any more (GitHub turned it down, or its keychain item is
    gone or no longer this program's): it isn't offered again until the person connects
    again, so no poll keeps trying it."""
    with contextlib.suppress(OSError):
        (_dir() / REFUSED_FILE).touch()


def _expired(record: dict) -> bool:
    expires = record.get("expires_at")
    return isinstance(expires, (int, float)) and expires <= time.time()


def _token(record: dict) -> str | None:
    """The connection's token, for the process that holds it. From the keychain it is read
    without any dialog: locked means none for now; gone or refused means connect again."""
    if record.get("storage") != "keychain":
        return record.get("token")
    if not is_helper():
        return None
    try:
        secret = _item().load()
    except mac_keychain.Refused as e:
        if not e.locked:
            note_refused()
        return None
    if secret is None:
        note_refused()
        return None
    return secret.decode()


def offered() -> bool:
    """Whether there is a connection for git to try: one GitHub hasn't turned down and
    that hasn't run out. Reads nothing secret."""
    record = _load()
    return record is not None and not _refused() and not _expired(record)


def credential() -> tuple[str, str] | None:
    """(username, password) for git on github.com, in the process that holds the token."""
    record = _load()
    if record is None or _refused() or _expired(record):
        return None
    token = _token(record)
    return (record.get("login") or "x-access-token", token) if token else None


# -- what the app, the CLI and doctor show ---------------------------------------------------------


def gh_available() -> bool:
    """Whether a copy of gh's sign-in can be offered: gh is installed and signed in to
    github.com. Asks gh's config only, which reads no keychain item."""
    from brindle import git

    return git.gh_credential_helper() is not None


def status() -> dict:
    """The connection as GET /v1/github/connection gives it. Never the token, and no
    keychain read."""
    record = _load()
    return {
        "connected": record is not None,
        "login": record.get("login") if record else None,
        "route": record["route"] if record else None,
        "storage": (record.get("storage") or "file") if record else None,
        "expires_at": record.get("expires_at") if record else None,
        "needs_reconnect": record is not None and (_refused() or _expired(record)),
        "gh_available": gh_available(),
    }


def describe(state: dict) -> str:
    """``status()`` in a sentence, for `brindle github status` and doctor."""
    if not state["connected"]:
        return "not connected: git uses your own credentials" + (
            " (`brindle github connect` can copy your GitHub CLI sign-in)" if state["gh_available"]
            else " (`brindle github connect --token` gives brindle its own sign-in)")
    how = "a copy of your GitHub CLI sign-in" if state["route"] == "gh" else "a token you pasted"
    where = "brindle's keychain item" if state["storage"] == "keychain" else "a 0600 file"
    line = f"connected as {state['login']} with {how}, kept in {where}"
    if state["expires_at"]:
        line += f", expires {time.strftime('%Y-%m-%d', time.localtime(state['expires_at']))}"
    return line + ("; it no longer works: connect again" if state["needs_reconnect"] else "")


# -- asking the daemon ----------------------------------------------------------------------------


def _daemon(method: str, body: dict | None = None) -> dict:
    """``method`` on the daemon's connection route, as the person (its token): for a CLI
    under the desktop app, whose connection lives in the daemon's keychain item."""
    from brindle.daemon import holds, lifecycle

    closed = ConnectError("daemon_unavailable", "under the desktop app brindle keeps this sign-in in its keychain "
                          "item, which only the running app can reach: open brindle and try again, or pass --file "
                          "to keep it in a 0600 file instead")
    token = (lifecycle.read_info() or {}).get("token")
    if not token:
        raise closed
    conn = holds._UnixConnection(str(lifecycle.socket_path()), CONNECT_SECONDS)
    try:
        conn.request(method, CONNECTION_PATH, json.dumps(body or {}),
                     {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
    except (OSError, ValueError, http.client.HTTPException):
        raise closed from None
    finally:
        conn.close()
    if not isinstance(data, dict):
        raise closed
    if resp.status != 200:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        raise ConnectError(str(error.get("code") or "internal"), str(error.get("message") or "brindle refused"))
    return data


def _through_daemon(file: bool) -> bool:
    """Whether a connect made in this process belongs to the daemon instead."""
    return helper_installed() and not is_helper() and not _file_chosen(file)


# -- connecting ---------------------------------------------------------------------------------


def _http(url: str, token: str) -> tuple[int, dict[str, str], dict]:
    """GET ``url`` as ``token``: (status, headers, JSON body). ConnectError when GitHub
    can't be reached, or air-gap mode forbids asking."""
    from brindle import airgap

    try:
        airgap.guard(url, "GitHub sign-in check")
    except airgap.AirGapError as e:
        raise ConnectError("github_unreachable", str(e)) from None
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "brindle"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_SECONDS, context=ssl.create_default_context()) as resp:
            status_code, headers, raw = resp.status, resp.headers, resp.read(keystore.MAX_BLOB)
    except urllib.error.HTTPError as e:
        status_code, headers, raw = e.code, e.headers, b""
    except (urllib.error.URLError, OSError, ValueError):
        raise ConnectError("github_unreachable", "couldn't reach GitHub to check the sign-in") from None
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    return status_code, {k.lower(): v for k, v in (headers or {}).items()}, body if isinstance(body, dict) else {}


def _expiry(header: str | None) -> float | None:
    """When GitHub says the token expires (its ``github-authentication-token-expiration``
    header), as a timestamp; None for one that doesn't, or a date it words another way."""
    for form in _EXPIRY_FORMATS:
        try:
            when = datetime.strptime((header or "").strip(), form)
        except ValueError:
            continue
        return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _connect(route: str, token: str, file: bool) -> dict:
    """Check ``token`` with GitHub, then keep it as the connection. The old one stays
    until the new one is known to work."""
    token = token.strip()
    if not token or len(token) > MAX_TOKEN or not token.isascii() or not token.isprintable() or " " in token:
        raise ConnectError("github_token_invalid", "that doesn't look like a GitHub token")
    status_code, headers, body = _http(f"{API}/user", token)
    if status_code in (401, 403):
        raise ConnectError("github_token_invalid", "GitHub didn't accept that token: it may be expired, revoked, "
                                                   "or waiting for an organization's approval")
    login = body.get("login")
    if status_code != 200 or not isinstance(login, str) or not login:
        raise ConnectError("github_unreachable", f"GitHub answered the sign-in check with HTTP {status_code}")
    in_keychain = is_helper() and not _file_chosen(file)
    record = {"route": route, "login": login, "saved_at": time.time(),
              "storage": "keychain" if in_keychain else "file",
              "expires_at": _expiry(headers.get("github-authentication-token-expiration"))}
    try:
        if in_keychain:
            _item().save(token.encode(), ask=True)      # the person asked: macOS may have them unlock it
        else:
            record["token"] = token
            if is_helper():
                _item().delete()        # an earlier connection's: the file holds it now
        _store().save(record)
    except mac_keychain.Refused:
        raise ConnectError("not_stored", "the keychain didn't take the sign-in (locked, or its dialog was "
                                         "dismissed): unlock it and connect again") from None
    except (keystore.CredentialError, OSError) as e:
        raise ConnectError("not_stored", f"couldn't keep the connection: {e}") from None
    with contextlib.suppress(OSError):
        (_dir() / REFUSED_FILE).unlink(missing_ok=True)
    return status()


def connect_token(token: str, *, file: bool = False, local: bool = False) -> dict:
    """Connect with a token the person pasted (a fine-grained one, made for the repositories
    brindle should reach). ``file``: keep it in the 0600 file whatever this install would
    choose. ``local``: this process is the one to keep it (the daemon, answering its route)."""
    if not isinstance(token, str):
        raise ConnectError("github_token_invalid", "that doesn't look like a GitHub token")
    if not local and _through_daemon(file):
        return _daemon("POST", {"route": "token", "token": token})
    return _connect("token", token, file)


def connect_gh(*, file: bool = False, local: bool = False) -> dict:
    """Connect with a copy of the GitHub CLI's sign-in, taken now, because the person asked.
    This is the one place gh's own store is read: on macOS that is its keychain item, so a
    locked keychain may ask here, once, with the person there to answer. brindle then has the
    same access gh has."""
    if not local and _through_daemon(file):
        return _daemon("POST", {"route": "gh"})
    unavailable = ConnectError("github_gh_unavailable", "the GitHub CLI isn't installed, or isn't signed in to "
                               "github.com: run `gh auth login` first, or connect with a token instead")
    exe = shutil.which("gh")
    if exe is None or not gh_available():
        raise unavailable
    try:
        proc = spawn.read([exe, "auth", "token", "-h", ORIGIN[1]], timeout=GH_COPY_SECONDS, to_its_end=True)
    except (OSError, subprocess.SubprocessError, spawn.Busy):
        raise unavailable from None
    token = proc.stdout.strip() if proc.returncode == 0 else ""
    if not token:
        raise unavailable
    return _connect("gh", token, file)


def disconnect(*, local: bool = False) -> dict:
    """Forget the connection. Nothing is revoked on GitHub: a copied sign-in is still gh's
    own, and a pasted token may be in use elsewhere; the person deletes a token on GitHub."""
    record = _load()
    if record is not None and record.get("storage") == "keychain":
        if not is_helper() and not local:
            return _daemon("DELETE")
        if is_helper():
            _item().delete()
    with contextlib.suppress(keystore.CredentialError, OSError):
        _store().delete()
    with contextlib.suppress(OSError):
        (_dir() / REFUSED_FILE).unlink(missing_ok=True)
    return status()


# -- git's credential helper ----------------------------------------------------------------------


def helper_command() -> str:
    """The helper as git's config names it: this brindle, asked for ``_github-credential``."""
    from brindle.providers import brindle_invocation

    return "!" + shlex.join([*brindle_invocation(), "_github-credential"])


def git_helper() -> str | None:
    """The helper for git to ask, when there is a connection for it to try."""
    return helper_command() if offered() else None


def answer(action: str, request: str, *, local: bool = False) -> str:
    """What the helper tells git (gitcredentials' protocol: ``key=value`` lines in and out).

    ``get`` gives the connection's credential, for ``https://github.com`` only. ``erase``
    is git saying GitHub refused what it was given: when that was this credential, it goes
    on record (note_refused). ``store`` keeps nothing: brindle already has it.

    A token in the daemon's keychain item is the daemon's to give: any other process
    passes git's question on to it (``local``: this is the daemon answering)."""
    fields = dict(line.split("=", 1) for line in request.splitlines() if "=" in line)
    if (fields.get("protocol"), fields.get("host")) != ORIGIN:
        return ""
    record = _load()
    if record is None:
        return ""
    if record.get("storage") == "keychain" and not is_helper():
        return "" if local else _ask_daemon(action, request)
    if action == "get":
        got = credential()
        return f"username={got[0]}\npassword={got[1]}\n" if got else ""
    if action == "erase":
        token = _token(record)
        if token and fields.get("password") == token:
            note_refused()
    return ""


def _ask_daemon(action: str, request: str) -> str:
    """git's question, answered by the daemon over its Unix socket; nothing when it isn't
    there to ask."""
    from brindle.daemon import holds

    got = holds.call(CREDENTIAL_PATH, {"action": action, "request": request}, ASK_DAEMON_SECONDS)
    return got["answer"] if got and isinstance(got.get("answer"), str) else ""


def main(argv: list[str]) -> int:
    """``brindle _github-credential <get|store|erase>``, as git runs it."""
    sys.stdout.write(answer(argv[0] if argv else "", sys.stdin.read()))
    return 0
