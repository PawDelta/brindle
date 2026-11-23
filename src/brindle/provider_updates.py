"""Daily release checks and idle-only provider CLI updates."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from urllib.request import urlopen

from packaging.version import InvalidVersion, Version

from brindle import agents, airgap, bus, config, providers_install as installs
from brindle.db import DB
from brindle.pro import analysis_policy, auth, license
from brindle.service.dto import InboxActionDTO, InboxChoiceDTO, InboxItemDTO
from brindle.service.errors import ServiceError

log = logging.getLogger(__name__)
SETTING = "provider_cli_updates"
MODES = ("automatic", "ask", "off")
DEFAULT_POLICY = {"mode": "automatic", "locked": False}
DAY = 24 * 60 * 60
REGISTRY = "https://registry.npmjs.org"
RELEASE_CHECK_WAIT = 5.0
VERSION = re.compile(r"\d+\.\d+\.\d+")


def org_setting(policy: dict) -> dict:
    value = policy.get(SETTING, DEFAULT_POLICY)
    if not valid_policy(value):
        raise ValueError("Invalid provider CLI update policy")
    return value


def valid_policy(value: object) -> bool:
    return (isinstance(value, dict) and value.get("mode") in MODES
            and isinstance(value.get("locked"), bool))


def can_govern(ent: license.Entitlement) -> bool:
    return ent.plan == "enterprise" and "analysis_docs_enterprise" in ent.features


def setting(*, refresh: bool = False) -> dict:
    """The active org's lock, then the person's choice, then the org default."""
    try:
        ent = license.current(refresh=False)
    except license.LicenseError:
        ent = None
    org = DEFAULT_POLICY
    if ent and ent.org_id and can_govern(ent):
        if refresh:
            from brindle.pro import analysis_docs

            try:
                client, store = analysis_docs._session()
                analysis_policy.fetch(ent.org_id, client, store)
            except Exception:  # Account policy is optional for the member setting.
                analysis_policy.provider_updates_read_failed(ent.org_id, ent.sub)
                log.debug("Couldn't read the organization provider update policy")
            return setting()  # use the same cache as Settings, for the current identity
        try:
            org = org_setting(analysis_policy.load_provider_updates_cached(ent.org_id, ent.sub))
        except Exception:
            log.debug("Couldn't read the cached organization provider update policy")
    local = config.user_settings().get(SETTING)
    if not org["locked"] and local in MODES:
        return {"mode": local, "locked": False}
    return org


def setting_fields(*, refresh: bool = False) -> dict:
    effective = setting(refresh=refresh)
    return {SETTING: effective["mode"], f"{SETTING}_locked": effective["locked"]}


def latest_version(provider: str, installed: Version) -> str:
    package = "@openai%2fcodex" if provider == "codex" else (
        "@opencode%2fcli" if installed.major >= 2 else "opencode-ai")
    with urlopen(f"{REGISTRY}/{package}/dist-tags", timeout=RELEASE_CHECK_WAIT) as response:
        latest = json.loads(response.read(65536))["latest"]
    if not isinstance(latest, str) or not VERSION.fullmatch(latest):
        raise ValueError("Invalid registry release version")
    return latest


def _path(provider: str) -> str | None:
    # An explicit binary override is outside the selection managed by install().
    if installs.turned_off(provider) or os.environ.get(f"BRINDLE_{provider.upper()}_BIN"):
        return None
    return installs.found(provider)


def _live(db: DB, provider: str) -> bool:
    return any(agents.is_alive(agent) for agent in db.list_agents() if agent.provider == provider)


def _state(db: DB, provider: str):
    return db.conn.execute("SELECT * FROM provider_updates WHERE provider=?", (provider,)).fetchone()


def _newer(latest: str, installed: str | None) -> bool:
    try:
        return bool(installed) and Version(latest) > Version(installed)
    except InvalidVersion:
        return False


def _check_release(db: DB, provider: str, now: float) -> bool:
    claimed = db.conn.execute(
        "INSERT INTO provider_updates (provider, checked_at) VALUES (?, ?) "
        "ON CONFLICT(provider) DO UPDATE SET checked_at=excluded.checked_at "
        "WHERE provider_updates.checked_at <= ?", (provider, now, now - DAY),
    ).rowcount
    if not claimed:
        return False
    path = _path(provider)
    installed = installs.probe_version(path) if path else None
    target = None
    if installed:
        try:
            latest = latest_version(provider, Version(installed))
        except (OSError, ValueError, KeyError, TypeError):
            log.debug("Couldn't check the latest %s release", provider)
            return False
        if _newer(latest, installed):
            target = latest
    with db.tx() as c:
        changed = c.execute(
            "UPDATE provider_updates SET target=?, state='pending' "
            "WHERE provider=? AND (target IS NOT ? OR (target IS NOT NULL AND state='failed'))",
            (target, provider, target),
        ).rowcount
        if changed and target is not None:
            c.execute("INSERT INTO inbox_occurrences (source, generation) VALUES (?,1) "
                      "ON CONFLICT(source) DO UPDATE SET generation=generation+1", (f"provider-update:{provider}",))
    if changed:
        bus.publish(db, "inbox.changed", provider=provider)
    return target is not None


def _install_ready(db: DB, provider: str, mode: str) -> bool:
    if installs.running(provider) or _live(db, provider):
        return False
    # Re-read under the same gate as launches and all other update attempts.
    row = _state(db, provider)
    if row is None or row["target"] is None or row["state"] == "failed" or mode == "off":
        return False
    if mode == "ask" and row["state"] != "approved":
        return False
    path = _path(provider)
    if path is None:
        return False
    target = row["target"]
    if not _newer(target, installs.probe_version(path)):
        db.conn.execute("UPDATE provider_updates SET target=NULL WHERE provider=?", (provider,))
        bus.publish(db, "inbox.changed", provider=provider)
        return False
    if airgap.enabled() or setting()["mode"] != mode or _live(db, provider):
        return False
    try:
        result = installs.install(provider, lambda line: None, update=(path, target))
    except (installs.InstallError, OSError):
        db.conn.execute("UPDATE provider_updates SET state='failed' WHERE provider=?", (provider,))
        bus.publish(db, "inbox.changed", provider=provider)
        return False
    agents._model_checker.clear()
    db.conn.execute("UPDATE provider_updates SET target=NULL WHERE provider=?", (provider,))
    version = result["version"]
    bus.publish(db, "provider.updated", provider=provider, version=version,
                text=f"{installs.NAMES[provider]} updated to {version}")
    return True


def sweep(db: DB, *, clock=time.time, refresh_policy: bool = False) -> None:
    if airgap.enabled():
        return
    mode = setting(refresh=refresh_policy)["mode"]
    if mode == "off":
        return
    now = clock()
    rows = {row["provider"]: row for row in db.conn.execute("SELECT * FROM provider_updates")}
    due = [p for p in installs.UPDATE_PROVIDERS if p not in rows or rows[p]["checked_at"] <= now - DAY]
    ready = [p for p, row in rows.items() if p in installs.UPDATE_PROVIDERS
             and row["target"] and (row["state"] == "approved" or
                                    (row["state"] == "pending" and mode == "automatic"))
             and not _live(db, p)]
    if not due and not ready:
        return
    # Read live org policy before checking releases or acting on a deferred update.
    if not refresh_policy:
        mode = setting(refresh=True)["mode"]
        if mode == "off":
            return
    checked = [p for p in due if _check_release(db, p, now)]
    for provider in dict.fromkeys(checked + ready):
        try:
            with installs.update_gate(provider, updating=True):
                updated = _install_ready(db, provider, mode)
        except BlockingIOError:
            continue
        if updated:
            auth.refresh_after_provider_update()


def inbox_items(db: DB) -> list[InboxItemDTO]:
    rows = list(db.conn.execute(
        "SELECT u.*, COALESCE(o.generation, 0) AS generation FROM provider_updates u "
        "LEFT JOIN inbox_occurrences o ON o.source='provider-update:' || u.provider WHERE u.target IS NOT NULL",
    ))
    if not rows or airgap.enabled():
        return []
    mode = setting_fields()[SETTING]
    if mode == "off":
        return []
    out = []
    for row in rows:
        provider, target, state = row["provider"], row["target"], row["state"]
        if state == "failed":
            out.append(InboxItemDTO(kind="provider_update", request_id=f"{provider}:{target}",
                                    reason=f"{installs.NAMES[provider]} update failed",
                                    detail=f"brindle kept the previous version. The update to {target} did not complete."))
        elif mode == "ask" and state == "pending":
            request = f"{provider}:{target}:{row['generation']}"
            out.append(InboxItemDTO(kind="provider_update", request_id=request,
                                    reason=f"{installs.NAMES[provider]} update available", detail=target,
                                    action=InboxActionDTO(method="POST", path=f"/v1/inbox/{request}/provider-update"),
                                    choices=[InboxChoiceDTO(id="update", label="Update")]))
    return out


def approve(db: DB, request: str) -> dict:
    provider, _, version = request.partition(":")
    target, _, generation = version.partition(":")
    if provider not in installs.UPDATE_PROVIDERS or airgap.enabled() or setting_fields()[SETTING] != "ask":
        raise ServiceError("conflict", "This update is no longer awaiting approval.", 409)
    if not db.conn.execute(
        "UPDATE provider_updates SET state='approved' WHERE provider=? AND target=? AND state='pending' "
        "AND CAST(COALESCE((SELECT generation FROM inbox_occurrences WHERE source=?), 0) AS TEXT)=?",
        (provider, target, f"provider-update:{provider}", generation),
    ).rowcount:
        raise ServiceError("conflict", "This update is no longer awaiting approval.", 409)
    bus.publish(db, "inbox.changed", provider=provider)
    return {"message": f"{installs.NAMES[provider]} will update when it is idle."}
