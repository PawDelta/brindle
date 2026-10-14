"""Short-lived cloud credentials for org members, through PawDelta.

``brindle cloud-credential aws|gcp|azure --org ID`` is the command each cloud's
own credential hook runs when the org's policy says ``claude.auth: federated``:
the AWS ``credential_process`` of the ``brindle-<org>`` profile, the executable
source of the GCP external-account file, and (Azure has no hook) the refresher of
the federated token file. It asks PawDelta for a 10-minute ID token for the org
(``POST /orgs/{org}/federation/token``), exchanges it with the cloud where the
cloud's own client can't (AWS), and caches the answer under
``$BRINDLE_HOME/federation/<org>/`` (a 0700 directory of 0600 files).

Pull-based: the cloud SDK calls this when it needs credentials, so nothing runs
in the background. What it prints is only what the hook contract requires; a
token or secret is never logged. A per-org lock makes ten panes that start
together make one mint, not ten.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from brindle import airgap, config

log = logging.getLogger(__name__)

CLOUDS = ("aws", "gcp", "azure")
ORG_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
REFRESH_BELOW = 0.3             # mint again once less than this fraction of a credential's life remains
AZURE_TOKEN_SECONDS = 600       # the ID token's life
AZURE_REFRESH_INTERVAL = 120.0  # under REFRESH_BELOW of it, so no tick can skip past the threshold
AWS_SESSION_SECONDS = 900      # the STS minimum: the 15-minute lifetime
STS_TIMEOUT = 30.0
MAX_RESPONSE = 256 * 1024
LOCK_TIMEOUT = 60.0
HOOK_TIMEOUT_MILLIS = 30000
MAX_TOKEN_CHARS = 8192

AWS_CACHE = "aws.json"
GCP_CONFIG = "gcp-credentials.json"
GCP_TOKEN = "gcp-subject-token.json"
AZURE_TOKEN = "azure-token"
LOCK_FILE = "mint.lock"
REAUTH_FILE = "reauth-org"
CACHE_FILES = (AWS_CACHE, GCP_TOKEN, AZURE_TOKEN)

LOGIN_LINE = "not logged in to brindle Pro; run `brindle account login`"
NOT_OFFERED = "this server doesn't offer federated credentials yet"
OFFLINE_LINE = "can't reach brindle Pro and no cached credential is still valid; check your network"
GCP_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
GCP_TOKEN_URL = "https://sts.googleapis.com/v1/token"
GCP_IMPERSONATION_URL = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{}:generateAccessToken"


class CredentialError(Exception):
    """A credential couldn't be had; the message is what the person sees."""


class Offline(Exception):
    """The backend or the cloud couldn't be reached (a cached credential may still serve)."""


# -- where things live ------------------------------------------------------------------------

def federation_root() -> Path:
    return config.brindle_home() / "federation"


def org_dir(org_id: str, create: bool = False) -> Path:
    if not ORG_RE.fullmatch(org_id or ""):
        raise CredentialError("invalid org id")
    d = federation_root() / org_id
    if create:
        old = os.umask(0o077)
        try:
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        finally:
            os.umask(old)
        os.chmod(d, 0o700)
        os.chmod(d.parent, 0o700)
    return d


def aws_cache_path(org_id: str) -> Path:
    return org_dir(org_id) / AWS_CACHE


def gcp_config_path(org_id: str) -> Path:
    return org_dir(org_id) / GCP_CONFIG


def azure_token_path(org_id: str) -> Path:
    return org_dir(org_id) / AZURE_TOKEN


def clear_all(keep=frozenset()) -> list[str]:
    """Remove every ``federation/<org>/`` but those of ``keep`` (logout, a
    signed-out ``doctor --fix``); the org ids that were removed."""
    done: list[str] = []
    try:
        children = sorted(federation_root().iterdir())
    except OSError:
        return done
    for d in children:
        if d.name not in keep and ORG_RE.fullmatch(d.name) and _remove(d):
            done.append(d.name)
    return done


def note_reauth(org_id: str) -> None:
    """Remember the org that needed a fresh sign-in, so the next login returns to it."""
    try:
        org_dir(org_id)
        root = federation_root()
        old = os.umask(0o077)
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        finally:
            os.umask(old)
        _write(root / REAUTH_FILE, org_id)
    except (OSError, CredentialError):
        log.debug("couldn't note the org that needs reauth", exc_info=True)


def take_reauth() -> str | None:
    """The org ``note_reauth`` recorded (forgotten as it is read)."""
    path = federation_root() / REAUTH_FILE
    try:
        org = path.read_text().strip()
        path.unlink()
    except OSError:
        return None
    return org if ORG_RE.fullmatch(org) else None


def _remove(d: Path) -> bool:
    try:
        if d.is_symlink():
            d.unlink()
        elif d.is_dir():
            shutil.rmtree(d)
        else:
            return False
    except OSError:
        log.debug("couldn't remove %s", d.name, exc_info=True)
        return False
    return True


def org_ids() -> list[str]:
    try:
        return sorted(d.name for d in federation_root().iterdir() if ORG_RE.fullmatch(d.name) and d.is_dir())
    except OSError:
        return []


def _read(path: Path, limit: int = MAX_RESPONSE) -> bytes | None:
    from brindle.pro._files import read_private

    try:
        return read_private(path, limit)
    except Exception:  # noqa: BLE001 - an unreadable or unsafe cache file is no cache
        log.debug("ignoring unreadable %s", path.name)
        return None


def _write(path: Path, data: str) -> None:
    from brindle.pro._files import write_private

    write_private(path, data.encode())


def _read_json(path: Path) -> dict | None:
    raw = _read(path)
    try:
        got = json.loads(raw) if raw else None
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


# -- the hook command ----------------------------------------------------------------------------

_SAFE_PATH = re.compile(r"[\w ./@+=:,-]+")


def _vet(path: str, what: str) -> None:
    """``path`` must be absolute, plain text the cloud SDKs can't misread (they
    run the hook through a shell or a command splitter), an existing executable
    file, and in no world-writable place."""
    if not os.path.isabs(path):
        raise CredentialError(f"{what} isn't an absolute path: {path!r}")
    if not _SAFE_PATH.fullmatch(path):
        raise CredentialError(f"{what} has characters that can't be put on a credential command line")
    real = os.path.realpath(path)
    if not os.path.exists(real):
        raise CredentialError(f"{what} doesn't exist: {path}")
    if not os.path.isfile(real) or not os.access(real, os.X_OK):
        raise CredentialError(f"{what} isn't an executable file: {path}")
    for p in {path, real, os.path.dirname(path), os.path.dirname(real)}:
        if os.stat(p).st_mode & 0o002:
            raise CredentialError(f"{what} is in or is world-writable ({p}); refusing to put it in a credential hook")


def hook_argv(argv0: str | None = None, python: str | None = None) -> list[str]:
    """The argv a cloud hook runs brindle with: the running ``brindle`` script
    when that is what started us, else the interpreter with ``-m brindle`` (a
    ``python -m`` run, a test runner or a shim has no script of its own). Never
    a relative or world-writable path."""
    raw = sys.argv[0] if argv0 is None else argv0
    if raw and Path(raw).name == "brindle":
        _vet(raw, "the brindle command")
        return [raw]
    interpreter = python or sys.executable
    _vet(interpreter, "the Python interpreter")
    return [interpreter, "-P", "-m", "brindle"]    # -P: not a brindle/ in the hook's cwd


def hook_command(cloud: str, org_id: str, argv0: str | None = None, python: str | None = None) -> str:
    """The command line for ``cloud``'s hook, the executable quoted."""
    if cloud not in CLOUDS:
        raise CredentialError(f"unknown cloud {cloud}")
    if not ORG_RE.fullmatch(org_id or ""):
        raise CredentialError("invalid org id")
    argv = hook_argv(argv0, python)
    return " ".join([f'"{argv[0]}"', *argv[1:], "cloud-credential", cloud, "--org", org_id])


# -- GCP external-account file ---------------------------------------------------------------------

def gcp_audience(fed) -> str:
    return (f"//iam.googleapis.com/projects/{fed.project_number}/locations/global/"
            f"workloadIdentityPools/{fed.pool_id}/providers/{fed.provider_id}")


def write_gcp_config(org_id: str, fed, command: str) -> Path:
    """Write ``federation/<org>/gcp-credentials.json``: an external-account file
    whose credential source is the executable ``command``. Returns its path."""
    d = org_dir(org_id, create=True)
    doc = {"type": "external_account", "audience": gcp_audience(fed),
           "subject_token_type": GCP_TOKEN_TYPE, "token_url": GCP_TOKEN_URL,
           "service_account_impersonation_url": GCP_IMPERSONATION_URL.format(fed.service_account),
           "service_account_impersonation": {"token_lifetime_seconds": 900},
           "credential_source": {"executable": {"command": command, "timeout_millis": HOOK_TIMEOUT_MILLIS,
                                                "output_file": str(d / GCP_TOKEN)}}}
    path = d / GCP_CONFIG
    _write(path, json.dumps(doc))
    return path


# -- tokens ----------------------------------------------------------------------------------

def claims(token: str) -> dict:
    """The payload of a JWT (not verified: only to time refreshes and name sessions)."""
    try:
        payload = token.split(".")[1]
        got = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, AttributeError):
        return {}
    return got if isinstance(got, dict) else {}


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _fresh(issued: float | None, expires: float | None, now: float) -> bool:
    """More than REFRESH_BELOW of the credential's life remains."""
    if issued is None or expires is None or expires <= issued:
        return False
    return (expires - now) / (expires - issued) > REFRESH_BELOW


def _valid(expires: float | None, now: float) -> bool:
    return expires is not None and expires > now


def session_name(member: str, fid: str) -> str:
    """The AWS role session name: who and which device (a hash of the sign-in family)."""
    who = re.sub(r"[^\w+=,.@-]", "-", member or "unknown", flags=re.ASCII)[:24]
    return f"brindle-{who}-{hashlib.sha256((fid or '').encode()).hexdigest()[:8]}"


# -- AWS: AssumeRoleWithWebIdentity -----------------------------------------------------------------

Sts = Callable[[str, dict], "tuple[int, bytes]"]


def sts_post(url: str, params: dict) -> tuple[int, bytes]:
    """An unsigned form POST to STS (no AWS credentials are needed to assume a
    role with a web identity); redirects are not followed."""
    try:
        airgap.guard(url, "AWS STS request")
    except airgap.AirGapError as e:
        raise Offline(str(e)) from None
    opener = urllib.request.OpenerDirector()
    for h in (urllib.request.HTTPSHandler(), urllib.request.HTTPErrorProcessor(),
              urllib.request.HTTPDefaultErrorHandler()):
        opener.add_handler(h)
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener.open(req, timeout=STS_TIMEOUT) as resp:
            return resp.status, resp.read(MAX_RESPONSE)
    except urllib.error.HTTPError as e:
        return e.code, e.read(MAX_RESPONSE)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Offline(f"couldn't reach AWS STS: {getattr(e, 'reason', e)}") from None


def _xml(raw: bytes) -> dict[str, str]:
    """Every leaf of an STS answer by local tag name (namespaces dropped)."""
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise CredentialError("AWS STS sent an unexpected answer")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise CredentialError("AWS STS sent an unreadable answer") from None
    return {el.tag.rsplit("}", 1)[-1]: (el.text or "").strip() for el in root.iter() if len(el) == 0}


def _iso(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def assume_role(sts: Sts, region: str, role_arn: str, id_token: str, now: float) -> dict:
    """Exchange the ID token for the role's credentials; the cache document."""
    c = claims(id_token)
    status, raw = sts(f"https://sts.{region}.amazonaws.com/", {
        "Action": "AssumeRoleWithWebIdentity", "Version": "2011-06-15", "RoleArn": role_arn,
        "RoleSessionName": session_name(str(c.get("member") or c.get("sub") or ""), str(c.get("fid") or "")),
        "WebIdentityToken": id_token, "DurationSeconds": str(AWS_SESSION_SECONDS)})
    leaves = _xml(raw)
    if status != 200:
        code = re.sub(r"[^A-Za-z0-9]", "", leaves.get("Code", ""))[:64] or f"HTTP{status}"
        raise CredentialError(f"AWS refused the role ({code}); ask an admin to check the role's trust policy")
    try:
        expires = _iso(leaves["Expiration"])
        doc = {"AccessKeyId": leaves["AccessKeyId"], "SecretAccessKey": leaves["SecretAccessKey"],
               "SessionToken": leaves["SessionToken"], "Expiration": leaves["Expiration"],
               "issued_at": now, "expires_at": expires, "arn": leaves.get("Arn", "")}
    except (KeyError, ValueError):
        raise CredentialError("AWS STS sent an incomplete answer") from None
    return doc


def aws_output(doc: dict) -> str:
    """The ``credential_process`` contract."""
    return json.dumps({"Version": 1, "AccessKeyId": doc["AccessKeyId"],
                       "SecretAccessKey": doc["SecretAccessKey"], "SessionToken": doc["SessionToken"],
                       "Expiration": doc["Expiration"]})


def aws_identity(org_id: str) -> tuple[str, str] | None:
    """(account, role name) of the role brindle last assumed for ``org_id``,
    read from its own cache (no AWS CLI); None when it hasn't assumed one."""
    try:
        doc = _read_json(aws_cache_path(org_id))
    except CredentialError:
        return None
    m = re.fullmatch(r"arn:aws[a-z-]*:sts::(\d{12}):assumed-role/([^/]+)/.+", str((doc or {}).get("arn", "")))
    return (m.group(1), m.group(2)) if m else None


# -- the command ----------------------------------------------------------------------------------

@contextlib.contextmanager
def mint_lock(d: Path, timeout: float = LOCK_TIMEOUT):
    """An exclusive lock on the org's directory, so concurrent hooks mint once."""
    old = os.umask(0o077)
    try:
        fd = os.open(d / LOCK_FILE, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    finally:
        os.umask(old)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise CredentialError("timed out waiting for another brindle process to fetch credentials") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _aws_route(org_id: str, client, store):
    """(region, role ARN) of the org's federated Bedrock route, from its policy."""
    from brindle.pro import team_policy

    policy = team_policy.load_cached(org_id)
    if policy is None:
        try:
            policy = team_policy.fetch_policy(org_id, client, store)
        except Exception as e:  # noqa: BLE001 - offline, or the policy can't be had
            raise Offline(f"couldn't read the org policy ({e})") from None
    claude = policy.agent_setup.claude if policy.agent_setup else None
    if claude is None or not claude.federated or claude.route != "bedrock":
        raise CredentialError("this org's policy doesn't use federated AWS credentials; run `brindle login`")
    return claude.aws.region, claude.aws.federation.role_arn


def device_headers() -> dict[str, str]:
    """The client headers on a mint. The server keys the device by the token
    family, so no device id is sent."""
    import brindle

    return {"X-Brindle-Version": brindle.__version__}


def _personal(store, client, org_id: str) -> bool:
    """Whether ``org_id`` is the signed-in user's own org (no team org is
    selected and the stored entitlement is for it)."""
    from brindle.pro import license

    try:
        if (store.load() or {}).get("org_id"):
            return False
        ent = license.current(store=store, client=client)
    except Exception:  # noqa: BLE001 - no usable entitlement: treat it as a team org
        return False
    return ent.org_id == org_id and "team" not in ent.features and ent.plan not in ("team", "enterprise")


def _mint(client, store, org_id: str, cloud: str) -> str:
    """An ID token for ``cloud`` from PawDelta. A revoked or too-old sign-in
    signs the family out on the server, then deletes the stored credentials."""
    from brindle.pro import auth

    path = "/me/federation/token" if _personal(store, client, org_id) else f"/orgs/{org_id}/federation/token"
    try:
        status, body = auth.authed(client, store, "POST", path,
                                   auth.JSONBody({"cloud": cloud}), headers=device_headers())
    except auth.AuthError as e:
        if isinstance(e, auth.TransportError):
            raise Offline(str(e)) from None
        if e.revoked or e.code == "not_logged_in":
            note_reauth(org_id)
            raise CredentialError(LOGIN_LINE) from None
        raise CredentialError(str(e)) from None
    if status == 200:
        token = body.get("id_token")
        if not isinstance(token, str) or token.count(".") != 2 or len(token) > MAX_TOKEN_CHARS:
            raise CredentialError("brindle Pro sent an unusable ID token")
        return token
    if status == 404:
        raise CredentialError(NOT_OFFERED)
    err = auth._error(status, body)
    if status == 401 and err.revoked:
        note_reauth(org_id)
        auth.logout(client, store)  # revoke the family first, or it lingers in the Devices list
        raise CredentialError(LOGIN_LINE)
    raise CredentialError(f"brindle Pro refused the credential request ({err.code})")


def _cached(cloud: str, org_id: str) -> tuple[dict | str | None, float | None, float | None]:
    """(the cached credential, when issued, when it expires)."""
    d = org_dir(org_id)
    if cloud == "aws":
        doc = _read_json(d / AWS_CACHE)
        if doc is None:
            return None, None, None
        return doc, _num(doc.get("issued_at")), _num(doc.get("expires_at"))
    if cloud == "gcp":
        doc = _read_json(d / GCP_TOKEN)
        token = doc.get("id_token") if doc else None
        if not isinstance(token, str):
            return None, None, None
        c = claims(token)
        return doc, _num(c.get("iat")), _num(doc.get("expiration_time"))
    raw = _read(d / AZURE_TOKEN, MAX_TOKEN_CHARS)
    token = raw.decode("utf-8", "replace").strip() if raw else ""
    c = claims(token)
    return (token or None), _num(c.get("iat")), _num(c.get("exp"))


def _render(cloud: str, got) -> str:
    if cloud == "aws":
        return aws_output(got)
    if cloud == "gcp":
        return json.dumps(got)
    return ""


def _exchange(cloud: str, org_id: str, token: str, client, store, sts: Sts, now: float):
    """Turn a fresh ID token into what ``cloud`` reads, and cache it."""
    d = org_dir(org_id, create=True)
    if cloud == "aws":
        region, role_arn = _aws_route(org_id, client, store)
        doc = assume_role(sts, region, role_arn, token, now)
        _write(d / AWS_CACHE, json.dumps(doc))
        return doc
    if cloud == "gcp":
        exp = claims(token).get("exp")
        if _num(exp) is None:
            raise CredentialError("brindle Pro sent an ID token without an expiry")
        doc = {"version": 1, "success": True, "token_type": GCP_TOKEN_TYPE, "id_token": token,
               "expiration_time": int(exp)}
        _write(d / GCP_TOKEN, json.dumps(doc))      # the library skips the executable until this expires
        return doc
    _write(d / AZURE_TOKEN, token)
    return token


def credential(cloud: str, org_id: str | None = None, *, client=None, store=None,
               sts: Sts | None = None, now: float | None = None) -> str:
    """What ``cloud``'s hook prints (empty for Azure, which reads a file).
    Raises :class:`CredentialError` with the line to show."""
    from brindle.pro import auth, credentials, license

    if cloud not in CLOUDS:
        raise CredentialError(f"unknown cloud {cloud}")
    store = store or credentials.default_store()
    try:
        cur = store.load() or {}
    except Exception:  # noqa: BLE001 - an unreadable store is a signed-out one
        cur = {}
    if not cur.get("refresh_token"):
        raise CredentialError(LOGIN_LINE)
    client = client or auth.Client(cur.get("base_url"))
    org_id = org_id or cur.get("org_id")
    if not org_id:
        try:  # a personal sign-in stores no org: use the entitlement's own
            org_id = license.current(store=store, client=client).org_id
        except Exception:  # noqa: BLE001 - no usable entitlement
            pass
    if not org_id:
        raise CredentialError("no organization given; pass --org ID")
    d = org_dir(org_id, create=True)
    now = time.time() if now is None else now
    with mint_lock(d):
        # Under the lock: the process that held it may have just minted.
        got, issued, expires = _cached(cloud, org_id)
        if got is not None and _fresh(issued, expires, now):
            return _render(cloud, got)
        try:
            token = _mint(client, store, org_id, cloud)
            return _render(cloud, _exchange(cloud, org_id, token, client, store, sts or sts_post, now))
        except Offline as e:
            if got is not None and _valid(expires, now):
                log.debug("offline (%s); serving the cached credential", e)
                return _render(cloud, got)
            raise CredentialError(OFFLINE_LINE) from None
        except CredentialError as e:
            if str(e) == LOGIN_LINE:
                for name in CACHE_FILES:
                    with contextlib.suppress(OSError):
                        (d / name).unlink()
            raise


def freshen_azure(token_file: str | os.PathLike, now: float | None = None) -> bool:
    """Rewrite the Azure federated token file at ``token_file`` (the
    ``federation/<org>/azure-token`` a pane's env names) when under
    REFRESH_BELOW of its life remains. Azure has no credential hook, so a pane
    launch and :class:`AzureRefresher` do this. Never raises; True when the
    file is fresh afterwards."""
    path = Path(token_file)
    org_id = path.parent.name
    if path.name != AZURE_TOKEN or not ORG_RE.fullmatch(org_id) or path.parent.parent != federation_root():
        return False
    now = time.time() if now is None else now
    _, issued, expires = _cached("azure", org_id)
    if _fresh(issued, expires, now):
        return True
    try:
        credential("azure", org_id, now=now)
    except CredentialError as e:
        log.debug("couldn't freshen the Azure token: %s", e)
        return False
    return True


class AzureRefresher:
    """Keeps the Azure token file fresh while the brindle session runs, so
    panes that outlive their first access token can renew it."""

    def __init__(self, token_file: str | os.PathLike, interval: float = AZURE_REFRESH_INTERVAL) -> None:
        self.token_file, self.interval = token_file, interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="brindle-azure-refresh", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while True:
            freshen_azure(self.token_file)
            if self._stop.wait(self.interval):
                return


def run(cloud: str, org_id: str | None = None, *, out: TextIO | None = None, err: TextIO | None = None,
        **kw) -> int:
    """The ``brindle cloud-credential`` command: the hook's output on ``out``,
    one line on ``err`` and exit status 1 when there is none."""
    out, err = out or sys.stdout, err or sys.stderr
    try:
        text = credential(cloud, org_id, **kw)
    except CredentialError as e:
        err.write(f"brindle cloud-credential: {e}\n")
        return 1
    if text:
        out.write(text + "\n")
    return 0


def check(cloud: str, org_id: str, **kw) -> str | None:
    """One real exchange (login uses it to verify the setup, which also warms
    the cache). None when it worked, else the reason."""
    out, err = io.StringIO(), io.StringIO()
    if run(cloud, org_id, out=out, err=err, **kw) == 0:
        return None
    return err.getvalue().strip().removeprefix("brindle cloud-credential: ") or "no credential"


__all__ = ["CredentialError", "check", "clear_all", "credential", "hook_command", "run"]
