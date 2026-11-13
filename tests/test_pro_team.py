"""brindle Team: the org policy plugin, the audit-event plugin, and the
``brindle account org`` commands. Both plugins are always installed and must do
nothing without a verified team entitlement."""
import io
import json
import os
import re
import stat
import time

import pytest

from brindle.events import Event
from brindle.policy import AssignInfo, MergeInfo
from brindle.pro import account, auth, credentials
from brindle.pro import route_health, team_events
from brindle.pro._files import private_dir
from brindle.pro.orgkey import OrgKey
from brindle.pro.team_events import ProEvents, Spool
from brindle.pro.team_policy import ProPolicy
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, ROOT_SHA, backend, claims, fixed_identity, pro_env, sign, signing_key,
)

ORG = "org_team1"
REPO = "/work/secret-client-project"
BRANCH = "feat/zebra-hotfix"
AGENT = "worker-zebra-7"
SUPERVISOR = "supervisor-zebra-1"
POLICY = {"allowed_providers": ["claude", "codex"], "allowed_models": ["claude-sonnet-4", "gpt-5"],
          "require_human_review": True, "max_parallel_workers": 4}


def team_claims(**over):
    c = dict(org_id=ORG, plan="team", features=["learning", "team"], role="member",
             policy_version=3)
    c.update(over)
    return claims(**c)


@pytest.fixture
def team(backend, fixed_identity):
    """The fake backend with team-org endpoints and a logged-in team member."""
    backend.policy_body = {"org_id": ORG, "version": 3, "policy": dict(POLICY),
                           "updated_at": 1, "updated_by": "u"}
    backend.events = []

    def guarded(fn):
        def route(form, headers):
            if not backend._bearer(headers):
                return 401, {"error": "invalid_token"}
            return fn(form, headers)
        return route

    backend.routes[f"GET /orgs/{ORG}/policy"] = guarded(lambda f, h: (200, backend.policy_body))

    def events(form, headers):
        assert set(form) == {"events"} and 1 <= len(form["events"]) <= 100
        backend.events.extend(form["events"])
        return 200, {"accepted": len(form["events"])}

    backend.routes[f"POST /orgs/{ORG}/events"] = guarded(events)
    backend.routes["GET /orgs"] = guarded(lambda f, h: (200, {"orgs": [
        {"org_id": "org_personal", "name": "me (personal)", "personal": True, "role": "owner",
         "plan": "pro", "status": "active", "seats": 1, "member_count": 1},
        {"org_id": ORG, "name": "Team One", "personal": False, "role": "member", "plan": "team",
         "status": "active", "seats": 5, "member_count": 3}]}))
    backend.routes[f"GET /entitlement?org_id={ORG}"] = guarded(
        lambda f, h: (200, {"entitlement": sign(backend.key, team_claims())}))
    backend.store = credentials.default_store()
    login(backend, team_claims())
    return backend


def login(backend, entitlement_claims, **extra):
    t = backend.issue()
    backend.store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                        "access_expires_at": time.time() + 900, "base_url": BASE,
                        "entitlement": sign(backend.key, entitlement_claims), **extra})


def client(backend):
    return auth.Client(BASE, backend)


def plugin(backend):
    return ProPolicy(REPO, store=backend.store, client=client(backend))


def assign(provider="claude", model="claude-sonnet-4", profile="developer", running=0):
    return AssignInfo(repo_root=REPO, task="t", profile=profile, provider=provider, model=model,
                      actor="user", running_workers=running)


def merge(actor):
    return MergeInfo(repo_root=REPO, workspace_id="ws1", branch=BRANCH, base_branch="main",
                     agent_id=AGENT, profile="developer", provider="claude", actor=actor)


# -- policy: no team entitlement ------------------------------------------------------------------


def test_a_broken_credential_store_allows_everything(backend, monkeypatch):
    def boom():
        raise RuntimeError("keychain is having a day")

    monkeypatch.setattr(credentials, "default_store", boom)
    p = ProPolicy(REPO)
    assert p.check_assign(assign()).allowed and p.check_merge(merge(SUPERVISOR)).allowed


# -- policy rules ------------------------------------------------------------------------------------


@pytest.mark.variant("bypass: an undeclared model cannot bypass an organization model allowlist")
def test_undeclared_model_is_denied_when_a_list_is_set(team):
    d = plugin(team).check_assign(assign(model=None))
    assert not d.allowed and "no declared model" in d.reason


@pytest.mark.variant("the reported bug: other tests reach these refusals but none asserts the org and link each one tells the person")
@pytest.mark.parametrize("path", ["assign", "routing", "create"])
def test_model_refusal_points_at_the_org_to_change(team, monkeypatch, path):
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit

    from brindle import providers
    from brindle.daemon.routes_actions import create_profile
    from brindle.pro import team_policy
    from brindle.service.errors import ServiceError
    from brindle.service.routing import RoutingPolicy

    acc = team_policy.Access(org_id=ORG, allowed_models=tuple(POLICY["allowed_models"]))
    monkeypatch.setattr(RoutingPolicy, "load", lambda repo: RoutingPolicy(acc, None, repo))
    monkeypatch.setattr(providers, "unusable", lambda provider: None)
    if path == "assign":
        reason = plugin(team).check_assign(assign(model="forbidden-model")).reason
    elif path == "routing":
        reason = RoutingPolicy(acc, None, REPO, {"developer": ("claude", "forbidden-model")}).refusal(
            SimpleNamespace(name="developer"))
    else:
        with pytest.raises(ServiceError) as error:
            create_profile(None, {}, {"repo": REPO, "name": "new-agent", "provider": "claude",
                                      "model": "forbidden-model"})
        reason = error.value.message
    [link] = re.findall(r"https?://[^\s)]+", reason)
    url = urlsplit(link)
    assert (url.scheme, url.netloc) == (urlsplit(BASE).scheme, urlsplit(BASE).netloc)
    assert url.path == f"/brindle/account/orgs/{ORG}/settings"
    assert parse_qs(url.query) == {"section": ["models"], "onboard": ["1"]}


# -- policy caching and failing closed -----------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {"version": "3", "policy": POLICY},
    pytest.param({"org_id": "org_other", "version": 3, "policy": POLICY},
                 marks=pytest.mark.variant("identity: a policy belonging to another organization must fail closed")),
])
def test_malformed_policy_fails_closed(team, body):
    team.policy_body = body
    assert not plugin(team).check_assign(assign()).allowed


def test_a_loosened_cache_file_is_not_trusted(team):
    from brindle.pro.team_policy import cache_path

    plugin(team).check_assign(assign())
    os.chmod(cache_path(ORG), 0o644)
    team.routes[f"GET /orgs/{ORG}/policy"] = [auth.TransportError("down")]
    assert not plugin(team).check_assign(assign()).allowed


@pytest.mark.variant("live incident: a pre-upgrade process rewrote the cache without a federated route's fields")
def test_policy_cache_preserves_unknown_fields_and_ignores_legacy_writes(team):
    from brindle.pro import team_policy
    from brindle.pro._files import write_private

    team.policy_body["policy"]["future_route"] = {"auth": "federated"}
    p = team_policy.fetch_policy(ORG, client(team), team.store, cached_for=("member", None))
    path = team_policy.cache_path(ORG)
    assert json.loads(path.read_bytes())["policy"] == team.policy_body["policy"]
    write_private(private_dir() / f"policy-{ORG}.json", json.dumps({"version": 0}).encode())
    cached = team_policy.load_cached(ORG)
    assert (cached.version, cached.cached_for, cached.fetched_at) == (3, ("member", None), p.fetched_at)
    team_policy.save_cached(cached)
    assert json.loads(path.read_bytes())["policy"] == team.policy_body["policy"]


# -- events -------------------------------------------------------------------------------------------


def ev(kind="merge", **over):
    base = dict(kind=kind, repo_root=REPO, agent_id=AGENT, branch=BRANCH, profile="developer",
                provider="claude", model="claude-sonnet-4", actor=SUPERVISOR, at=1_800_000_000.5,
                approved=None, merged=None)
    base.update(over)
    return Event(**base)


def events_plugin(backend, **kw):
    return ProEvents(REPO, store=backend.store, client=client(backend),
                     start_thread=kw.pop("start_thread", False), **kw)


@pytest.mark.variant("privacy: serialized team events must hash identities and reject unsafe profile, provider and model values")
def test_event_payload_has_exactly_the_contract_keys(team):
    p = events_plugin(team)
    p.emit(ev("review", approved=True))
    p.emit(ev("remove", merged=False, actor="user"))
    p.emit(ev(profile="my profile", provider="Claude Code", model="/Users/me/models/x.gguf"))
    assert p.flush()
    review, remove, unsafe = team.events
    assert (unsafe["profile"], unsafe["provider"], unsafe["model"]) == (None, None, None)
    keys = {"kind", "agent_ref", "branch_ref", "profile", "provider", "model", "actor_ref", "at",
            "approved", "merged", "cost_usd"}     # by_model only when there is a split
    assert set(review) == set(remove) == keys
    assert review["cost_usd"] is None
    key = OrgKey(ORG, *team.org_key(ORG))
    assert review["agent_ref"] == team_events.agent_ref(key, AGENT) == key.ref("agent\0" + AGENT)
    assert review["branch_ref"] == key.ref("branch\0" + ROOT_SHA + "\0" + BRANCH)
    assert re.fullmatch(r"[0-9a-f]{64}", review["branch_ref"])
    assert review["actor_ref"] == team_events.agent_ref(key, SUPERVISOR)
    assert remove["actor_ref"] == "user"
    assert (review["approved"], review["merged"], remove["merged"]) == (True, None, False)
    assert review["at"] == 1_800_000_000.5
    blob = json.dumps(team.events)
    for s in ("zebra", BRANCH, AGENT, SUPERVISOR, REPO, "secret-client-project", "x.gguf"):
        assert s not in blob


def test_spool_survives_offline_periods_and_restarts(team):
    team.routes[f"POST /orgs/{ORG}/events"] = [auth.TransportError("down")]
    p = events_plugin(team)
    for k in ("assign", "review", "merge"):
        p.emit(ev(k))
    assert not p.flush()
    assert [e["event"]["kind"] for e in Spool().entries()] == ["assign", "review", "merge"]
    spool_file = private_dir() / "events-spool.jsonl"
    assert stat.S_IMODE(os.stat(spool_file).st_mode) == 0o600
    # "restart": a new plugin, backend reachable again
    team.routes[f"POST /orgs/{ORG}/events"] = lambda f, h: (team.events.extend(f["events"]),
                                                             (200, {}))[1]
    again = events_plugin(team)
    assert again.send_pending()
    assert [e["kind"] for e in team.events] == ["assign", "review", "merge"]
    assert Spool().entries() == []


def test_route_health_rides_the_feed_as_codes_without_provider_text(team):
    p = events_plugin(team)
    error = "API Error: 401 {'type':'authentication_error','message':'invalid x-api-key sk-ant-api03-zebra'}"
    p.emit(Event(kind="route_health", repo_root=REPO, provider="claude", model="claude-opus-5-5",
                 route="console", outcome=route_health.provider_outcome(error), count=3, at=1_800_000_000.5))
    assert p.flush()
    assert team.events == [{"kind": "route_health", "agent": "claude", "route": "console",
                            "model": "claude-opus-5-5", "outcome": "provider_401", "count": 3,
                            "at": 1_800_000_000.5}]
    blob = json.dumps(team.events)
    assert "zebra" not in blob and "x-api-key" not in blob and "authentication_error" not in blob
    assert p.dropped_no_key == 0 and not any("/key" in c for c in team.paths())


def test_rejected_batches_are_dropped_not_retried_forever(team):
    team.routes[f"POST /orgs/{ORG}/events"] = [(422, {"error": "invalid_request"})]
    p = events_plugin(team)
    p.emit(ev())
    assert p.flush()
    assert Spool().entries() == []


def test_spool_caps_drop_the_oldest(tmp_path):
    d = tmp_path / "pro"
    d.mkdir(mode=0o700)
    s = Spool(d, max_events=5)
    s.append(ORG, [{"kind": "merge", "n": i} for i in range(8)])
    assert [e["event"]["n"] for e in s.entries()] == [3, 4, 5, 6, 7] and s.dropped == 3
    small = Spool(d, max_bytes=300)
    small.append(ORG, [{"kind": "merge", "n": i, "pad": "x" * 50} for i in range(20)])
    assert (d / "events-spool.jsonl").stat().st_size <= 300
    assert small.entries()[-1]["event"]["n"] == 19


def test_no_org_key_means_no_team_events(team):
    team.key_status = (403, {"error": "forbidden"})
    p = events_plugin(team)
    p.emit(ev())
    p.flush()
    assert Spool().entries() == [] and team.events == [] and p.dropped_no_key == 1


# -- account org commands and wiring ------------------------------------------------------------------


def run(backend, *args):
    out, err = io.StringIO(), io.StringIO()
    code = account.ProAccount(REPO, store=backend.store, transport=backend, out=out, err=err).run(list(args))
    return code, out.getvalue(), err.getvalue()


def test_org_list_use_and_policy(team, monkeypatch):
    from brindle.daemon import routes_read

    login(team, claims())          # personal org first
    code, out, _ = run(team, "org", "list")
    assert code == 0 and ORG in out and "Team One" in out and "team" in out
    code, out, _ = run(team, "org", "use", ORG)
    assert code == 0 and ORG in out and "member" in out
    assert team.store.load()["org_id"] == ORG
    code, out, _ = run(team, "org", "list")
    assert f"* {ORG}" in out
    # The app's org picker: the same list, from GET /v1/account.
    monkeypatch.setattr(account.ProAccount, "_client", lambda self, base: auth.Client(BASE, transport=team))
    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "path": "/v1/account", "query_string": b"",
                       "headers": []})
    body = json.loads(routes_read.account(request).body)
    assert [(o["id"], o["name"], o["role"], o["current"]) for o in body["orgs"]] == [
        ("org_personal", "me (personal)", "owner", False), (ORG, "Team One", "member", True)]
    code, out, _ = run(team, "org", "policy")
    assert code == 0 and "version 3" in out and "claude, codex" in out
    assert "require human review   yes" in out
    code, out, _ = run(team, "status")
    assert f"{ORG} (member)" in out


def test_org_policy_prints_every_role_override(team):
    login(team, team_claims())
    grants = {"allow": True, "kinds": ["bash", "write"], "overridable": ["env"], "max_minutes": 60, "max_uses": 5}
    team.policy_body = {**team.policy_body, "policy": {**POLICY, "roles": {"reviewer": {
        "allowed_providers": ["claude"], "allowed_models": ["gpt-5"], "allowed_profiles": ["light"],
        "require_human_review": False, "max_parallel_workers": 2, "permission_grants": grants}}}}
    code, out, err = run(team, "org", "policy")
    assert code == 0, err
    grant_line = next(line for line in out.splitlines() if "permission grants" in line)
    assert "reviewer" in out and all(v in grant_line for v in ("bash", "write", "60", "5"))


# -- org CI tokens ---------------------------------------------------------------------------------


@pytest.mark.brindle_ci
def test_ci_token_create_list_revoke(team):
    secret = "cpc_" + "c" * 43
    token_id = "ct_" + "a" * 32
    seen = []

    def create(form, headers):
        seen.append(form)
        return 200, {"token": secret, "token_id": token_id, "org_id": ORG, "name": form["name"],
                     "created_at": 1_900_000_000}

    team.routes[f"POST /orgs/{ORG}/ci-tokens"] = create
    team.routes[f"GET /orgs/{ORG}/ci-tokens"] = lambda f, h: (200, {"org_id": ORG, "tokens": [
        {"token_id": token_id, "name": "github actions", "created_by": "user_1",
         "created_at": 1_900_000_000, "last_used_at": None, "status": "active"}]})
    team.routes[f"DELETE /orgs/{ORG}/ci-tokens/{token_id}"] = lambda f, h: (
        200, {"org_id": ORG, "token_id": token_id, "status": "revoked"})

    code, out, err = run(team, "org", "ci-token", "create", "github", "actions", "--org", ORG)
    assert code == 0, err
    assert seen == [{"name": "github actions"}]
    assert secret in out and "BRINDLE_PRO_TOKEN" in out and "shown once" in out
    assert team.store.load().get("ci_token") is None and secret not in json.dumps(team.store.load())

    code, out, _ = run(team, "org", "ci-token", "list", "--org", ORG)
    assert code == 0 and token_id in out and "github actions" in out and "active" in out
    assert secret not in out

    code, out, _ = run(team, "org", "ci-token", "revoke", token_id, "--org", ORG)
    assert code == 0 and f"Revoked CI token {token_id}" in out


def test_ci_token_errors_and_usage(team):
    team.routes[f"POST /orgs/{ORG}/ci-tokens"] = lambda f, h: (
        403, {"error": "forbidden", "error_description": "requires the admin role"})
    code, _, err = run(team, "org", "ci-token", "create", "deploy", "--org", ORG)
    assert code == 1 and "forbidden" in err and "admin" in err
    code, _, err = run(team, "org", "ci-token", "revoke", "not-an-id", "--org", ORG)
    assert code == 1 and "invalid CI token id" in err


# -- org budgets and protected paths (Team "org_budgets") ---------------------------------------------

BUDGETS = {"budget": {"seat_month_usd": 50, "goal_usd": 5}, "protected_paths": ["infra/", "*.lock"]}


@pytest.fixture
def budgets(team, monkeypatch):
    """A member of a team org whose plan has org_budgets, and a policy that sets both."""
    from brindle import budget  # noqa: F401 - imported so the patch below finds the module loaded
    from brindle.pro import license, team_policy

    login(team, team_claims(features=["learning", "team", "org_budgets"]))
    team.policy_body = {**team.policy_body, "policy": {**POLICY, **BUDGETS},
                        "spend": {"month": time.strftime("%Y-%m", time.gmtime()), "seat_usd": 12.5}}
    monkeypatch.setattr(license, "has", lambda feature: feature in ("org_budgets", "cost"))
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: plugin(team).org_budgets())
    return team


def test_org_budgets_fail_closed_when_the_policy_was_never_fetched(budgets, monkeypatch):
    budgets.routes[f"GET /orgs/{ORG}/policy"] = [auth.TransportError("down")]
    d = plugin(budgets).org_budgets()
    assert not d.allowed and "never been fetched" in d.reason
    # ... and a budget that can't be read means the budget is exhausted, not unlimited
    from types import SimpleNamespace

    from brindle import budget

    monkeypatch.setattr(budget, "entitled", lambda: False)
    lim = budget.limits(SimpleNamespace(budget={}), REPO)
    assert (lim.month_usd, lim.goal_usd) == (0.0, 0.0)


@pytest.mark.parametrize("over", [
    {"budget": "50"}, {"budget": {"goal_usd": True}}, {"protected_paths": ["x"] * 65},
])
def test_malformed_budget_fields_fail_closed(team, over):
    team.policy_body = {**team.policy_body, "policy": {**POLICY, **over}}
    assert not plugin(team).check_assign(assign()).allowed


def org_policy(**kw):
    from brindle.pro.team_policy import OrgPolicy

    return OrgPolicy(org_id=ORG, version=1, **kw)


# a real branch through the rule check's protected-paths gate


@pytest.fixture
def protected_branch(db, repo):
    from pathlib import Path

    from conftest import sh
    from brindle import workspaces

    (repo / "infra").mkdir()
    (repo / "infra" / "a.tf").write_text("a\n")
    sh("git add -A && git commit -qm infra && git push -q origin main", repo)
    ws = workspaces.create(db, str(repo), "feat").workspace
    path = Path(ws.path)
    (path / "src").mkdir()
    (path / "src" / "ok.py").write_text("x = 1\n")
    sh("git add -A && git commit -qm ok", path)
    return ws, path


def commit(path, name, text="y\n"):
    from conftest import sh

    (path / name).parent.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(text)
    sh("git add -A && git commit -qm more", path)


def test_a_change_to_a_protected_path_fails_the_gate(db, protected_branch, monkeypatch):
    from brindle import rule_checks
    from brindle.pro import team_policy

    ws, path = protected_branch
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: org_policy(protected_paths=BUDGETS["protected_paths"]))
    result = rule_checks.run(db, ws)                       # a worker-less branch is checked too
    assert result.ok and result.packs == ["protected_paths"]
    commit(path, "infra/main.tf")
    commit(path, "deep/dir/poetry.lock")
    from conftest import sh

    sh("git rm -q infra/a.tf && git commit -qm rm", path)         # deleting a protected file fails too
    result = rule_checks.run(db, ws)
    assert not result.ok
    assert sorted(v.detail.split(" ")[0] for v in result.violations) == [
        "deep/dir/poetry.lock", "infra/a.tf", "infra/main.tf"]
    assert "protected path" in rule_checks.gate_problem(result, ws.branch)
    files = ["infra/a.tf", "src/infra/b.py", ".github/workflows/ci.yml", "INFRA/c.tf", "src/app.py"]
    assert rule_checks.protected_files(files, ["infra"]) == ["infra/a.tf", "INFRA/c.tf"]    # a bare directory
    assert rule_checks.protected_files(files, ["./.github/workflows/"]) == [".github/workflows/ci.yml"]


def test_protected_paths_that_cant_be_read_fail_the_gate(db, protected_branch, monkeypatch):
    from brindle import rule_checks
    from brindle.policy import deny
    from brindle.pro import team_policy

    ws, _ = protected_branch
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: deny("never been fetched"))
    result = rule_checks.run(db, ws)
    assert not result.ok and "can't be checked" in result.problem
    assert "couldn't run" in rule_checks.gate_problem(result, ws.branch)

    def boom(root):
        raise RuntimeError("keychain")

    monkeypatch.setattr(team_policy, "org_budgets", boom)
    assert not rule_checks.run(db, ws).ok
