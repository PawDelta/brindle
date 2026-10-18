"""Cloud login state, read without calling the model.

``status(provider)`` says whether the Bedrock, Vertex or Foundry login is still
good, when it lapses, and the one command that renews it. It reads the CLIs'
own credential files (AWS SSO cache, Azure CLI token cache) and, for Google,
whose application-default credentials hold only a refresh token, asks gcloud
for an access token. Token values are never logged or returned.

``sign_in`` opens the one browser sign-in a lapsed login needs, however many
sessions and panes hit the lapse.
"""

from __future__ import annotations

import configparser
import fcntl
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from brindle import model_access

CLI_TIMEOUT = 15.0
# A sign-in tab this old is dead: AWS and Azure device codes and Google's
# authorization codes are good for about 10 minutes.
TAB_LIFE = 600.0
# No fresh tab sooner than this after the last opened: a sign-in that dies at
# once (the CLI won't start, a bad config) must not open one per keystroke.
REOPEN_FLOOR = 60.0
# The person is at brindle: someone typed in one of its sessions this recently.
PRESENT_WITHIN = 300.0

_FLAGS = {"bedrock": "CLAUDE_CODE_USE_BEDROCK", "vertex": "CLAUDE_CODE_USE_VERTEX",
          "foundry": "CLAUDE_CODE_USE_FOUNDRY"}


@dataclass(frozen=True)
class LoginStatus:
    ok: bool
    expires_at: float | None
    refresh_cmd: str
    note: str
    # The login lapsed and the CLI couldn't renew it: only ``refresh_cmd`` (the
    # person) can. False for a login that is fine, missing or undetermined.
    expired: bool = False


def provider_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """The cloud provider whose ``CLAUDE_CODE_USE_*`` flag is on, or None."""
    env = os.environ if env is None else env
    for provider, flag in _FLAGS.items():
        if str(env.get(flag, "")).strip().lower() in ("1", "true", "yes", "on"):
            return provider
    return None


def _epoch(value) -> float | None:
    """A timestamp as epoch seconds: a number, a numeric string or ISO 8601."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value.strip().replace("UTC", "Z").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _json_files(paths) -> list:
    out = []
    for p in paths:
        try:
            out.append(json.loads(Path(p).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _from_expiry(expires_at: float | None, refresh_cmd: str, now: float, missing: str) -> LoginStatus:
    if expires_at is None:
        return LoginStatus(False, None, refresh_cmd, missing)
    if expires_at <= now:
        return LoginStatus(False, expires_at, refresh_cmd, "login expired", expired=True)
    return LoginStatus(True, expires_at, refresh_cmd, "logged in")


def _aws_token_key(profile: str, config_path: Path) -> str | None:
    """What the SSO cache file name is the SHA-1 of for ``profile``: its
    sso_session name, or (legacy config) its sso_start_url; None when the
    profile doesn't use SSO."""
    cp = configparser.RawConfigParser()
    try:
        cp.read(config_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    section = "default" if profile == "default" else f"profile {profile}"
    if not cp.has_section(section):
        return None
    return cp.get(section, "sso_session", fallback=None) or cp.get(
        section, "sso_start_url", fallback=None)


def _aws(env: Mapping[str, str], home: Path, now: float) -> LoginStatus:
    profile = env.get("AWS_PROFILE") or env.get("AWS_DEFAULT_PROFILE") or "default"
    refresh = f"aws sso login --profile {profile}"
    config = Path(env.get("AWS_CONFIG_FILE") or home / ".aws" / "config")
    key = _aws_token_key(profile, config)
    if key is None:
        return LoginStatus(False, None, refresh, f"AWS profile {profile} is not an SSO profile")
    token = home / ".aws" / "sso" / "cache" / (hashlib.sha1(key.strip().encode()).hexdigest() + ".json")
    data = (_json_files([token]) or [None])[0]
    expires = _epoch(data.get("expiresAt")) if isinstance(data, dict) and data.get("accessToken") else None
    return _from_expiry(expires, refresh, now, "no AWS SSO login found for this profile")


def run_bounded(argv: list[str], *, capture_output: bool = True, text: bool = True,
                timeout: float = CLI_TIMEOUT, env=None) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a cloud CLI that leaves nothing behind: the CLI
    gets no terminal and its own process group, and a timeout ends the whole
    group (gcloud is a wrapper around python, which may hold a local auth
    server and its port), not just the wrapper."""
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=text, env=env,
                          start_new_session=True) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            raise      # Popen's exit reaps the wrapper
        try:    # a server the CLI left running (trade-off: the group id could in theory
            os.killpg(proc.pid, signal.SIGKILL)    # be reused once the group is empty)
        except OSError:
            pass
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _full_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    """The environment for a CLI call: the supplied one over the ambient one,
    or None (inherit) when the caller supplied none."""
    return None if env is None else {**os.environ, **env}


def _azure_expiry(token: dict) -> float | None:
    exp = _epoch(token.get("expires_on"))
    if exp is not None:
        return exp
    try:   # "expiresOn" is a naive local time
        return datetime.fromisoformat(str(token.get("expiresOn")).strip()).timestamp()
    except ValueError:
        return None


def _azure(run, env, now: float) -> LoginStatus:
    refresh = "az login"
    try:
        proc = run(["az", "account", "get-access-token", "--output", "json"],
                   capture_output=True, text=True, timeout=CLI_TIMEOUT, env=_full_env(env))
    except subprocess.TimeoutExpired:
        return LoginStatus(False, None, refresh, "az timed out fetching an access token")
    except OSError:
        return LoginStatus(False, None, refresh, "az is not installed")
    if proc.returncode == 0:
        try:
            token = json.loads(proc.stdout or "")
        except ValueError:
            token = None
        exp = _azure_expiry(token) if isinstance(token, dict) else None
        if exp is not None:
            return _from_expiry(exp, refresh, now, "")
    if model_access.expired_login(f"{proc.stdout or ''}\n{proc.stderr or ''}") is not None:
        return LoginStatus(False, None, refresh, "login expired", expired=True)
    return LoginStatus(False, None, refresh, "no Azure CLI login found")


def tokeninfo_expiry(token: str, timeout: float = CLI_TIMEOUT) -> float | None:
    """Seconds the Google access ``token`` has left, from Google's tokeninfo
    endpoint (the token goes in the POST body, never a URL), or None."""
    req = urllib.request.Request("https://oauth2.googleapis.com/tokeninfo",
                                 data=urllib.parse.urlencode({"access_token": token}).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - fixed https URL
            return float(json.load(r)["expires_in"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _google(run, env, now: float, expiry) -> LoginStatus:
    refresh = "gcloud auth application-default login"
    try:
        proc = run(["gcloud", "auth", "application-default", "print-access-token"],
                   capture_output=True, text=True, timeout=CLI_TIMEOUT, env=_full_env(env))
    except subprocess.TimeoutExpired:
        return LoginStatus(False, None, refresh, "gcloud timed out refreshing the access token")
    except OSError:
        return LoginStatus(False, None, refresh, "gcloud is not installed")
    token = (proc.stdout or "").strip()
    if proc.returncode == 0 and token:
        left = expiry(token)
        if left is None:
            return LoginStatus(True, None, refresh, "logged in; token expiry unknown")
        return LoginStatus(True, now + left, refresh, "logged in")
    if model_access.expired_login(f"{proc.stdout or ''}\n{proc.stderr or ''}") is not None:
        return LoginStatus(False, None, refresh, "login expired (invalid_grant)", expired=True)
    return LoginStatus(False, None, refresh, "gcloud could not produce an access token")


def status(provider: str | None = None, *, env: Mapping[str, str] | None = None,
           home: str | Path | None = None, now: float | None = None,
           run=run_bounded, expiry=tokeninfo_expiry) -> LoginStatus:
    """The login state for ``provider`` (bedrock, vertex or foundry; by default
    the one the environment's ``CLAUDE_CODE_USE_*`` flag selects)."""
    supplied = env
    env = os.environ if env is None else {**os.environ, **env}
    provider = provider or provider_from_env(env)
    home = Path(home) if home is not None else Path.home()
    now = time.time() if now is None else now
    if provider == "bedrock":
        return _aws(env, home, now)
    if provider == "vertex":
        return _google(run, supplied, now, expiry)
    if provider == "foundry":
        return _azure(run, supplied, now)
    return LoginStatus(True, None, "", "no cloud provider selected")


def refresh(provider: str | None = None, *, env: Mapping[str, str] | None = None,
            home: str | Path | None = None, now: float | None = None,
            run=run_bounded, expiry=tokeninfo_expiry) -> LoginStatus:
    """Renew the login without the person where the CLI can, and return the
    status after. Asking gcloud or az for an access token (as ``status`` does)
    renews it from the stored refresh token; the AWS CLI renews an SSO token
    from its sso-session refresh token on any call. What needs a browser
    (invalid_rapt, a lapsed SSO session) stays ``expired``."""
    full = os.environ if env is None else {**os.environ, **env}
    provider = provider or provider_from_env(full)
    said = ""
    if provider == "bedrock":
        profile = full.get("AWS_PROFILE") or full.get("AWS_DEFAULT_PROFILE") or "default"
        try:
            proc = run(["aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json"],
                       capture_output=True, text=True, timeout=CLI_TIMEOUT, env=_full_env(env))
            said = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        except (subprocess.TimeoutExpired, OSError):
            pass
    s = status(provider, env=env, home=home, now=now, run=run, expiry=expiry)
    if provider == "bedrock" and s.expired and model_access.expired_login(said) is None:
        # The cached SSO token is past its time but the CLI didn't say the SSO
        # session lapsed: it couldn't reach AWS (offline, timed out, no CLI).
        # A sign-in wouldn't fix that; the next renewal will.
        return LoginStatus(False, s.expires_at, s.refresh_cmd, "could not renew the AWS SSO token")
    return s


# -- the one browser sign-in -----------------------------------------------------------

def _credentials_file(provider: str, env: Mapping[str, str], home: Path) -> Path | None:
    """The file the CLI keeps the login in, which its sign-in rewrites."""
    if provider == "vertex":
        if env.get("GOOGLE_APPLICATION_CREDENTIALS"):
            return Path(env["GOOGLE_APPLICATION_CREDENTIALS"])
        root = Path(env.get("CLOUDSDK_CONFIG") or home / ".config" / "gcloud")
        return root / "application_default_credentials.json"
    if provider == "bedrock":
        profile = env.get("AWS_PROFILE") or env.get("AWS_DEFAULT_PROFILE") or "default"
        key = _aws_token_key(profile, Path(env.get("AWS_CONFIG_FILE") or home / ".aws" / "config"))
        if key is None:
            return None
        return home / ".aws" / "sso" / "cache" / (hashlib.sha1(key.strip().encode()).hexdigest() + ".json")
    if provider == "foundry":
        return Path(env.get("AZURE_CONFIG_DIR") or home / ".azure") / "msal_token_cache.json"
    return None


def _fingerprint(provider: str, path: Path | None) -> str:
    """Which sign-in ``path`` holds, as a digest (never the credentials). The
    file changes on a sign-in and, while the login works, on renewals; during
    a lapse only a sign-in changes it."""
    if path is None:
        return "none"
    try:
        data = path.read_bytes()
    except OSError:
        return "missing"
    if provider == "foundry":
        # The MSAL cache also holds access tokens for every resource and
        # tenant, which other `az` calls rewrite during a lapse; a sign-in is
        # its refresh tokens. shortcut: every account's refresh tokens count,
        # so another account's sign-in reads as a new lapse for this one.
        try:
            data = json.dumps(json.loads(data).get("RefreshToken"), sort_keys=True).encode()
        except (ValueError, AttributeError):
            pass
    return hashlib.sha256(data).hexdigest()


def _signin_paths(provider: str, creds: Path | None) -> tuple[Path, Path]:
    """The lock held while a sign-in runs, and the record of which lapse it was
    for. One pair per login: every pane using that login shares them."""
    from brindle.config import brindle_home

    key = hashlib.sha1(f"{provider}:{creds}".encode()).hexdigest()[:12]
    base = brindle_home() / "cloud-signin"
    return base / f"{provider}-{key}.lock", base / f"{provider}-{key}.json"


def _browser_here(env: Mapping[str, str], platform: str) -> bool:
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return False
    return not platform.startswith("linux") or bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def _locked(fd: int) -> bool:
    """Take the lock on ``fd``; False when another process holds it."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@dataclass(frozen=True)
class _Login:
    """One login's files: where the CLI keeps it, and brindle's sign-in lock
    and record for it (shared by every pane using that login)."""
    provider: str
    env: Mapping[str, str]      # the full environment
    creds: Path | None
    lock: Path
    record: Path

    def fingerprint(self) -> str:
        return _fingerprint(self.provider, self.creds)

    def read(self) -> dict:
        last = (_json_files([self.record]) or [None])[0]
        return last if isinstance(last, dict) else {}

    def write(self, data: dict) -> None:
        tmp = self.record.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.record)     # several sweepers read it: never half written


def _login(provider: str | None, env: Mapping[str, str] | None, home) -> _Login | None:
    full = os.environ if env is None else {**os.environ, **env}
    provider = provider or provider_from_env(full)
    if provider is None:
        return None
    creds = _credentials_file(provider, full, Path(home) if home is not None else Path.home())
    lock, record = _signin_paths(provider, creds)
    return _Login(provider, full, creds, lock, record)


def login_key(env: Mapping[str, str] | None = None, home: str | Path | None = None) -> str | None:
    """Which login ``env`` uses (agents with the same key share one), or None
    off the cloud."""
    lg = _login(None, env, home)
    return None if lg is None else str(lg.lock)


def _running(lg: _Login) -> bool:
    """Whether a sign-in for ``lg`` is running: something holds its lock."""
    try:
        fd = os.open(lg.lock, os.O_RDONLY)
    except OSError:
        return False
    try:
        return not _locked(fd)
    finally:
        os.close(fd)


def signing_in(provider: str | None = None, *, env: Mapping[str, str] | None = None,
               home: str | Path | None = None) -> bool:
    """Whether the browser sign-in for this lapse is open right now."""
    lg = _login(provider, env, home)
    return lg is not None and _running(lg) and lg.read().get("lapse") == lg.fingerprint()


def _end_signin(pid: int) -> None:
    os.killpg(pid, signal.SIGTERM)    # its own session: the CLI and anything it started


def end_signin(provider: str | None = None, *, env: Mapping[str, str] | None = None,
               home: str | Path | None = None, kill=None) -> bool:
    """End brindle's sign-in for this login if one is still running: once the
    login works (the person signed in some other way) its tab is no use."""
    lg = _login(provider, env, home)
    pid = lg.read().get("pid") if lg is not None else None
    if not isinstance(pid, int) or not _running(lg):
        return False
    try:
        (kill or _end_signin)(pid)
    except OSError:
        return False    # already gone, or not ours to end
    return True


def account(provider: str | None = None, *, env: Mapping[str, str] | None = None,
            home: str | Path | None = None) -> str | None:
    """Who the login on disk belongs to, when the CLI records it. shortcut:
    only Google's ADC file names its account; an AWS profile pins its account
    itself, and Azure's cache can hold several, so both give None."""
    lg = _login(provider, env, home)
    if lg is None or lg.provider != "vertex" or lg.creds is None:
        return None
    data = (_json_files([lg.creds]) or [None])[0]
    name = data.get("account") if isinstance(data, dict) else None
    return name if isinstance(name, str) and name else None


def _dead_at(last: dict, running: bool, now: float) -> float | None:
    """When the tab ``last`` records stopped being usable: its sign-in exited,
    or it is older than ``TAB_LIFE``. None while it can still be used."""
    opened = float(last.get("opened_at") or now)
    if not running:
        return float(last.get("dead_at") or now)
    return opened + TAB_LIFE if now >= opened + TAB_LIFE else None


def sign_in(provider: str | None = None, *, env: Mapping[str, str] | None = None,
            home: str | Path | None = None, now: float | None = None,
            last_input: float | None = None, run=run_bounded, expiry=tokeninfo_expiry,
            popen=None, kill=None, platform: str | None = None, which=None) -> str:
    """Open the browser sign-in a lapsed login needs: one tab machine-wide,
    when the person is there to use it.

    Single-flight: the sign-in runs ``refresh_cmd`` holding an OS lock under
    brindle's home for as long as it runs; a caller that finds it open gets
    ``"waiting"``. Under the lock the login is checked again, so a sign-in
    that just finished elsewhere opens no second one, and only a real lapse
    opens one (never a login that is fine, signed out or unreachable).

    ``last_input`` is when the person last typed in any brindle session. The
    first tab of a lapse opens once they're present (typed within
    ``PRESENT_WITHIN``; until then ``"deferred"``). A tab that was closed,
    ignored or failed is replaced only on input after it died (its sign-in
    exited, or it is ``TAB_LIFE`` old) and at least ``REOPEN_FLOOR`` after it
    opened, one fresh tab per such input; until
    then ``"tried"`` (or ``"waiting"`` while it can still be used), so typing
    on past an open tab never opens another.

    Also ``"manual"`` (no browser can open here, or the sign-in didn't start)
    and ``"no lapse"``."""
    lg = _login(provider, env, home)
    if lg is None:
        return "no lapse"
    now = time.time() if now is None else now
    present = last_input is not None and now - last_input <= PRESENT_WITHIN
    lg.lock.parent.mkdir(parents=True, exist_ok=True)

    def still_lapsed() -> LoginStatus:
        return refresh(provider=lg.provider, env=env, home=home, now=now, run=run, expiry=expiry)

    fd = os.open(lg.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        last = lg.read()
        running = not _locked(fd)
        this_lapse = last.get("lapse") == lg.fingerprint() and "opened_at" in last
        ended = False
        if this_lapse:
            dead_at = _dead_at(last, running, now)
            if dead_at is None:
                return "waiting"
            if running and isinstance(last.get("pid"), int):
                try:    # past TAB_LIFE its code is useless: don't leave its auth server up
                    (kill or _end_signin)(last["pid"])
                except OSError:
                    pass
                ended = True
            if not running and "dead_at" not in last:
                lg.write({**last, "dead_at": dead_at})   # we hold the lock
            floor = float(last["opened_at"]) + REOPEN_FLOOR
            if not present or last_input <= max(dead_at, floor):
                return "tried"
        elif not present:
            return "deferred"
        # Open one: this lapse has no tab yet, or its tab died and they typed since.
        s = still_lapsed()
        if not s.expired or not s.refresh_cmd:
            return "no lapse"
        if running:     # a dead tab, or one left from an earlier lapse: end it, take its lock
            if isinstance(last.get("pid"), int) and not ended:
                try:
                    (kill or _end_signin)(last["pid"])
                except OSError:
                    pass    # already gone, or not ours to end: the lock says which
            deadline = time.monotonic() + 5.0
            while not _locked(fd):
                if time.monotonic() > deadline:
                    return "waiting"
                time.sleep(0.1)
        child_env = _full_env(env)
        if not _browser_here(lg.env, sys.platform if platform is None else platform):
            # WSL2 has no Linux browser, but the Windows one can open the URL.
            from brindle import providers

            opener = None if lg.env.get("SSH_CONNECTION") or lg.env.get("SSH_TTY") else providers.windows_browser(which)
            if not opener:
                return "manual"
            child_env = {**os.environ, **(env or {}), "BROWSER": opener}
        try:
            # The child inherits the locked descriptor: the lock is held until
            # the sign-in exits, however long the person takes.
            proc = (popen or subprocess.Popen)(
                shlex.split(s.refresh_cmd), env=child_env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(fd,))
        except OSError:
            return "manual"
        lg.write({"lapse": lg.fingerprint(), "pid": getattr(proc, "pid", None), "opened_at": now})
        return "opened"
    finally:
        os.close(fd)
