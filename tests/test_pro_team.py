"""brindle Team: the org policy plugin, the audit-event plugin, and the
``brindle account org`` commands. Both plugins are always installed and must do
nothing without a verified team entitlement."""
import io
import json
import os
import re
import stat
import threading
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


def test_undeclared_model_is_denied_when_a_list_is_set(team):
    d = plugin(team).check_assign(assign(model=None))
    assert not d.allowed and "no declared model" in d.reason


# -- policy caching and failing closed -----------------------------------------------------------------


@pytest.mark.parametrize("body", [
    {"version": "3", "policy": POLICY},
    {"org_id": "org_other", "version": 3, "policy": POLICY},
])
def test_malformed_policy_fails_closed(team, body):
    team.policy_body = body
    assert not plugin(team).check_assign(assign()).allowed


def test_a_loosened_cache_file_is_not_trusted(team):
    plugin(team).check_assign(assign())
    os.chmod(private_dir() / f"policy-{ORG}.json", 0o644)
    team.routes[f"GET /orgs/{ORG}/policy"] = [auth.TransportError("down")]
    assert not plugin(team).check_assign(assign()).allowed


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


def test_event_payload_has_exactly_the_contract_keys(team):
    p = events_plugin(team)
    p.emit(ev("review", approved=True))
    p.emit(ev("remove", merged=False, actor="user"))
    assert p.flush()
    review, remove = team.events
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
    for s in ("zebra", BRANCH, AGENT, SUPERVISOR, REPO, "secret-client-project"):
        assert s not in blob


def test_unsafe_identifiers_become_null(team):
    p = events_plugin(team)
    p.emit(ev(profile="my profile", provider="Claude Code", model="/Users/me/models/x.gguf"))
    assert p.flush()
    (e,) = team.events
    assert (e["profile"], e["provider"], e["model"]) == (None, None, None)


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


def test_failed_sends_back_off_exponentially(team):
    team.routes[f"POST /orgs/{ORG}/events"] = [(500, {"error": "internal_error"})]
    p = events_plugin(team)
    p.emit(ev())
    delays = []
    for _ in range(4):
        p.flush()
        delays.append(p._delay)
    assert delays == [1.0, 2.0, 4.0, 8.0]
    assert len(Spool().entries()) == 1


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


def test_a_backend_without_route_health_keeps_the_audit_events_and_drops_the_counts(team):
    sent, answers = [], iter([422, 200, 403])

    def route(f, h):
        sent.append(f["events"])
        return next(answers), {}

    team.routes[f"POST /orgs/{ORG}/events"] = route
    p = events_plugin(team)
    key = OrgKey(ORG, "k1", b"k" * 32)
    health = route_health.payload(Event(kind="route_health", repo_root=REPO, provider="claude", route="bedrock",
                                        model="m", outcome="provider_403", count=2, at=1_800_000_000.5))
    Spool().append(ORG, [team_events.event_payload(key, ROOT_SHA, ev()), health])
    assert p.send_pending()      # 422 for the mixed batch: the audit event goes alone
    assert [[e["kind"] for e in b] for b in sent] == [["merge", "route_health"], ["merge"]]
    Spool().append(ORG, [health])
    assert p.send_pending()      # 403: this org can't report route health; dropped, not retried
    assert len(sent) == 3 and Spool().entries() == []


def test_rejected_batches_are_dropped_not_retried_forever(team):
    team.routes[f"POST /orgs/{ORG}/events"] = [(422, {"error": "invalid_request"})]
    p = events_plugin(team)
    p.emit(ev())
    assert p.flush()
    assert Spool().entries() == []


def test_a_backend_without_by_model_gets_the_events_without_it(team):
    sent = []

    def route(f, h):
        sent.append(f["events"])
        return (422, {"error": "invalid_request"}) if any("by_model" in e for e in f["events"]) else (200, {})

    team.routes[f"POST /orgs/{ORG}/events"] = route
    p = events_plugin(team)
    key = OrgKey(ORG, "k1", b"k" * 32)
    Spool().append(ORG, [team_events.event_payload(
        key, ROOT_SHA, ev(kind="remove", cost_usd=1.0, by_model={"opus": {"tokens": 5, "usd": 1.0}}))])
    assert p.send_pending()
    assert len(sent) == 2 and "by_model" not in sent[1][0] and sent[1][0]["cost_usd"] == 1.0
    assert Spool().entries() == []


def test_batches_are_at_most_100(team):
    sizes = []
    team.routes[f"POST /orgs/{ORG}/events"] = lambda f, h: (sizes.append(len(f["events"])), (200, {}))[1]
    p = events_plugin(team)
    key = OrgKey(ORG, "k1", b"k" * 32)
    Spool().append(ORG, [team_events.event_payload(key, ROOT_SHA, ev(at=1_800_000_000 + i))
                         for i in range(250)])
    assert p.send_pending()
    assert sizes == [100, 100, 50]


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


def test_full_queue_spills_to_the_spool(team, monkeypatch):
    monkeypatch.setattr(team_events, "QUEUE_SIZE", 2)
    team.routes[f"POST /orgs/{ORG}/events"] = [auth.TransportError("down")]
    p = events_plugin(team)
    p.keys.get(ORG)          # the spill path uses only a cached key
    for i in range(5):
        p.emit(ev(at=1_800_000_000 + i))
    assert p.queue.qsize() == 2 and len(Spool().entries()) == 3
    p.flush()
    assert len(Spool().entries()) == 5


def test_background_sender_delivers(team, monkeypatch):
    # The send is held until flush is polling, so flush's wait loop always runs.
    polling = threading.Event()
    send = team.routes[f"POST /orgs/{ORG}/events"]
    team.routes[f"POST /orgs/{ORG}/events"] = lambda f, h: (polling.wait(5), send(f, h))[1]
    monkeypatch.setattr(team_events.time, "sleep", lambda _: polling.set())
    p = events_plugin(team, start_thread=True)
    p.emit(ev())
    assert p.flush(5)
    assert len(team.events) == 1


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


def test_org_list_use_and_policy(team):
    login(team, claims())          # personal org first
    code, out, _ = run(team, "org", "list")
    assert code == 0 and ORG in out and "Team One" in out and "team" in out
    code, out, _ = run(team, "org", "use", ORG)
    assert code == 0 and ORG in out and "member" in out
    assert team.store.load()["org_id"] == ORG
    code, out, _ = run(team, "org", "list")
    assert f"* {ORG}" in out
    code, out, _ = run(team, "org", "policy")
    assert code == 0 and "version 3" in out and "claude, codex" in out
    assert "require human review   yes" in out
    code, out, _ = run(team, "status")
    assert f"{ORG} (member)" in out


def test_org_use_rejects_a_bad_org_id(team):
    code, _, err = run(team, "org", "use", "../../etc")
    assert code == 1 and "invalid org id" in err


# -- self-serve Team: create an org, buy seats, invite, join ---------------------------------------


def test_create_a_team_org_then_check_out_team_seats(team):
    login(team, claims())
    new_org = "org_" + "a" * 24

    def create(form, headers):
        assert form == {"name": "Acme Eng"}
        return 200, {"org_id": new_org, "name": "Acme Eng", "personal": False, "role": "owner"}

    def checkout(form, headers):
        assert form == {"plan": "team", "seats": 5, "org_id": new_org}
        return 200, {"url": "https://checkout.stripe.test/c/team", "id": "cs_1"}

    team.routes["POST /orgs"] = create
    team.routes["POST /billing/checkout"] = checkout
    code, out, _ = run(team, "org", "create", "Acme", "Eng")
    assert code == 0 and new_org in out and f"upgrade --team --seats N --org {new_org}" in out
    code, out, _ = run(team, "upgrade", "--team", "--seats", "5", "--org", new_org)
    assert code == 0 and out.strip() == "https://checkout.stripe.test/c/team"


def test_invite_and_join(team):
    code_ = "cpi_" + "b" * 43
    team.routes[f"POST /orgs/{ORG}/invites"] = lambda f, h: (
        200, {"invite_code": code_, "org_id": ORG, "email": f["email"], "role": f["role"]})
    team.routes["POST /invites/accept"] = lambda f, h: (
        (200, {"org_id": ORG, "role": "member"}) if f == {"invite_code": code_} else (400, {}))
    code, out, _ = run(team, "org", "invite", "dev@acme.test", "--admin", "--org", ORG)
    assert code == 0 and "as admin" in out and f"brindle account org join {code_}" in out
    code, out, _ = run(team, "org", "join", code_)
    assert code == 0 and f"Joined {ORG} as member" in out


# -- org CI tokens ---------------------------------------------------------------------------------


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
    assert [c[0] for c in team.calls if "ci-tokens" in c[0]] == [
        f"POST /orgs/{ORG}/ci-tokens", f"GET /orgs/{ORG}/ci-tokens", f"DELETE /orgs/{ORG}/ci-tokens/{token_id}"]


def test_ci_token_defaults_to_the_current_org_and_needs_one(team):
    code, _, err = run(team, "org", "ci-token", "list")
    assert code == 1 and "no team org selected" in err
    team.store.save({**team.store.load(), "org_id": ORG})
    team.routes[f"GET /orgs/{ORG}/ci-tokens"] = lambda f, h: (200, {"org_id": ORG, "tokens": []})
    code, out, _ = run(team, "org", "ci-token", "list")
    assert code == 0 and "no CI tokens" in out


def test_ci_token_errors_and_usage(team):
    team.routes[f"POST /orgs/{ORG}/ci-tokens"] = lambda f, h: (
        403, {"error": "forbidden", "error_description": "requires the admin role"})
    code, _, err = run(team, "org", "ci-token", "create", "deploy", "--org", ORG)
    assert code == 1 and "forbidden" in err and "admin" in err
    code, _, err = run(team, "org", "ci-token", "revoke", "not-an-id", "--org", ORG)
    assert code == 1 and "invalid CI token id" in err
    for bad in (("org", "ci-token"), ("org", "ci-token", "create"), ("org", "ci-token", "list", "x"),
                ("org", "ci-token", "revoke"), ("org", "ci-token", "rotate", "x"),
                ("org", "ci-token", "list", "--admin")):
        assert run(team, *bad)[0] == 2, bad


def test_ci_token_create_refuses_a_response_without_a_token(team):
    team.routes[f"POST /orgs/{ORG}/ci-tokens"] = lambda f, h: (200, {"token_id": "ct_1", "name": "x"})
    code, _, err = run(team, "org", "ci-token", "create", "x", "--org", ORG)
    assert code == 1 and "no CI token" in err


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


def test_the_members_effective_policy_carries_the_budgets(budgets):
    budgets.policy_body["effective"] = {**POLICY, "budget": {"seat_month_usd": 20, "goal_usd": None},
                                        "protected_paths": ["infra/", "*.lock", "secrets/"]}
    p = plugin(budgets).org_budgets()
    assert (p.budget_seat_month_usd, p.budget_goal_usd) == (20.0, None)
    assert p.protected_paths == ("infra/", "*.lock", "secrets/")
    assert p.spend_seat_usd == 12.5


def test_org_budgets_fail_closed_when_the_policy_was_never_fetched(budgets):
    budgets.routes[f"GET /orgs/{ORG}/policy"] = [auth.TransportError("down")]
    d = plugin(budgets).org_budgets()
    assert not d.allowed and "never been fetched" in d.reason


@pytest.mark.parametrize("over", [
    {"budget": "50"}, {"budget": {"goal_usd": True}}, {"protected_paths": ["x"] * 65},
])
def test_malformed_budget_fields_fail_closed(team, over):
    team.policy_body = {**team.policy_body, "policy": {**POLICY, **over}}
    assert not plugin(team).check_assign(assign()).allowed


def cfg_with(**budget):
    from types import SimpleNamespace

    return SimpleNamespace(budget=budget)


def org_policy(**kw):
    from brindle.pro.team_policy import OrgPolicy

    return OrgPolicy(org_id=ORG, version=1, **kw)


def test_an_org_budget_that_cant_be_read_exhausts_the_budget(monkeypatch):
    from brindle import budget
    from brindle.pro import team_policy
    from brindle.policy import deny

    monkeypatch.setattr(budget, "entitled", lambda: False)
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: deny("never been fetched"))
    lim = budget.limits(cfg_with(), REPO)
    assert (lim.month_usd, lim.goal_usd) == (0.0, 0.0)

    def boom(root):
        raise RuntimeError("keychain")

    monkeypatch.setattr(team_policy, "org_budgets", boom)
    assert budget.limits(cfg_with(), REPO).month_usd == 0.0


def test_the_orgs_count_of_the_seats_spend_joins_the_months(db, repo, monkeypatch):
    from brindle import budget
    from brindle.pro import team_policy

    monkeypatch.setattr(budget, "entitled", lambda: False)
    month = time.strftime("%Y-%m", time.gmtime())
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: org_policy(
        budget_seat_month_usd=50.0, spend_seat_usd=12.5, spend_month=month))
    gate = budget.Gate(db, cfg_with(), str(repo), None)
    assert gate.active and gate.month_spend() == 12.5
    # last month's number is not this month's
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: org_policy(
        budget_seat_month_usd=50.0, spend_seat_usd=12.5, spend_month="2000-01"))
    assert budget.Gate(db, cfg_with(), str(repo), None).month_spend() == 0.0


def test_a_remove_event_reports_what_the_worker_cost(budgets, monkeypatch):
    from brindle import budget, events
    from brindle.db import Agent

    monkeypatch.setattr(budget, "worker_spend", lambda db, agent, root: 1.234567)
    worker = Agent(AGENT, "ws1", "developer", "claude", None, "assign", "idle", "@0", None, time.time())
    assert events._cost("remove", worker, REPO) == 1.2346
    assert events._cost("merge", worker, REPO) is None            # only the end of a worker's task
    assert events._cost("remove", None, REPO) is None
    p = events_plugin(budgets)
    p.emit(ev("remove", cost_usd=1.2346))
    p.emit(ev("remove", cost_usd=1e9))                            # more than the backend takes
    p.emit(ev("remove", cost_usd=-1))
    assert p.flush()
    assert [e["cost_usd"] for e in budgets.events] == [1.2346, None, None]


def test_no_cost_is_priced_without_the_org_budgets_feature(monkeypatch):
    from brindle import budget, events
    from brindle.db import Agent
    from brindle.pro import license

    monkeypatch.setattr(license, "has", lambda feature: False)
    monkeypatch.setattr(budget, "worker_spend", lambda *a: pytest.fail("priced without the feature"))
    worker = Agent(AGENT, "ws1", "developer", "claude", None, "assign", "idle", "@0", None, time.time())
    assert events._cost("remove", worker, REPO) is None


# a real branch through the rule check's protected-paths gate


@pytest.fixture
def protected_branch(db, repo):
    from pathlib import Path

    from conftest import sh
    from brindle import workspaces

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
    assert result.summary() == "PASS `rules protected_paths`"
    commit(path, "infra/main.tf")
    commit(path, "deep/dir/poetry.lock")
    result = rule_checks.run(db, ws)
    assert not result.ok
    assert sorted(v.detail.split(" ")[0] for v in result.violations) == ["deep/dir/poetry.lock", "infra/main.tf"]
    assert result.summary().startswith("FAIL `rules protected_paths`")
    assert "protected path" in rule_checks.gate_problem(result, ws.branch)


def test_deleting_or_renaming_a_protected_file_fails_too(db, repo, monkeypatch):
    from pathlib import Path

    from conftest import sh
    from brindle import rule_checks, workspaces
    from brindle.pro import team_policy

    (repo / "infra").mkdir()
    (repo / "infra" / "a.tf").write_text("a\n")
    sh("git add -A && git commit -qm infra && git push -q origin main", repo)
    ws = workspaces.create(db, str(repo), "feat2").workspace
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: org_policy(protected_paths=("infra/",)))
    sh("git rm -q infra/a.tf && git commit -qm rm", Path(ws.path))
    result = rule_checks.run(db, ws)
    assert not result.ok and result.violations[0].detail.startswith("infra/a.tf ")


def test_protected_path_matching():
    from brindle.rule_checks import protected_files

    files = ["infra/a.tf", "src/infra/b.py", ".github/workflows/ci.yml", "Makefile", "docs/Makefile",
             "src/app.py", "INFRA/c.tf"]
    assert protected_files(files, ["infra"]) == ["infra/a.tf", "INFRA/c.tf"]
    assert protected_files(files, ["./.github/workflows/"]) == [".github/workflows/ci.yml"]
    assert protected_files(files, ["Makefile"]) == ["Makefile", "docs/Makefile"]
    assert protected_files(files, ["src/**/*.py"]) == ["src/infra/b.py", "src/app.py"]
    assert protected_files(files, []) == []


def test_without_protected_paths_nothing_is_checked(db, protected_branch, monkeypatch):
    from brindle import rule_checks
    from brindle.pro import team_policy

    ws, _ = protected_branch
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: None)
    assert rule_checks.run(db, ws) is None
    monkeypatch.setattr(team_policy, "org_budgets", lambda root: org_policy())
    assert rule_checks.run(db, ws) is None


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
