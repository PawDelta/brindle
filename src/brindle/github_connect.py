"""Connect GitHub: brindle's own sign-in to github.com.

A person connects once, either with a copy of their GitHub CLI sign-in (taken at
their click, the one moment gh's keychain item is read) or a fine-grained token
they paste. brindle keeps that credential in its own store, and git signs in to
``https://github.com`` through brindle's credential helper alone (:func:`answer`,
wired in by :mod:`brindle.git`): in the fetches brindle starts on its own, a clone
or push it runs for the person, and agents' panes. git's keychain helper and gh's
keychain item are never asked on those paths, so no dialog can come of them.

What this does not do is keep the credential from agents. They run as the same
user: a command brindle's rules don't recognise can read the store or ask the
helper, as it can ask gh's today. The rules are not a sandbox. What a taken
credential is worth is set by its kind: a fine-grained token reaches the
repositories and permissions it was made for until it expires; a copy of gh's
sign-in reaches everything gh does and doesn't expire.

Nothing here logs, prints or raises a token.
"""

from __future__ import annotations

import contextlib
import json
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

from brindle import keystore, spawn

SERVICE = "brindle-github"
# What git must be asking about for the helper to answer: never another host or scheme,
# whatever a repo's config has git ask.
ORIGIN = ("https", "github.com")
API = "https://api.github.com"
ROUTES = ("gh", "token")
REFUSED_FILE = "refused"
MAX_TOKEN = 255
HTTP_SECONDS = 15.0
GH_COPY_SECONDS = 60.0       # the keychain may ask the person to unlock it: they clicked, so it may wait for them
_EXPIRY_FORMATS = ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z")


class ConnectError(Exception):
    """Connecting didn't work. ``code`` is the service error's; the message never carries a token."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# -- the store ----------------------------------------------------------------------------------


def _dir() -> Path:
    from brindle.config import brindle_home

    return brindle_home() / SERVICE


def storage() -> str:
    """Where the connection is kept: ``file``, a 0600 file in a 0700 folder under brindle's
    home (keystore.FileStore), which no dialog can stand in front of."""
    return "file"


def _store():
    return keystore.FileStore(_dir(), "connection")


def store_paths() -> list[str]:
    """What holds the credential on disk, for the lists of what agents may not read."""
    return [str(_dir())]


def _load() -> dict | None:
    try:
        record = _store().load()
    except (keystore.CredentialError, OSError):
        return None     # a store that can't be trusted is no connection
    return record if record and record.get("token") and record.get("route") in ROUTES else None


def _refused() -> bool:
    return (_dir() / REFUSED_FILE).exists()


def note_refused() -> None:
    """GitHub turned the credential down (revoked, expired, replaced): it isn't offered
    again until the person connects again, so no poll keeps trying it."""
    with contextlib.suppress(OSError):
        (_dir() / REFUSED_FILE).touch()


def _expired(record: dict) -> bool:
    expires = record.get("expires_at")
    return isinstance(expires, (int, float)) and expires <= time.time()


def credential() -> tuple[str, str] | None:
    """(username, password) for git on github.com, or None without a connection GitHub still takes."""
    record = _load()
    if record is None or _refused() or _expired(record):
        return None
    return record.get("login") or "x-access-token", record["token"]


# -- what the app, the CLI and doctor show ---------------------------------------------------------


def gh_available() -> bool:
    """Whether a copy of gh's sign-in can be offered: gh is installed and signed in to
    github.com. Asks gh's config only, which reads no keychain item."""
    from brindle import git

    return git.gh_credential_helper() is not None


def status() -> dict:
    """The connection as GET /v1/github/connection gives it. Never the token."""
    record = _load()
    return {
        "connected": record is not None,
        "login": record.get("login") if record else None,
        "route": record["route"] if record else None,
        "storage": storage() if record else None,
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
    line = f"connected as {state['login']} with {how}, kept in a {state['storage']}"
    if state["expires_at"]:
        line += f", expires {time.strftime('%Y-%m-%d', time.localtime(state['expires_at']))}"
    return line + ("; GitHub no longer accepts it: connect again" if state["needs_reconnect"] else "")


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
            status, headers, raw = resp.status, resp.headers, resp.read(keystore.MAX_BLOB)
    except urllib.error.HTTPError as e:
        status, headers, raw = e.code, e.headers, b""
    except (urllib.error.URLError, OSError, ValueError):
        raise ConnectError("github_unreachable", "couldn't reach GitHub to check the sign-in") from None
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    return status, {k.lower(): v for k, v in (headers or {}).items()}, body if isinstance(body, dict) else {}


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


def _connect(route: str, token: str) -> dict:
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
    record = {"route": route, "login": login, "token": token, "saved_at": time.time(),
              "expires_at": _expiry(headers.get("github-authentication-token-expiration"))}
    try:
        _store().save(record)
    except (keystore.CredentialError, OSError) as e:
        raise ConnectError("not_stored", f"couldn't keep the connection: {e}") from None
    with contextlib.suppress(OSError):
        (_dir() / REFUSED_FILE).unlink(missing_ok=True)
    return status()


def connect_token(token: str) -> dict:
    """Connect with a token the person pasted (a fine-grained one, made for the repositories
    brindle should reach)."""
    if not isinstance(token, str):
        raise ConnectError("github_token_invalid", "that doesn't look like a GitHub token")
    return _connect("token", token)


def connect_gh() -> dict:
    """Connect with a copy of the GitHub CLI's sign-in, taken now, because the person asked.
    This is the one place gh's own store is read: on macOS that is its keychain item, so a
    locked keychain may ask here, once, with the person there to answer. brindle then has the
    same access gh has."""
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
    return _connect("gh", token)


def disconnect() -> dict:
    """Forget the connection. Nothing is revoked on GitHub: a copied sign-in is still gh's
    own, and a pasted token may be in use elsewhere; the person deletes a token on GitHub."""
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
    """The helper for git to ask, when there is a connection GitHub still takes."""
    return helper_command() if credential() is not None else None


def answer(action: str, request: str) -> str:
    """What the helper tells git (gitcredentials' protocol: ``key=value`` lines in and out).

    ``get`` gives the connection's credential, for ``https://github.com`` only. ``erase``
    is git saying GitHub refused what it was given: when that was this credential, it goes
    on record (note_refused). ``store`` keeps nothing: brindle already has it."""
    fields = dict(line.split("=", 1) for line in request.splitlines() if "=" in line)
    if (fields.get("protocol"), fields.get("host")) != ORIGIN:
        return ""
    if action == "get":
        got = credential()
        return f"username={got[0]}\npassword={got[1]}\n" if got else ""
    if action == "erase":
        record = _load()
        if record is not None and fields.get("password") == record["token"]:
            note_refused()
    return ""


def main(argv: list[str]) -> int:
    """``brindle _github-credential <get|store|erase>``, as git runs it."""
    sys.stdout.write(answer(argv[0] if argv else "", sys.stdin.read()))
    return 0
