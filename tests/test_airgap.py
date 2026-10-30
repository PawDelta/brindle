"""Air-gap mode (brindle Enterprise): no outbound traffic, local models only.

Every brindle Pro path that would leave the machine refuses (the fake
transport is never called), delegation reaches only local profiles, the team
policy comes from an offline file, the entitlement from an offline license
installed and verified without a network, and ``brindle doctor`` says so."""
import io
import json
import time

import pytest

from brindle import airgap, doctor, policy
from brindle.config import RepoConfig, load_repo_config
from brindle.events import Event
from brindle.learning import Outcome, TaskInfo
from brindle.policy import AssignInfo
from brindle.pro import auth, credentials, license, team_policy
from brindle.pro.account import ProAccount
from brindle.pro.learning import CloudLearner
from brindle.pro.orgkey import OrgKeyUnavailable, OrgKeys
from brindle.pro.team_events import ProEvents, Spool
from brindle.pro.team_policy import ProPolicy
from brindle.profiles import Profile, _parse
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, backend, claims, fixed_identity, pro_env, sign, signing_key, token,
)

ORG = "org_gap1"
POLICY = {"org_id": ORG, "version": 3,
          "policy": {"allowed_providers": ["native"], "allowed_models": None,
                     "require_human_review": True, "max_parallel_workers": 1}}


@pytest.fixture(autouse=True)
def _reset_airgap():
    airgap.reset()
    yield
    airgap.reset()


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(airgap.ENV, "1")


def team_claims(**over):
    c = dict(org_id=ORG, plan="team", features=["learning", "team"], role="member",
             policy_version=3)
    c.update(over)
    return claims(**c)


def login(backend, entitlement_claims, **extra):
    """A logged-in member whose access token has expired, so any backend call
    would need a refresh first."""
    t = backend.issue()
    store = credentials.default_store()
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() - 1, "base_url": BASE,
                "entitlement": sign(backend.key, entitlement_claims), **extra})
    return store


def client(backend):
    return auth.Client(BASE, backend)


def native(name="local", base_url="http://localhost:11434/v1", **kw) -> Profile:
    return Profile(name=name, description="", provider="native", prompt="", api="openai",
                   model="qwen", base_url=base_url, **kw)


def hosted(provider, name="dev", **kw) -> Profile:
    return Profile(name=name, description="", provider=provider, prompt="", **kw)


def add_profile(repo, name, text):
    d = repo / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text)


def write_config(repo, **cfg):
    d = repo / ".brindle"
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg))


# -- switching it on --------------------------------------------------------------------------------


def test_off_by_default():
    assert not airgap.enabled()
    assert not airgap.enabled(RepoConfig())
    assert airgap.source() == "off"
    airgap.guard("https://pawdelta.com/api")       # nothing refused


def test_local_config_can_turn_it_on_but_not_off(tmp_path):
    write_config(tmp_path, airgap=True)
    (tmp_path / ".brindle" / "config.local.json").write_text('{"airgap": false}')
    assert load_repo_config(tmp_path).airgap
    airgap.reset()
    write_config(tmp_path, airgap=False)
    (tmp_path / ".brindle" / "config.local.json").write_text('{"airgap": true}')
    assert load_repo_config(tmp_path).airgap


# -- which endpoints are local ------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["::", "localhost.evil.com"])
def test_other_hosts_are_not_local(host):
    assert not airgap.is_local_host(host)


def test_local_urls():
    assert airgap.is_local_url("http://localhost:11434/v1")
    assert airgap.is_local_url("http://[::1]:11434/v1")
    assert airgap.is_local_url("https://10.1.2.3:8443/v1")
    assert not airgap.is_local_url("https://api.anthropic.com")
    assert not airgap.is_local_url("")
    assert not airgap.is_local_url(None)
    assert not airgap.is_local_url("http://[bad")


# -- every brindle Pro path refuses: the transport is never called ----------------------------------------


def test_login_is_refused(on, backend):
    with pytest.raises(auth.AirGapped):
        auth.login(client(backend), credentials.default_store())
    assert backend.calls == []
    assert credentials.default_store().load() is None


def test_entitlement_fetch_and_authed_calls_are_refused(on, backend):
    store = login(backend, claims())
    with pytest.raises(auth.AirGapped):
        auth.authed(client(backend), store, "GET", "/me")
    with pytest.raises(auth.AirGapped):
        auth._fetch_entitlement(client(backend), "at_x", time.time())
    with pytest.raises(auth.AirGapped):
        auth.switch_org(client(backend), store, "org_other")
    assert backend.calls == []


def test_dev_mode_trusts_no_unpinned_key(on, monkeypatch):
    monkeypatch.setenv("BRINDLE_PRO_DEV", "1")
    with pytest.raises(license.LicenseError, match="unknown key"):
        license._trusted_key("some-other-kid")


def test_license_current_never_refreshes(on, backend):
    now = int(time.time())
    # A stored entitlement that is expired but in grace: online, brindle would refresh it.
    store = login(backend, claims(iat=now - 3000, exp=now - 100))
    ent = license.current(store=store, client=client(backend), now=now)
    assert ent.in_grace and ent.org_id == "org_1"
    assert backend.calls == []
    # A stored entitlement past its grace is simply no entitlement.
    store = login(backend, claims(iat=now - 30 * 86400, exp=now - 29 * 86400))
    with pytest.raises(license.LicenseError, match="expired"):
        license.current(store=store, client=client(backend), now=now)
    assert backend.calls == []


def test_team_policy_is_not_fetched(on, backend):
    store = login(backend, team_claims())
    with pytest.raises(team_policy.PolicyUnavailable, match="airgap"):
        team_policy.fetch_policy(ORG, client(backend), store)
    assert backend.calls == []


def test_org_key_is_not_fetched(on, backend):
    store = login(backend, team_claims())
    keys = OrgKeys(store=store, client=client(backend))
    with pytest.raises(OrgKeyUnavailable):
        keys.get(ORG)
    assert backend.calls == []


def test_audit_events_are_neither_recorded_nor_sent(on, backend, tmp_path, fixed_identity):
    store = login(backend, team_claims())
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(mode=0o700)
    ev = ProEvents(str(tmp_path), store=store, client=client(backend),
                   spool=Spool(spool_dir), start_thread=False)
    ev.emit(Event(kind="assign", repo_root=str(tmp_path), agent_id="a1", branch="feat/x",
                  profile="developer", provider="claude", actor="user"))
    assert ev.queue.empty()
    assert ev.spool.entries() == []
    assert ev.flush() is False
    assert backend.calls == []
    assert not (spool_dir / "events-spool.jsonl").exists()


def test_spooled_events_from_an_online_run_stay_put(on, backend, tmp_path, fixed_identity):
    """An earlier, online run may have left events in the spool: they are
    neither sent nor dropped while the machine is air-gapped."""
    store = login(backend, team_claims())
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(mode=0o700)
    spool = Spool(spool_dir)
    spool.append(ORG, [{"kind": "assign", "at": time.time()}])
    ev = ProEvents(str(tmp_path), store=store, client=client(backend), spool=spool,
                   start_thread=False)
    assert ev.send_pending() is False
    assert len(spool.entries()) == 1
    assert backend.calls == []


def test_learning_sends_nothing_and_suggests_nothing(on, backend, tmp_path, fixed_identity):
    store = login(backend, claims(features=["learning"]))
    learner = CloudLearner(str(tmp_path), store=store, client=client(backend), start_thread=False)
    assert not learner.active()
    task = TaskInfo(repo_root=str(tmp_path), task="fix the bug", agent_id="a1",
                    profile="developer", weight="light")
    assert learner.suggest(task, ["a", "b", "c"]) is None
    learner.record(task, Outcome(event="merged", checks_passed=True))
    assert learner.flush()
    assert learner.queue.empty()
    assert backend.calls == []
    assert "inactive" in learner.report()


def test_ci_token_commands_are_refused(on, backend):
    """The org ci-token helpers are Pro paths like any other: refused before
    the transport."""
    store = login(backend, team_claims(features=["team", "ci"]))
    for call in (lambda: auth.create_ci_token(client(backend), store, ORG, "gh"),
                 lambda: auth.list_ci_tokens(client(backend), store, ORG),
                 lambda: auth.revoke_ci_token(client(backend), store, ORG, "ct_" + "0" * 32)):
        with pytest.raises(auth.AirGapped):
            call()
    assert backend.calls == []


def test_account_status_shows_the_offline_state(on, backend):
    store = login(backend, claims())
    out = io.StringIO()
    acct = ProAccount(store=store, transport=backend, out=out, err=io.StringIO())
    assert acct.run(["status"]) == 0
    text = out.getvalue()
    assert "(air-gap mode: showing the offline license)" in text
    assert "air-gap   on" in text and "doesn't include it" in text
    assert backend.calls == []


# -- local vs hosted profiles ----------------------------------------------------------------------------


@pytest.mark.parametrize("profile, why", [
    # `local: true` can't launder a hosted provider: its CLI talks to its own service.
    (hosted("codex", local=True), "`local: true` is ignored"),
    (native(base_url=None), "no base_url"),
])
def test_hosted_profiles_are_refused(profile, why):
    ok, reason = airgap.profile_allowed(profile)
    assert not ok and why in reason


def test_local_flag_parses_from_a_profile():
    p = _parse("---\nname: x\nprovider: native\nbase_url: http://box.lan:8000/v1\nlocal: true\n---\nhi",
               "x")
    assert p.local
    assert not _parse("---\nname: y\nprovider: native\n---\nhi", "y").local


def test_check_assign_refuses_hosted_profiles_in_air_gap_mode(on, repo):
    add_profile(repo, "local", "---\nname: local\nprovider: native\napi: openai\n"
                               "base_url: http://127.0.0.1:11434/v1\nmodel: qwen\n---\nlocal\n")
    add_profile(repo, "lan", "---\nname: lan\nprovider: native\napi: openai\n"
                             "base_url: http://ollama.lan:11434/v1\nmodel: qwen\nlocal: true\n---\nlan\n")
    cfg = RepoConfig()
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign")
    assert not d.allowed and "air-gap mode" in d.reason and "'claude'" in d.reason
    d = policy.check_assign(cfg, str(repo), "reviewer-codex", "t", "handoff")
    assert not d.allowed and "'codex'" in d.reason
    assert policy.check_assign(cfg, str(repo), "local", "t", "assign").allowed
    assert policy.check_assign(cfg, str(repo), "lan", "t", "assign").allowed
    d = policy.check_assign(cfg, str(repo), "no-such-profile", "t", "assign")
    assert not d.allowed and "couldn't be loaded" in d.reason


def test_air_gap_denials_are_audited_like_any_other(on, repo, brindle_home, monkeypatch):
    """A delegation air-gap mode refuses reaches the events plugins as a
    deny_assign with the reason, so the audit chain records it."""
    from brindle import plugins
    from brindle.pro import audit_chain
    from brindle.pro.audit_chain import AuditChain
    from test_plugins import Recorder, install

    plugins.reset()
    audit = AuditChain(str(repo), home=brindle_home, entitled=lambda: True)
    recorder = Recorder()
    install(monkeypatch, {plugins.EVENTS: [("audit", lambda r: audit), ("rec", lambda r: recorder)]})
    try:
        d = policy.check_assign(RepoConfig(), str(repo), "developer", "secret task", "assign",
                                branch="feat/x")
        assert not d.allowed and "air-gap mode" in d.reason
        recs = audit_chain.read_records(audit_chain.log_path(str(repo), brindle_home))
        assert [r["event"]["kind"] for r in recs] == ["deny_assign"]
        assert recs[0]["event"]["reason"] == d.reason
        assert recs[0]["event"]["profile"] == "developer" and recs[0]["event"]["provider"] == "claude"
        assert [e.kind for e in recorder.events] == ["deny_assign"]
        assert audit_chain.verify(str(repo), home=brindle_home).ok
    finally:
        plugins.reset()


def test_check_assign_from_the_repo_config_alone(repo):
    """The config object is enough: the process needn't be armed."""
    assert policy.check_assign(RepoConfig(), str(repo), "developer", "t", "assign").allowed
    d = policy.check_assign(RepoConfig(airgap=True), str(repo), "developer", "t", "assign")
    assert not d.allowed and "air-gap mode" in d.reason


def test_check_profile_refuses_an_unnamed_profile(on, repo):
    ok, why = airgap.check_profile(None, str(repo))
    assert not ok and "local profile" in why


# -- no hosted agent is launched, whatever the path ------------------------------------------------------------

LOCAL_PROFILE = ("---\nname: local\nprovider: native\napi: openai\n"
                 "base_url: http://127.0.0.1:11434/v1\nmodel: qwen\n---\nlocal\n")


@pytest.fixture
def ws(db, repo):
    from brindle import workspaces

    return workspaces.create(db, str(repo), "feature").workspace


def test_spawn_refuses_a_hosted_profile_in_air_gap_mode(on, db, ws):
    from brindle import agents

    with pytest.raises(agents.AgentError, match="air-gap mode.*'developer'.*'claude'"):
        agents.spawn(db, ws, "developer", provider_name="shell", mode="assign")
    assert db.list_agents() == []
    with pytest.raises(agents.AgentError, match="'subagent'"):
        agents.spawn(db, ws, "subagent", mode="handoff", prompt="t")
    assert db.list_agents() == []


def test_request_review_refuses_a_hosted_reviewer(on, db, ws):
    """The default reviewers (reviewer-codex, reviewer) are hosted: in air-gap
    mode no reviewer starts and the diff never leaves the machine."""
    from brindle import agents

    with pytest.raises(agents.AgentError, match="air-gap mode.*hosted service"):
        agents.request_review(db, None, ws)
    with pytest.raises(agents.AgentError, match="air-gap mode.*'reviewer'.*'claude'"):
        agents.request_review(db, None, ws, profile="reviewer")
    with pytest.raises(agents.AgentError, match="'codex'"):
        agents.request_review(db, None, ws, profile="reviewer-codex")
    assert db.list_agents() == []


def test_request_review_passes_a_local_reviewer_to_the_launch(on, db, ws, repo, monkeypatch):
    from brindle import agents

    add_profile(repo, "local", LOCAL_PROFILE)
    launched = []
    monkeypatch.setattr(agents, "spawn", lambda db, ws, profile, **kw: launched.append(profile))
    agents.request_review(db, None, ws, profile="local")
    assert launched == ["local"]


def test_resume_of_a_hosted_agent_is_refused(on, db, ws):
    """A paused hosted agent from before air-gap mode was turned on can't be
    brought back either: every launch goes through the same gate."""
    import time as _time

    from brindle import agents
    from brindle.db import Agent

    a = Agent(id="old1", workspace_id=ws.id, profile="developer", provider="claude", parent_id=None,
              mode="interactive", status="paused", tmux_window="", result=None,
              created_at=_time.time(), task=None)
    db.add_agent(a)
    with pytest.raises(agents.AgentError, match="air-gap mode"):
        agents._launch(db, a, ws, prompt=None, resume="s1", watch_pane=False)


def test_native_client_refuses_a_non_local_endpoint(on):
    """Defence in depth: even a native profile that slipped through (marked
    local, or with its base_url overridden) can't reach a hosted endpoint."""
    from brindle.native import client as native_client

    hosted_ep = native_client.Endpoint("https://api.together.xyz/v1", "m", retries=0)
    with pytest.raises(native_client.ClientError, match="air-gap mode"):
        native_client.Client(hosted_ep)._post({"x": 1})
    local_ep = native_client.Endpoint("http://127.0.0.1:1/v1", "m", retries=0, timeout=0.2)
    with pytest.raises(native_client.ClientError) as e:
        native_client.Client(local_ep, sleep=lambda s: None)._post({"x": 1})
    assert "air-gap" not in str(e.value)       # refused by the connection, not the gate


# -- the offline team policy ------------------------------------------------------------------------------


def team_plugin(backend, repo):
    store = login(backend, team_claims())
    return ProPolicy(str(repo), store=store, client=client(backend))


def assign(provider, model="qwen", running=0):
    return AssignInfo(repo_root="/r", task="t", profile="p", provider=provider, model=model,
                      actor="user", running_workers=running)


def test_offline_policy_file_is_enforced_instead_of_a_fetch(on, backend, repo):
    (repo / ".brindle").mkdir(exist_ok=True)
    airgap.policy_path(str(repo)).write_text(json.dumps(POLICY))
    p = team_plugin(backend, repo)
    assert p.check_assign(assign("native")).allowed
    d = p.check_assign(assign("claude"))
    assert not d.allowed and f"org {ORG} allows only native" in d.reason
    d = p.check_assign(assign("native", running=1))
    assert not d.allowed and "at most 1 parallel" in d.reason
    from brindle.policy import MergeInfo

    d = p.check_merge(MergeInfo(repo_root=str(repo), workspace_id="w", branch="b",
                                base_branch="main", actor="supervisor-1"))
    assert not d.allowed and "human review" in d.reason
    assert backend.calls == []


def test_offline_policy_wins_over_the_cached_copy(on, backend, repo):
    (repo / ".brindle").mkdir(exist_ok=True)
    airgap.policy_path(str(repo)).write_text(json.dumps(POLICY))
    team_policy.save_cached(team_policy.parse_policy(ORG, {
        "org_id": ORG, "version": 9, "policy": {"allowed_providers": ["claude", "native"]}}))
    p = team_plugin(backend, repo)
    assert not p.check_assign(assign("claude")).allowed
    assert backend.calls == []


def test_missing_offline_policy_refuses_everything(on, backend, repo):
    p = team_plugin(backend, repo)
    d = p.check_assign(assign("native"))
    assert not d.allowed and "air-gap mode" in d.reason and "policy.json" in d.reason
    assert backend.calls == []


@pytest.mark.parametrize("body", [
    "not json", json.dumps({**POLICY, "org_id": "org_other"}),
    json.dumps({"version": "three", "policy": {}}),
    json.dumps({"version": 1, "policy": {"allowed_providers": "native"}}),
])
def test_bad_offline_policy_refuses_everything(on, backend, repo, body):
    (repo / ".brindle").mkdir(exist_ok=True)
    airgap.policy_path(str(repo)).write_text(body)
    p = team_plugin(backend, repo)
    assert not p.check_assign(assign("native")).allowed
    assert backend.calls == []


# -- the offline license ------------------------------------------------------------------------------------


def enterprise(token, **over):
    now = int(time.time())
    c = dict(org_id=ORG, plan="enterprise", features=["learning", "team", "airgap"],
             exp=now + 365 * 86400)
    c.update(over)
    return token(**c)


def test_install_accepts_a_json_license_file(on, token):
    tok = enterprise(token)
    ent = license.install(json.dumps({"license": tok, "issued_to": "Acme"}).encode())
    assert ent.plan == "enterprise"
    assert license.license_path().read_text() == tok


@pytest.mark.parametrize("mangle", [
    lambda t: t[:-4] + ("AAAA" if not t.endswith("AAAA") else "BBBB"),     # bad signature
    lambda t: json.dumps({"something": "else"}),
])
def test_a_forged_or_malformed_license_is_not_installed(on, token, mangle):
    with pytest.raises(license.LicenseError):
        license.install(mangle(enterprise(token)))
    assert not license.license_path().exists()
    assert license.installed() is None


def test_installed_license_is_used_when_not_logged_in_even_online(token):
    assert not airgap.enabled()
    license.install(enterprise(token))
    assert license.current().plan == "enterprise"
    assert license.has("airgap")


def test_login_credentials_win_over_the_license_when_online(backend, token):
    login(backend, claims(plan="pro"), access_expires_at=time.time() + 900)
    license.install(enterprise(token))
    ent = license.current(store=credentials.default_store(), client=client(backend))
    assert ent.plan == "pro"


def test_uninstall(on, token):
    license.install(enterprise(token))
    assert license.uninstall()
    assert license.installed() is None
    assert not license.uninstall()


def test_account_license_commands(on, token, tmp_path):
    lic = tmp_path / "acme.license"
    lic.write_text(enterprise(token) + "\n")
    out, err = io.StringIO(), io.StringIO()
    acct = ProAccount(store=credentials.default_store(), out=out, err=err)
    assert acct.run(["license"]) == 1
    assert "No offline license installed" in out.getvalue()
    assert acct.run(["license", "install", str(lic)]) == 0
    text = out.getvalue()
    assert f"Installed offline license for org {ORG}" in text and "air-gap mode is included" in text
    out.truncate(0), out.seek(0)
    assert acct.run(["license", "status"]) == 0
    text = out.getvalue()
    assert f"org       {ORG}" in text and "airgap" in text and "air-gap   on" in text
    assert "doesn't include" not in text
    assert acct.run(["license", "remove"]) == 0
    assert license.installed() is None
    assert acct.run(["license", "install"]) == 2 and "usage" in err.getvalue()
    assert acct.run(["license", "install", str(tmp_path / "missing")]) == 1
    assert "cannot read" in err.getvalue()


def test_account_license_install_rejects_a_bad_file(on, tmp_path):
    bad = tmp_path / "bad.license"
    bad.write_text("not a license")
    err = io.StringIO()
    acct = ProAccount(store=credentials.default_store(), out=io.StringIO(), err=err)
    assert acct.run(["license", "install", str(bad)]) == 1
    assert "malformed entitlement" in err.getvalue()
    assert license.installed() is None


# -- doctor -----------------------------------------------------------------------------------------------------


def by_name(checks):
    return {c.name: c for c in checks}


def test_doctor_is_quiet_when_off(repo):
    [c] = doctor.airgap_checks(str(repo))
    assert (c.level, c.name, c.detail) == (doctor.OK, "air-gap", "off")


def test_doctor_shows_air_gap_status_and_hosted_profiles(on, repo, token):
    checks = by_name(doctor.airgap_checks(str(repo)))
    assert checks["air-gap"].level == doctor.WARN
    assert "on via BRINDLE_AIRGAP=1" in checks["air-gap"].detail
    assert "doesn't include it" in checks["air-gap"].detail
    assert checks["hosted profiles"].level == doctor.WARN
    assert "developer" in checks["hosted profiles"].detail
    assert "refused in air-gap mode" in checks["hosted profiles"].detail
    assert checks["offline license"].level == doctor.WARN
    assert checks["offline policy"].level == doctor.WARN and "policy.json" in checks["offline policy"].detail
    # The chat is a hosted agent too: brindle itself can't start on the default developer.
    assert checks["default agent"].level == doctor.FAIL
    assert "developer" in checks["default agent"].detail
    assert "`brindle` won't start" in checks["default agent"].detail

    license.install(enterprise(token))
    (repo / ".brindle").mkdir(exist_ok=True)
    airgap.policy_path(str(repo)).write_text(json.dumps(POLICY))
    add_profile(repo, "local", LOCAL_PROFILE)
    write_config(repo, default_agent="local")
    checks = by_name(doctor.airgap_checks(str(repo)))
    assert checks["air-gap"].level == doctor.OK and "no outbound traffic" in checks["air-gap"].detail
    assert checks["offline license"].level == doctor.OK and ORG in checks["offline license"].detail
    assert checks["offline policy"].level == doctor.OK
    assert checks["default agent"].level == doctor.OK and "local" in checks["default agent"].detail


def test_doctor_reads_air_gap_from_the_repo_config(repo):
    write_config(repo, airgap=True)
    checks = by_name(doctor.airgap_checks(str(repo)))
    assert checks["air-gap"].level == doctor.WARN
    assert "config.json" in checks["air-gap"].detail


def test_doctor_renders_the_air_gap_lines(on, repo):
    text = doctor.render(doctor.airgap_checks(str(repo)))
    assert "! air-gap" in text and "hosted profiles" in text


def test_a_broken_config_fails_closed_at_launch(tmp_path):
    from brindle import agents

    (tmp_path / ".brindle").mkdir()
    (tmp_path / ".brindle" / "config.json").write_text("{not json")
    with pytest.raises(agents.AgentError, match="air-gap mode can't be ruled out"):
        agents._airgap_check(None, str(tmp_path))
