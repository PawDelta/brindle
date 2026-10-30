"""Org governance commands (brindle Enterprise): the managed device file, the
device tokens behind it, and the offline policy export.

``brindle org device-file`` asks the server for a managed device file
(``POST /orgs/{id}/managed-devices``), checks it with
:func:`brindle.pro.managed.verify_file` and writes ``managed.json``. It never
installs the file: an MDM puts it in :func:`managed.system_dir`.
``brindle account org policy`` shows the verified policy and where it came
from, and with ``--export`` writes the signed offline policy export to
``.brindle/policy.json``.

The logic takes ``call(method, path, body) -> (status, body)``, so it is
independent of the transport; :func:`session` builds the signed-in one. The
device token lives only in the file this writes (mode 0600) and is never
printed.
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
from pathlib import Path

from brindle.pro import auth, managed

EXPORT_MIN_DAYS, EXPORT_MAX_DAYS, EXPORT_DEFAULT_DAYS = 1, 90, 30


class OrgCliError(Exception):
    """A command that can't proceed; the message is for the admin."""


def session(repo_root: str | None, org: str | None = None, base: str | None = None, transport=None):
    """``(call, org_id)`` for the signed-in account: ``call(method, path, body)``
    sends JSON with the account's access token."""
    from brindle.pro.account import ProAccount

    acct = ProAccount(repo_root, transport=transport)
    client, org_id = acct._client(base), acct._team_org(org)

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        return auth.authed(client, acct.store, method, path, auth.JSONBody(body) if body is not None else None)

    return call, org_id


def _path(org_id: str, tail: str = "") -> str:
    return f"/orgs/{urllib.parse.quote(org_id, safe='')}{tail}"


def _ok(status: int, body: dict) -> dict:
    if status != 200 or not isinstance(body, dict):
        raise OrgCliError(str(auth._error(status, body if isinstance(body, dict) else {})))
    return body


def audit_sink(url: str | None) -> dict | None:
    """The ``audit_sink`` request for ``url``: ``https://`` is an https sink;
    ``udp://``, ``tcp://`` and ``tls://`` are syslog collectors."""
    if not url:
        return None
    scheme = urllib.parse.urlparse(url).scheme
    if scheme == "https":
        return {"kind": "https", "url": url}
    if scheme in ("udp", "tcp", "tls"):
        return {"kind": "syslog", "url": url}
    raise OrgCliError("--audit-sink must be an https:// URL, or a udp://, tcp:// or tls:// syslog collector")


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` (mode 0600) through a unique temp file in the
    same directory, replaced atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _target(out: str | None) -> Path:
    """``-o``: a directory (or nothing: the current one) gets ``managed.json``;
    anything else is the file's own path."""
    p = Path(out) if out else Path.cwd()
    return p / managed.FILE if (out is None or p.is_dir()) else p


def create_device_file(call, org_id: str, *, name: str | None = None, allow_airgap: bool = False,
                       sink: str | None = None, out: str | None = None, now: float | None = None) -> list[str]:
    """Issue a managed device file, verify it and write it. The lines to show."""
    req: dict = {"name": name or "managed device", "allow_airgap": allow_airgap}
    if audit_sink(sink):
        req["audit_sink"] = audit_sink(sink)
    answer = _ok(*call("POST", _path(org_id, "/managed-devices"), req))
    data = json.dumps({k: answer.get(k) for k in ("managed", "policy_body", "device_id")}).encode()
    try:
        device = managed.verify_file(data, now=now)
    except Exception as e:   # noqa: BLE001 - ManagedError, SignedError, PolicyUnavailable
        raise OrgCliError(f"the server's managed device file did not verify ({e}); nothing was written") from None
    if device.org_id != org_id:
        raise OrgCliError("the managed device file is bound to another org; nothing was written")
    path = _target(out)
    _write_private(path, data)
    lines = [f"Wrote the managed device file for device {device.device_id} (org {device.org_id}) to {path}.",
             "It holds the device token, shown only here: keep it private (mode 0600).",
             f"Have your MDM install it as {managed.system_dir() / managed.FILE}; brindle does not install it itself.",
             f"Airgap mode on the device: {'allowed' if device.allow_airgap else 'not allowed'}."]
    if device.audit_sink:
        lines.append(f"Audit sink: {device.audit_sink['kind']} {device.audit_sink['url']}")
    if device.snapshot_expired:
        lines.append("Note: the signed policy in this file has already lapsed; the device fetches a "
                     "fresh policy with its device token.")
    return lines


def list_devices(call, org_id: str) -> list[str]:
    devices = _ok(*call("GET", _path(org_id, "/managed-devices"))).get("devices") or []
    if not devices:
        return ["No managed devices."]
    return [f"{d.get('device_id', '')}  {d.get('status', ''):<8} airgap={'yes' if d.get('allow_airgap') else 'no':<3} "
            f"{auth._sanitize(d.get('name', ''), 100)}" for d in devices if isinstance(d, dict)]


def revoke_device(call, org_id: str, device_id: str) -> list[str]:
    _ok(*call("DELETE", _path(org_id, f"/managed-devices/{urllib.parse.quote(device_id, safe='')}")))
    return [f"Revoked the device token of {device_id}."]


def export_policy(call, org_id: str, repo_root: str | None, days: int = EXPORT_DEFAULT_DAYS) -> list[str]:
    """Write the org's signed offline policy export to ``.brindle/policy.json``."""
    if not isinstance(days, int) or isinstance(days, bool) or not EXPORT_MIN_DAYS <= days <= EXPORT_MAX_DAYS:
        raise OrgCliError(f"--days must be a whole number from {EXPORT_MIN_DAYS} to {EXPORT_MAX_DAYS}")
    from brindle import airgap

    answer = _ok(*call("POST", _path(org_id, "/policy/offline"), {"days": days}))
    if not isinstance(answer.get("token"), str) or not isinstance(answer.get("body"), dict):
        raise OrgCliError("the server's offline policy export is incomplete; nothing was written")
    path = airgap.policy_path(repo_root)
    _write_private(path, json.dumps({"token": answer["token"], "body": answer["body"]}, indent=2).encode())
    return [f"Wrote the offline policy export for org {org_id} (valid {days} days) to {path}."]


def run_device_file(repo_root: str | None, *, org: str | None, name: str | None, allow_airgap: bool,
                    sink: str | None, out: str | None, list_: bool, revoke: str | None, echo=print) -> int:
    """``brindle org device-file``: the exit code."""
    try:
        if list_ and revoke:
            raise OrgCliError("--list and --revoke can't be used together")
        call, org_id = session(repo_root, org)
        if list_:
            lines = list_devices(call, org_id)
        elif revoke:
            lines = revoke_device(call, org_id, revoke)
        else:
            lines = create_device_file(call, org_id, name=name, allow_airgap=allow_airgap, sink=sink, out=out)
    except (OrgCliError, auth.AuthError, OSError) as e:
        echo(f"brindle org device-file: {e}")
        return 1
    for line in lines:
        echo(line)
    return 0


def run_export(repo_root: str | None, *, org: str | None, days: int | None, base: str | None = None,
               transport=None, echo=print) -> int:
    """``brindle account org policy --export``: the exit code."""
    try:
        if days is not None and not EXPORT_MIN_DAYS <= days <= EXPORT_MAX_DAYS:
            raise OrgCliError(f"--days must be a whole number from {EXPORT_MIN_DAYS} to {EXPORT_MAX_DAYS}")
        call, org_id = session(repo_root, org, base, transport)
        lines = export_policy(call, org_id, repo_root, EXPORT_DEFAULT_DAYS if days is None else days)
    except (OrgCliError, auth.AuthError, OSError) as e:
        echo(f"brindle account: {e}")
        return 1
    for line in lines:
        echo(line)
    return 0


def context(repo_root: str | None, *, store=None, client=None, now: float | None = None):
    """The shared governance context, using the CLI's account transport."""
    from brindle.pro import org_context, team_policy

    def member():
        return team_policy.ProPolicy(repo_root or "", store=store, client=client).policy()

    return org_context.OrgContext.resolve(repo_root=repo_root, now=now, providers=org_context.Providers(
        policy=member))
