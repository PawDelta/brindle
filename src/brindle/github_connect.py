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

Nothing here logs or raises a token. The credential helper writes the token to
stdout only as Git's credential-protocol reply, which Git consumes to sign in.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import http.client
import json
import os
import secrets
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from brindle import keystore, mac_keychain, spawn

try:
    import fcntl
except ImportError:     # Windows: threads are still serialized
    fcntl = None

SERVICE = "brindle-github"
ITEM_ACCOUNT = "connection"
# What git must be asking about for the helper to answer: never another host or scheme,
# whatever a repo's config has git ask.
ORIGIN = ("https", "github.com")
# The helper's word to git that nobody else is to be asked: no other helper, askpass or terminal.
QUIT = "quit=true\n"
API = "https://api.github.com"
ROUTES = ("gh", "token")
STORE_ENV = "BRINDLE_GITHUB_STORE"
REFUSED_FILE = "refused"            # holds the generation GitHub (or the keychain) no longer takes
ITEMS_FILE = "items"                # every generation that may have a keychain item (see "the store")
STAGING_PREFIX = "staging-"         # a generation's staging lock file
LOCK_FILE = "lock"
_threads = threading.Lock()
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
#
# A connection is one generation: a record (who, how, until when, where the token is) and
# the token it names. A change stages the new generation's token where nothing reads it yet
# (a keychain item under a name of its own; with ``file`` storage, inside the new record's
# own temp file), publishes the record with one rename, and only then removes the token of
# the generation before. So:
#
# * a reader finds a record and the token that goes with it, never one side of a change;
# * a change that fails, at either step, leaves the old connection whole, also when it was
#   moving between the keychain and the file;
# * a removal that fails leaves an orphan to remove at a later change, never a record
#   without its token.
#
# No keychain item is ever without something that names it. Three things say what an item
# is, and none of them is a guess about time:
#
# * the list (ITEMS_FILE): a generation goes on it before its item is made, making the
#   item waits on that write, and it comes off only once the item's removal has succeeded;
# * the record: the generation it names is the live one;
# * the generation's staging lock: the connect that stages it holds that from before it
#   is listed until it has published it or taken it back, however long a dialog keeps it.
#   The system gives the lock back if the process dies.
#
# A listed generation that is not the live one and whose staging lock is free is in
# nobody's use: an old one replaced, one disconnected, or one a connect left behind. That
# is all clearing away removes, so whatever fails on the way (a publish, a removal, a write
# of the list itself, the process), a later change still finds the item and removes it,
# and never one a connect is still about to make or publish.
#
# Publishing, the memory of a refusal and that list change under one lock, across threads
# and processes. Staging, which may wait at a keychain dialog, and reading don't take it.


def _dir() -> Path:
    from brindle.config import brindle_home

    return brindle_home() / SERVICE


def _store():
    """The connection's record: what isn't secret, and with ``file`` storage the token too.
    Its save is one rename of a file written under a name of its own."""
    return keystore.FileStore(_dir(), "connection")


def _generation(record: dict) -> str:
    return record.get("generation") or ""


def _item(record: dict) -> mac_keychain.Item:
    """The keychain item of ``record``'s generation."""
    generation = _generation(record)
    return mac_keychain.Item(SERVICE, f"{ITEM_ACCOUNT}-{generation}" if generation else ITEM_ACCOUNT)


def store_paths() -> list[str]:
    """What holds the credential on disk, for the lists of what agents may not read."""
    return [str(_dir())]


@contextlib.contextmanager
def _lock():
    """Alone among brindle's threads and processes in changing what is committed. Never
    taken twice by one thread, and never held across anything that can wait on a person."""
    folder = _dir()
    mask = os.umask(0o077)
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(folder / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    finally:
        os.umask(mask)
    try:
        with _threads:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
    finally:
        os.close(fd)        # which gives the lock back


def _write(name: str, text: str) -> None:
    """``text`` as the small file ``name`` beside the record, whole or not at all."""
    fd, tmp = tempfile.mkstemp(dir=_dir(), prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, _dir() / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _load() -> dict | None:
    try:
        record = _store().load()
    except (keystore.CredentialError, OSError):
        return None     # a store that can't be trusted is no connection
    return record if record and record.get("route") in ROUTES else None


def _refused(record: dict) -> bool:
    """Whether ``record``'s generation is the one on record as no use: a refusal of an
    earlier generation, arriving late, says nothing of this one."""
    try:
        return (_dir() / REFUSED_FILE).read_text(encoding="utf-8") == _generation(record)
    except OSError:
        return False


def note_refused(record: dict) -> None:
    """``record``'s credential is no use any more (GitHub turned it down, or its keychain
    item is gone or no longer this program's): it isn't given to git again until the person
    connects again, so no poll keeps trying it. Nothing, if another connection was committed
    meanwhile."""
    with contextlib.suppress(OSError), _lock():
        committed = _load()
        if committed is not None and _generation(committed) == _generation(record):
            _write(REFUSED_FILE, _generation(record))


def _items() -> dict[str, float]:
    """Every generation that may have a keychain item, with when it was listed. OSError
    when the list is there and can't be read: it is never taken for empty, as the next
    write would then forget what it named."""
    try:
        listed = json.loads((_dir() / ITEMS_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:
        raise OSError("brindle's list of keychain items can't be read") from None
    if not isinstance(listed, dict):
        raise OSError("brindle's list of keychain items can't be read")
    return {g: at for g, at in listed.items() if isinstance(at, (int, float))}


def _list_item(generation: str) -> None:
    """Put ``generation`` on the list, before its item is made. OSError when that can't be
    written: the caller then makes no item."""
    with _lock():
        listed = _items()
        listed[generation] = time.time()
        _write(ITEMS_FILE, json.dumps(listed))


@contextlib.contextmanager
def _staging(generation: str, wait: bool = True):
    """The staging lock of ``generation``: held, for all of it, by the connect that lists,
    makes and publishes (or takes back) its item. ``wait`` off is clearing away looking in:
    it yields False when that connect is still at work, in any thread or process. Taken
    before the lock on what is committed, never the other way round."""
    if fcntl is None:       # no keychain items where there is no flock
        yield True
        return
    folder = _dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(folder / f"{STAGING_PREFIX}{generation}", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)        # which gives the lock back


def _remove_item(generation: str) -> None:
    """Remove ``generation``'s keychain item and, only once it is gone, take it off the
    list. With its staging lock held. A removal that doesn't work leaves it listed."""
    if not _item({"generation": generation}).delete():
        return
    with _lock():
        listed = _items()
        listed.pop(generation, None)
        if listed:
            _write(ITEMS_FILE, json.dumps(listed))
        else:
            (_dir() / ITEMS_FILE).unlink(missing_ok=True)
    (_dir() / f"{STAGING_PREFIX}{generation}").unlink(missing_ok=True)


def _clean_up() -> None:
    """Remove the keychain items in nobody's use: each listed generation that is not the
    live one and that no connect is staging. Nothing here can break a connection, and
    nothing it fails at is forgotten: an item leaves the list only after it is gone."""
    if not is_helper():
        return      # the items are the helper's to remove: they stay listed for it
    try:
        with _lock():
            listed = list(_items())
    except OSError:
        return
    for generation in listed:
        with contextlib.suppress(OSError, keystore.CredentialError), _staging(generation, wait=False) as free:
            if not free:
                continue        # its connect is still at work: it may yet make the item, or publish it
            # Read strictly (a record that can't be read is not "no connection"), and with that
            # connect out of the way: only it could have made this generation the live one.
            committed = _store().load()
            if (committed is not None and committed.get("storage") == "keychain"
                    and _generation(committed) == generation):
                continue
            _remove_item(generation)


def _expired(record: dict) -> bool:
    expires = record.get("expires_at")
    return isinstance(expires, (int, float)) and expires <= time.time()


def _committed() -> tuple[dict, str | None] | None:
    """The committed connection and its token, of one generation; None without a
    connection. The token is None where it can't be had: in a process that isn't the one
    holding it, or from a keychain that is locked (which passes) or no longer gives it
    (which goes on record). Read without any dialog."""
    record = _load()
    for _ in range(2):      # a reconnect may publish between the record's read and the item's: once more
        if record is None:
            return None
        if record.get("storage") != "keychain":
            return record, record.get("token")
        if not is_helper():
            return record, None
        try:
            secret = _item(record).load()
        except mac_keychain.Refused as e:
            if not e.locked:
                note_refused(record)
            return record, None
        if secret is not None:
            return record, secret.decode()
        latest = _load()
        if latest is not None and _generation(latest) == _generation(record):
            note_refused(record)        # still the committed one, and its item is gone
            return record, None
        record = latest
    return (record, None) if record is not None else None


def connected() -> bool:
    """Whether the person connected GitHub, working or not. Reads nothing secret."""
    return _load() is not None


def credential() -> tuple[str, str] | None:
    """(username, password) for git on github.com, in the process that holds the token."""
    record = _load()
    if record is None or _refused(record) or _expired(record):
        return None
    got = _committed()
    if got is None or not got[1] or _refused(got[0]) or _expired(got[0]):
        return None
    return got[0].get("login") or "x-access-token", got[1]


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
        "needs_reconnect": record is not None and (_refused(record) or _expired(record)),
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
    record = {"route": route, "login": login, "saved_at": time.time(), "generation": secrets.token_hex(8),
              "storage": "keychain" if in_keychain else "file",
              "expires_at": _expiry(headers.get("github-authentication-token-expiration"))}
    generation = _generation(record)
    staged = _item(record) if in_keychain else None
    try:
        # This connect's own lock on its generation, for as long as it may yet make or publish
        # the item: clearing away, here or in another process, leaves the generation alone meanwhile.
        with _staging(generation) if staged is not None else contextlib.nullcontext():
            listed = False
            try:
                # Stage: nothing reads this generation yet. Its item is on the list before it
                # exists. The person asked, so macOS may have them unlock the keychain, and the
                # lock on what is committed is not held while it does.
                if staged is not None:
                    _list_item(generation)
                    listed = True
                    staged.save(token.encode(), ask=True)
                else:
                    record["token"] = token
                # Publish: from this rename on, this is the connection.
                with _lock():
                    with contextlib.suppress(OSError):
                        for abandoned in _dir().glob(".connection.*.tmp"):  # a write that died half way: no writer is in here now
                            abandoned.unlink(missing_ok=True)
                    _store().save(record)
                    # Past the rename nothing may fail this connect: what was staged is the connection now.
                    with contextlib.suppress(OSError):
                        (_dir() / REFUSED_FILE).unlink(missing_ok=True)
            except (mac_keychain.Refused, keystore.CredentialError, OSError):
                # The connection before this one is as it was. Take back what was staged: now
                # if that works, and if not, it stays on the list for a later change.
                if listed:
                    with contextlib.suppress(OSError):
                        _remove_item(generation)
                raise
    except (mac_keychain.Refused, keystore.CredentialError, OSError) as e:
        _clean_up()
        if isinstance(e, mac_keychain.Refused):
            raise ConnectError("not_stored", "the keychain didn't take the sign-in (locked, or its dialog was "
                                             "dismissed): unlock it and connect again") from None
        raise ConnectError("not_stored", f"couldn't keep the connection: {e}") from None
    # Only now the token of the generation before, if it has one of its own, and any left from earlier.
    _clean_up()
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
    own, and a pasted token may be in use elsewhere; the person deletes a token on GitHub.
    ConnectError when the record couldn't be removed: the connection is then still there,
    and nothing says otherwise. Clearing its token away afterwards can't fail it."""
    record = _load()
    if record is not None and record.get("storage") == "keychain" and not is_helper() and not local:
        return _daemon("DELETE")
    try:
        with _lock():
            _store().delete()       # from here on there is no connection
            # Past that nothing may fail this disconnect.
            with contextlib.suppress(OSError):
                (_dir() / REFUSED_FILE).unlink(missing_ok=True)
    except (keystore.CredentialError, OSError) as e:
        # Still connected, and said so: git and agents can use the credential as before.
        raise ConnectError("not_disconnected", f"couldn't forget the connection, which is still in place: {e}") from None
    # Only then its token, if it has one of its own: still listed if it can't be removed
    # now, and never removed while the record names it.
    _clean_up()
    return status()


# -- git's credential helper ----------------------------------------------------------------------


def helper_command() -> str:
    """The helper as git's config names it: this brindle, asked for ``_github-credential``."""
    from brindle.providers import brindle_invocation

    return "!" + shlex.join([*brindle_invocation(), "_github-credential"])


def git_helper() -> str | None:
    """The helper for git to ask, for as long as the person is connected: also once
    GitHub has stopped taking the credential or it has run out, when the helper ends
    git's asking (QUIT). Dropping it then would hand github.com back to git's keychain helper, or
    to gh's, just when the person has to connect again: a dialog, or another account's
    credential used without a word. Only disconnecting gives their helpers back."""
    return helper_command() if connected() else None


def answer(action: str, request: str, *, local: bool = False) -> str:
    """What the helper tells git (gitcredentials' protocol: ``key=value`` lines in and out).

    ``get`` gives the connection's credential, for ``https://github.com`` only. ``erase``
    is git saying GitHub refused what it was given: when that was this credential, it goes
    on record (note_refused). ``store`` keeps nothing: brindle already has it.

    A process that can't answer by itself passes git's question on to the daemon
    (``local``: this is the daemon answering). That is every process but the daemon
    when the token is in its keychain item, and one that can't read brindle's store at
    all: git under Codex's sandbox, which is denied the folder (and sees it as absent).

    A ``get`` that a connection can't answer (refused, run out, its keychain locked, the
    daemon not there to ask) ends git's asking (QUIT): with an empty answer git would go
    on to an askpass program or the terminal, which can raise a dialog or sign in as
    someone else. Only with no connection at all is the answer empty."""
    fields = dict(line.split("=", 1) for line in request.splitlines() if "=" in line)
    if (fields.get("protocol"), fields.get("host")) != ORIGIN:
        return ""
    stop = QUIT if action == "get" else ""
    record = _load()
    if record is None or record.get("storage") == "keychain" and not is_helper():
        if local:
            return stop if record is not None else ""
        return _ask_daemon(action, request, stop)
    if action == "get":
        got = credential()
        return f"username={got[0]}\npassword={got[1]}\n" if got else stop
    if action == "erase":
        got = _committed()
        if got is not None and got[1] and fields.get("password") == got[1]:
            note_refused(got[0])
    return ""


def _ask_daemon(action: str, request: str, unanswered: str) -> str:
    """git's question, answered by the daemon over its Unix socket; ``unanswered`` when it
    isn't there to ask."""
    from brindle.daemon import holds

    got = holds.call(CREDENTIAL_PATH, {"action": action, "request": request}, ASK_DAEMON_SECONDS)
    return got["answer"] if got and isinstance(got.get("answer"), str) else unanswered


def main(argv: list[str]) -> int:
    """``brindle _github-credential <get|store|erase>``, as git runs it. git only does for
    github.com, so a ``get`` that goes wrong here ends its asking too, rather than leaving
    it to go on to an askpass program."""
    action = argv[0] if argv else ""
    try:
        said = answer(action, sys.stdin.read())
    except Exception:  # noqa: BLE001 - whatever broke, git must not fall through to a prompt
        said = QUIT if action == "get" else ""
    # CodeQL py/clear-text-logging-sensitive-data (alert #19): this is Git's
    # credential-protocol channel, not a log. Git consumes the password field
    # from stdout to authenticate; redacting it would break the helper.
    sys.stdout.write(said)
    return 0
