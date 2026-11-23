from pathlib import Path
from types import SimpleNamespace

import pytest

from brindle import agents, config, provider_updates as updates, providers_install as installs, tmux
from brindle.daemon import org_settings
from brindle.db import Agent, Workspace
from brindle.model_availability import Catalog, ModelAvailabilityChecker, provisioning_context
from brindle.pro import analysis_docs, analysis_policy, auth, license
from brindle.profiles import Profile
from brindle.service import inbox
from test_provider_install import _send


@pytest.fixture
def machine(monkeypatch, tmp_path, db):
    system = tmp_path / "bin"
    system.mkdir()
    monkeypatch.setenv("PATH", str(system))
    for provider in installs.PROVIDERS:
        monkeypatch.delenv(f"BRINDLE_{provider.upper()}_BIN", raising=False)
    state = SimpleNamespace(now=1000.0, latest="0.171.0")

    def binary(path, version):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(version)
        path.chmod(0o755)

    for provider in ("codex", "claude", "antigravity"):
        binary(system / installs.CLI[provider], "0.170.0")

    monkeypatch.setattr(installs, "probe_version", lambda path: Path(path).read_text())
    monkeypatch.setattr(updates, "latest_version", lambda provider, installed: state.latest)
    monkeypatch.setattr(auth, "refresh_after_provider_update", lambda: None)
    monkeypatch.setattr(installs, "_run_installer", lambda provider, command, progress, env:
                        binary(Path(env["CODEX_INSTALL_DIR"]) / "codex", env["CODEX_RELEASE"]))
    state.sweep = lambda: updates.sweep(db, clock=lambda: state.now)
    state.installed = lambda: installs.probe_version(installs.found("codex"))
    state.original = system / "codex"
    return state


def test_updates_idle_codex_and_invalidates_cached_catalog(db, machine, monkeypatch):
    def discover(request, limit):
        models = {"new"} if machine.installed() == "0.171.0" else {"old"}
        return Catalog(frozenset(models), "codex")

    checker = ModelAvailabilityChecker(discover=discover, clock=lambda: machine.now)
    monkeypatch.setattr(agents, "_model_checker", checker)
    profile = Profile("codex", "", "codex", "")

    def new_model():
        return checker.first_listed(profile, ["new"], effective_env={}, cwd=".",
                                    account_identity="person", catalog_context="catalog")

    def launcher_model():
        return launcher.first_listed(profile, ["new"], effective_env={}, cwd=".",
                                     account_identity="person", catalog_context=provisioning_context("codex", ".", {}))

    launcher = ModelAvailabilityChecker(discover=discover, clock=lambda: machine.now)
    assert new_model() is None
    assert launcher_model() is None
    machine.sweep()
    assert machine.installed() == "0.171.0"
    assert new_model() == "new"
    assert launcher_model() == "new"
    assert machine.original.read_text() == "0.170.0"
    assert db.conn.execute("SELECT COUNT(*) FROM events WHERE kind='provider.updated'").fetchone()[0] == 1

    machine.latest = "0.172.0"
    machine.sweep()
    assert machine.installed() == "0.171.0"
    machine.now += updates.DAY
    machine.sweep()
    assert machine.installed() == "0.172.0"


def test_worker_appearing_during_release_check_defers_update(db, machine, monkeypatch, tmp_path):
    path = str(tmp_path)
    ws = Workspace("ws", path, "main", "main", "main", "main", path, 9000, "session", machine.now)
    db.add_workspace(ws)
    worker = Agent("worker", ws.id, "developer", "codex", None, "assign", "idle", "@1", None, machine.now)
    panes = {"@1"}
    monkeypatch.setattr(tmux, "window_alive", lambda window, **kwargs: window in panes)

    def latest(provider, installed):
        db.add_agent(worker)
        return machine.latest

    monkeypatch.setattr(updates, "latest_version", latest)
    machine.sweep()
    assert machine.installed() == "0.170.0"
    panes.clear()
    machine.sweep()
    assert machine.installed() == "0.171.0"


def test_update_cannot_overtake_a_worker_launch(db, machine, monkeypatch, tmp_path):
    path = str(tmp_path)
    ws = Workspace("ws", path, "main", "main", "main", "main", path, 9000, "session", machine.now)
    db.add_workspace(ws)
    worker = Agent("worker", ws.id, "developer", "codex", None, "assign", "starting", "", None,
                   machine.now, config_home="")
    db.add_agent(worker)

    def launching(*args, **kwargs):
        machine.sweep()
        assert machine.installed() == "0.170.0"

    monkeypatch.setattr(agents, "_launch_resolved", launching)
    agents._launch_checked(db, worker, ws, prompt=None, resume=None, watch_pane=False)
    machine.sweep()
    assert machine.installed() == "0.171.0"


def test_self_updaters_are_untouched(db, machine):
    installs.turn_off("codex")
    for provider in ("claude", "antigravity"):
        assert installs.found(provider)
    machine.sweep()
    checked = {r["provider"] for r in db.conn.execute("SELECT provider FROM provider_updates")}
    assert checked.isdisjoint({"claude", "antigravity"})


def test_off_skips_provider_checks(db, machine):
    config.set_user(updates.SETTING, "off")
    machine.sweep()
    assert db.conn.execute("SELECT COUNT(*) FROM provider_updates").fetchone()[0] == 0
    assert machine.installed() == "0.170.0"


def test_ask_queues_an_inbox_update(db, machine):
    config.set_user(updates.SETTING, "ask")
    machine.sweep()
    assert machine.installed() == "0.170.0"
    [item] = [item for item in inbox.items(db, None, snapshot_rows=[]) if item.kind == "provider_update"]
    assert [choice.id for choice in item.choices] == ["update"]
    assert _send("POST", item.action.path)[0] == 200
    assert _send("POST", item.action.path)[0] == 409
    machine.sweep()
    assert machine.installed() == "0.171.0"
    assert updates.inbox_items(db) == []


def test_org_lock_overrides_member_and_refuses_personal_edit(db, machine, monkeypatch):
    ent = SimpleNamespace(org_id="org_test", sub="person", role="admin", plan="enterprise",
                          features=["analysis_docs_enterprise"])
    monkeypatch.setattr(license, "current", lambda **kwargs: ent)
    monkeypatch.setattr(org_settings, "_docs_entitlement", lambda repo: ent)
    served = {"switches": {}, "groups": {}}

    def policy(method, tail, body=None, **kwargs):
        if method == "PUT":
            served.update(body)
        return served

    monkeypatch.setattr(org_settings, "docs_org_api", policy)
    monkeypatch.setattr(analysis_policy, "fetch", lambda *args: served)
    analysis_policy.save_cached(ent.org_id, served)
    status, settings = _send("GET", "/v1/user-settings")
    assert status == 200
    assert settings[updates.SETTING] == "automatic" and not settings["provider_cli_updates_locked"]

    config.set_user(updates.SETTING, "ask")
    assert _send("GET", "/v1/user-settings")[1][updates.SETTING] == "ask"
    status, _ = _send("PUT", "/v1/org-settings", {
        "org_id": ent.org_id, "actor_sub": ent.sub, "name": updates.SETTING,
        "previous": {"mode": "automatic", "locked": False}, "value": {"mode": "off", "locked": True},
    })
    assert status == 200
    ent.role = "member"
    machine.sweep()
    assert machine.installed() == "0.170.0"
    status, settings = _send("GET", "/v1/user-settings")
    assert status == 200
    assert settings[updates.SETTING] == "off" and settings["provider_cli_updates_locked"]
    assert _send("PATCH", "/v1/user-settings", {updates.SETTING: "ask"})[0] == 403

    ent.plan = "team"
    settings = _send("GET", "/v1/user-settings")[1]
    assert settings[updates.SETTING] == "ask" and not settings["provider_cli_updates_locked"]
    assert _send("PATCH", "/v1/user-settings", {updates.SETTING: "automatic"})[0] == 200
    ent.role = "admin"
    assert not _send("GET", "/v1/org-settings")[1]["can_edit_org"]
    status, _ = _send("PUT", "/v1/org-settings", {
        "org_id": ent.org_id, "actor_sub": ent.sub, "name": updates.SETTING,
        "previous": {"mode": "off", "locked": True}, "value": {"mode": "ask", "locked": True},
    })
    assert status == 403


def test_failed_staged_install_preserves_old_version_and_one_notice(db, machine, monkeypatch):
    def broken(provider, command, progress, env):
        binary = Path(env["CODEX_INSTALL_DIR"]) / "codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("0.0.0")

    monkeypatch.setattr(installs, "_run_installer", broken)
    machine.sweep()
    machine.now += updates.DAY
    machine.sweep()
    assert machine.installed() == "0.170.0"
    [notice] = updates.inbox_items(db)
    assert notice.action is None
    assert not list((config.brindle_home() / "provider-clis" / "codex").iterdir())


def test_refused_org_policy_leaves_updates_unlocked(db, machine, monkeypatch):
    ent = SimpleNamespace(org_id="org_refused", sub="person", plan="enterprise",
                          features=["analysis_docs_enterprise"])
    monkeypatch.setattr(license, "current", lambda **kwargs: ent)
    monkeypatch.setattr(analysis_docs, "_session", lambda: (object(), object()))
    monkeypatch.setattr(auth, "authed", lambda *args: (403, {"error": "forbidden"}))

    policy = {
        "switches": {}, "groups": {}, updates.SETTING: {"mode": "automatic", "locked": True},
    }
    analysis_policy.save_cached(ent.org_id, policy)
    config.set_user(updates.SETTING, "ask")
    machine.sweep()
    assert machine.installed() == "0.170.0"
    [item] = updates.inbox_items(db)
    assert item.action is not None
    status, settings = _send("GET", "/v1/user-settings")
    assert status == 200
    assert settings[updates.SETTING] == "ask" and not settings["provider_cli_updates_locked"]

    assert _send("PATCH", "/v1/user-settings", {updates.SETTING: "off"})[0] == 200
    config.forget_user(updates.SETTING)
    assert updates.setting() == {"mode": "automatic", "locked": False}
    assert analysis_policy.load_cached(ent.org_id) == policy

    monkeypatch.setattr(auth, "authed", lambda *args: (200, policy))
    assert updates.setting(refresh=True)["locked"]
    assert _send("GET", "/v1/user-settings")[1]["provider_cli_updates_locked"]


def test_older_org_policy_success_cannot_restore_lock_after_a_refusal(db, monkeypatch):
    ent = SimpleNamespace(org_id="org_overlap", sub="member", plan="enterprise",
                          features=["analysis_docs_enterprise"])
    policy = {"switches": {}, "groups": {}, updates.SETTING: {"mode": "automatic", "locked": True}}
    analysis_policy.save_cached(ent.org_id, policy)
    config.set_user(updates.SETTING, "ask")
    monkeypatch.setattr(license, "current", lambda **kwargs: ent)
    monkeypatch.setattr(analysis_docs, "_session", lambda: (object(), object()))
    monkeypatch.setattr(auth, "authed", lambda *args: (200, policy))
    save = analysis_policy.save_cached

    def save_after_refusal(org, response):
        with monkeypatch.context() as active:
            active.setattr(auth, "authed", lambda *args: (403, {"error": "forbidden"}))
            assert updates.setting(refresh=True) == {"mode": "ask", "locked": False}
        save(org, response)

    with monkeypatch.context() as older:
        older.setattr(analysis_policy, "save_cached", save_after_refusal)
        assert updates.setting(refresh=True) == {"mode": "ask", "locked": False}

    ent.sub = "other_member"
    assert updates.setting(refresh=True)["locked"]
    ent.sub = "member"
    settings = _send("GET", "/v1/user-settings")[1]
    assert settings[updates.SETTING] == "ask" and not settings["provider_cli_updates_locked"]
    assert updates.setting(refresh=True)["locked"]


@pytest.mark.parametrize("mode", [
    "automatic",
    pytest.param("ask", marks=pytest.mark.variant("a renewed offer needs a fresh Inbox approval")),
])
def test_same_failed_release_waits_for_live_worker_after_next_check(db, machine, monkeypatch, tmp_path, mode):
    config.set_user(updates.SETTING, mode)
    installer = installs._run_installer

    def unavailable(*args, **kwargs):
        raise installs.InstallError("download unavailable")

    monkeypatch.setattr(installs, "_run_installer", unavailable)
    machine.sweep()
    if mode == "ask":
        [original] = updates.inbox_items(db)
        assert _send("POST", original.action.path)[0] == 200
        machine.sweep()
    [failure] = updates.inbox_items(db)
    assert failure.action is None
    monkeypatch.setattr(installs, "_run_installer", installer)

    path = str(tmp_path)
    ws = Workspace("ws", path, "main", "main", "main", "main", path, 9000, "session", machine.now)
    db.add_workspace(ws)
    db.add_agent(Agent("worker", ws.id, "developer", "codex", None, "assign", "idle", "@1", None, machine.now))
    live = True
    monkeypatch.setattr(tmux, "window_alive", lambda *args, **kwargs: live)
    machine.now += updates.DAY
    machine.sweep()
    assert machine.installed() == "0.170.0"
    if mode == "ask":
        [renewed] = updates.inbox_items(db)
        assert renewed.action is not None
        assert _send("POST", original.action.path)[0] == 409
        assert _send("POST", renewed.action.path)[0] == 200
    live = False
    machine.sweep()
    assert machine.installed() == "0.171.0"
