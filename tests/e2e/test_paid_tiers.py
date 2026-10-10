"""End-to-end checks of the paid tiers (Pro, Team, Enterprise), driven through
the real CLI and the real plugins wherever they can be.

Nothing here touches the network or real billing: every brindle Pro request
goes to an in-memory ``Net`` (an unrouted request fails like an unreachable
backend), audit sinks post to a recorder, and entitlements are signed with a
throwaway key the license module is told to trust for the test. Fixtures live
in this file only.
"""

import csv
import hashlib
import hmac
import io
import json
import stat
import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from brindle import agents, airgap, budget, cli, doctor, events, plugins, policy, profiles, repos, workspaces
from brindle.config import load_repo_config
from brindle.db import Agent
from brindle.pro import (
    account, audit_chain, audit_export, auth, cost_centers, credentials, license, managed_models,
    org_profiles, rollout, team_policy,
)
from conftest import sh
from pro_fixtures import BASE, claims, pro_env, sign, signing_key  # noqa: F401 - fixtures

ORG = "org_acme"
RANK = {"pro": 0, "team": 1, "enterprise": 2}


def plan_features(plan: str) -> list[str]:
    """Every feature in the paid-feature table the plan includes."""
    return [f for f, p, _what, _how in account.FEATURES if RANK[p] <= RANK[plan]]


# -- the fake world ----------------------------------------------------------------------------------


class Net:
    """The brindle Pro backend: ``routes`` maps "METHOD /path" to (status, body)
    or a callable(form, headers). Anything else is an unreachable backend."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def request(self, method, url, form, headers):
        assert url.startswith(BASE), f"left the test backend: {url}"
        key = f"{method} {url[len(BASE):]}"
        self.calls.append(key)
        route = self.routes.get(key)
        if route is None:
            raise auth.TransportError(f"cannot reach brindle Pro backend ({key} is not routed)")
        return route(form or {}, headers) if callable(route) else route

    def route(self, key, status, body):
        self.routes[key] = (status, body)


class SinkNet:
    """Where audit sinks post: records every request, answers ``status``."""

    def __init__(self):
        self.posts = []
        self.status = 200

    def __call__(self, *a, **kw):
        return self

    def post(self, url, headers, body):
        self.posts.append((url, dict(headers), body))
        return self.status


@pytest.fixture(autouse=True)
def world(monkeypatch, signing_key, brindle_home):
    """No network, a clean air-gap and plugin state."""
    net, sinks = Net(), SinkNet()
    monkeypatch.setattr(auth, "UrllibTransport", lambda **kw: net)
    monkeypatch.setattr(audit_export, "UrllibTransport", sinks)
    # The exporter sends from a background thread as events arrive; tests ship by hand.
    monkeypatch.setattr(audit_export.AuditExporter, "_ensure_sender", lambda self: None)
    monkeypatch.delenv(airgap.ENV, raising=False)
    airgap.reset()
    plugins.reset()
    org_profiles.clear_memo()
    yield SimpleNamespace(net=net, sinks=sinks, key=signing_key, home=brindle_home)
    airgap.reset()
    plugins.reset()
    org_profiles.clear_memo()
    license.clear_cache()


@pytest.fixture
def net(world):
    return world.net


@pytest.fixture
def sinks(world):
    return world.sinks


@pytest.fixture
def acct(world, tmp_path):
    """Logs in (or writes an offline license) as a plan, with claims overridable."""

    def entitlement(plan="pro", **over):
        c = dict(org_id=ORG, plan=plan, features=plan_features(plan), role="admin", policy_version=3)
        c.update(over)
        return sign(world.key, claims(**c))

    def login(plan="pro", **over):
        credentials.default_store().save({
            "access_token": "at_1", "refresh_token": "rt_1", "access_expires_at": time.time() + 900,
            "entitlement": entitlement(plan, **over), "base_url": BASE, "org_id": over.get("org_id", ORG)})
        license.clear_cache()

    def license_file(plan="enterprise", name="license.json", **over):
        path = tmp_path / name
        path.write_text(json.dumps({"entitlement": entitlement(plan, **over)}))
        return path

    return SimpleNamespace(entitlement=entitlement, login=login, license_file=license_file)


@pytest.fixture
def run(repo, monkeypatch):
    """``run("account", ...)``: ``brindle <args>`` in the repo; (exit code, stdout+stderr)."""

    def go(*args, env=None):
        plugins.reset()      # the account plugin keeps the stdout it was built with: one CLI call per process
        monkeypatch.chdir(repo)
        result = CliRunner().invoke(cli.app, list(args), env=env)
        return result.exit_code, result.output

    return go


def feature_line(out: str, feature: str) -> str:
    (line,) = [ln for ln in out.splitlines() if ln.split()[:1] == [feature] or ln.split()[:2] == ["✓", feature]]
    return line


# -- brindle account: no license / Pro / Team / Enterprise -----------------------------------------------


def test_account_without_a_license_lists_every_feature_as_needing_its_plan(run, net):
    code, out = run("account")
    assert code == 0
    assert "not logged in" in out and "brindle is complete without it" in out
    for feature, plan, _what, _how in account.FEATURES:
        line = feature_line(out, feature)
        assert "✓" not in line and line.endswith(f"needs {plan.capitalize()}"), line
    assert "brindle account login" in out
    assert net.calls == []


def test_account_status_without_a_license_says_so(run):
    code, out = run("account", "status")
    assert code == 1 and "not logged in" in out


@pytest.mark.parametrize("plan,hint", [
    ("pro", "for a team"),
    ("team", "sales-led"),
    ("enterprise", None),
])
def test_account_marks_exactly_the_plans_features(run, acct, plan, hint):
    acct.login(plan)
    code, out = run("account")
    assert code == 0
    assert out.splitlines()[0].startswith(f"brindle {plan.capitalize()}: org {ORG}")
    have = set(plan_features(plan))
    for feature, need, _what, _how in account.FEATURES:
        line = feature_line(out, feature)
        if feature in have:
            assert line.lstrip().startswith("✓"), line
        else:
            assert "✓" not in line and line.endswith(f"needs {need.capitalize()}"), line
    if hint:
        assert hint in out.split("Next")[-1]
    else:
        assert "Next" not in out
    assert "brindle account status" in out


@pytest.mark.parametrize("plan", ["pro", "team", "enterprise"])
def test_account_status_reports_the_plan_offline(run, acct, plan):
    acct.login(plan)
    code, out = run("account", "status")
    assert code == 0, out
    assert f"plan      {plan} (5 seat(s))" in out
    assert f"org       {ORG} (admin)" in out
    features = next(ln for ln in out.splitlines() if ln.strip().startswith("features"))
    assert set(features.split(None, 1)[1].split(", ")) == set(plan_features(plan))
    assert "(offline: showing the stored entitlement)" in out


def test_an_expired_or_forged_entitlement_is_no_plan(run, acct):
    now = int(time.time())
    acct.login("enterprise", exp=now - 40 * 86400, iat=now - 41 * 86400)
    code, out = run("account")
    assert code == 0 and "expired" in out
    assert all("✓" not in ln for ln in out.splitlines())
    acct.login("enterprise")
    store = credentials.default_store()
    creds = store.load()
    creds["entitlement"] = creds["entitlement"][:-4] + "AAAA"          # the signature no longer matches
    store.save(creds)
    license.clear_cache()
    code, out = run("account")
    assert "signature" in out and all("✓" not in ln for ln in out.splitlines())


def test_offline_grace_keeps_the_plan_for_a_while_after_expiry(run, acct):
    now = int(time.time())
    acct.login("team", iat=now - 2 * 86400, exp=now - 3600)
    code, out = run("account")
    assert "(offline grace)" in out
    assert feature_line(out, "team").lstrip().startswith("✓")


def test_account_usage_errors_never_reach_the_backend(run, acct, net):
    acct.login("team")
    for args in (["bogus"], ["upgrade", "--team"], ["upgrade", "--seats", "3"], ["portal", "extra"],
                 ["org", "member", "policy-role", "x"], ["org", "profiles", "push"], ["--base-url"]):
        code, out = run("account", *args)
        assert code == 2 and "usage: brindle account" in out, (args, code, out)
    code, out = run("account", "--help")
    assert code == 0 and "org profiles push" in out and "license install" in out
    assert net.calls == []


# -- the offline license --------------------------------------------------------------------------------


def test_license_install_status_remove_round_trip(run, acct, brindle_home):
    path = acct.license_file("enterprise")
    code, out = run("account", "license", "install", str(path))
    assert code == 0 and "Installed offline license for org org_acme (plan enterprise" in out
    assert "air-gap mode is included" in out
    stored = brindle_home / "pro" / "license.jwt"
    assert stored.exists() and stat.S_IMODE(stored.stat().st_mode) == 0o600
    code, out = run("account", "license", "status")
    assert code == 0 and "offline license: active" in out and "plan      enterprise" in out
    code, out = run("account")                         # used when there is no login
    assert out.splitlines()[0].startswith("brindle Enterprise") and "✓ managed_rollout" in out
    code, out = run("account", "license", "remove")
    assert code == 0 and "Removed the offline license" in out and not stored.exists()
    code, out = run("account", "license", "status")
    assert code == 1 and "No offline license installed" in out


def test_license_without_airgap_says_so(run, acct):
    code, out = run("account", "license", "install", str(acct.license_file("team")))
    assert code == 0 and "doesn't include air-gap mode" in out


def test_a_bad_license_is_refused_and_stores_nothing(run, acct, tmp_path, brindle_home):
    cases = {
        "garbage": ("not a license", "malformed"),
        "empty": ("{}", "holds no entitlement"),
        "expired": (json.dumps({"entitlement": acct.entitlement(
            "enterprise", exp=int(time.time()) - 600, iat=int(time.time()) - 1200)}), "expired"),
    }
    for name, (text, why) in cases.items():
        path = tmp_path / f"{name}.json"
        path.write_text(text)
        code, out = run("account", "license", "install", str(path))
        assert code == 1 and why in out, (name, out)
        assert not (brindle_home / "pro" / "license.jwt").exists()
    code, out = run("account", "license", "install", str(tmp_path / "missing.json"))
    assert code == 1 and "cannot read" in out


def test_a_license_signed_by_an_unknown_key_is_refused(run, tmp_path, brindle_home):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = tmp_path / "rogue.json"
    path.write_text(json.dumps(
        {"entitlement": sign(Ed25519PrivateKey.generate(), claims(plan="enterprise", features=["airgap"]))}))
    code, out = run("account", "license", "install", str(path))
    assert code == 1 and "signature is invalid" in out
    assert not (brindle_home / "pro" / "license.jwt").exists()


# -- feature gating --------------------------------------------------------------------------------------


def test_cost_report_is_a_pro_feature(run, acct):
    code, out = run("cost", "report")
    assert code == 1 and "Pro" in out
    acct.login("pro")
    code, out = run("cost", "report")
    assert code == 0, out


def test_cost_estimate_needs_the_cost_feature(run, acct):
    code, out = run("cost", "estimate")
    assert code == 1 and "cost estimates are a brindle Pro feature" in out
    acct.login("pro")
    assert run("cost", "estimate")[0] == 0


def test_enterprise_only_commands_refuse_lower_plans(run, acct, brindle_home):
    code, out = run("cost", "request", "--usd", "5", "--reason", "launch")
    assert code == 1 and "Enterprise feature" in out
    code, out = run("cost", "requests")
    assert code == 1 and "Enterprise feature" in out
    configure_sinks(brindle_home)
    for plan in (None, "pro", "team"):
        if plan:
            acct.login(plan)
        code, out = run("audit", "ship")
        assert code == 2 and "doesn't include audit export" in out
        code, out = run("audit", "prune", "--days", "1")
        assert code == 2 and "doesn't include audit export" in out


def test_gating_is_by_feature_not_by_plan_name(run, acct):
    acct.login("enterprise", features=["team"])
    assert run("cost", "request", "--usd", "5", "--reason", "x")[0] == 1
    assert cost_centers.held() is False
    acct.login("team", features=["team", "cost_centers"])
    assert cost_centers.held() is True


# -- the org library (Team) -----------------------------------------------------------------------------


REVIEWER = "---\nname: org-reviewer\ndescription: the org's reviewer\nprovider: claude\n---\nOrg review rules.\n"
SECURITY = ("---\nname: security/org\ndescription: org security\ndeny_patterns:\n  - eval\\(\n---\n"
            "No eval.\n")


class Library:
    """The backend's profile library for ORG: signed on every answer, editable by push."""

    def __init__(self, world):
        self.world, self.version = world, 1
        self.items = {("profile", "org-reviewer"): (REVIEWER, False),
                      ("pack", "security/org"): (SECURITY, True)}
        world.net.routes[f"GET /orgs/{ORG}/profiles"] = lambda form, headers: (200, self.answer())
        world.net.routes[f"POST /orgs/{ORG}/profiles"] = self.post

    def listing(self, kind):
        return [{"kind": k, "name": n, "text": t, "pinned": pinned}
                for (k, n), (t, pinned) in sorted(self.items.items()) if k == kind]

    def answer(self):
        profiles_, packs = self.listing("profile"), self.listing("pack")
        digest = hashlib.sha256(org_profiles.canonical(ORG, self.version, profiles_, packs)).hexdigest()
        c = claims(org_id=ORG, sub="org:" + ORG, version=self.version, library_sha256=digest,
                   token_use="org_library")
        return {"org_id": ORG, "version": self.version, "profiles": profiles_, "packs": packs,
                "updated_at": 1, "updated_by": "u", "library_sha256": digest,
                "signed": sign(self.world.key, c, header={"typ": org_profiles.TYP}),
                "expires_at": c["exp"]}

    def post(self, form, headers):
        for it in form.get("delete", []):
            self.items.pop((it["kind"], it["name"]), None)
        for it in form.get("publish", []):
            self.items[(it["kind"], it["name"])] = (it["text"], bool(it.get("pinned")))
        self.version += 1
        return 200, {"version": self.version}


@pytest.fixture
def library(world):
    return Library(world)


def test_org_library_is_a_team_feature(run, acct, library, net):
    acct.login("pro")
    code, out = run("account", "org", "profiles")
    assert code == 0 and "has no profile library (plan pro)" in out
    assert f"GET /orgs/{ORG}/profiles" not in net.calls
    assert org_profiles.items("profile") == {}


def test_org_library_list_push_rm_and_use(run, acct, library, repo, tmp_path):
    acct.login("team")
    code, out = run("account", "org", "profiles")
    assert code == 0, out
    assert "Library for org org_acme, version 1" in out
    assert "org-reviewer" in out and "security/org  (pinned)" in out

    new = tmp_path / "auditor.md"
    new.write_text("---\nname: backend-auditor\ndescription: audits\nprovider: claude\n---\nAudit.\n")
    code, out = run("account", "org", "profiles", "push", str(new))
    assert code == 0 and "Published profile backend-auditor" in out and "version 2" in out
    pack = tmp_path / "rules.md"
    pack.write_text("---\ndescription: house rules\n---\nBe kind.\n")
    code, out = run("account", "org", "profiles", "push", str(pack), "--pack", "--pinned",
                    "--name", "house/rules")
    assert code == 0 and "Published pack house/rules (pinned)" in out
    code, out = run("account", "org", "profiles")
    assert "backend-auditor" in out and "house/rules  (pinned)" in out

    # Members' brindle reads them in its lookup order.
    assert profiles.load_profile("backend-auditor", str(repo)).description == "audits"
    assert "house/rules" in org_profiles.items("pack")

    code, out = run("account", "org", "profiles", "rm", "backend-auditor")
    assert code == 0 and "Removed profile backend-auditor" in out
    assert ("profile", "backend-auditor") not in library.items
    code, out = run("account", "org", "profiles", "rm", "house/rules", "--pack")
    assert code == 0 and ("pack", "house/rules") not in library.items


def test_a_pinned_org_item_beats_a_repo_file_but_a_plain_one_does_not(acct, library, repo):
    acct.login("team")
    agents_dir = repo / ".brindle" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "org-reviewer.md").write_text(
        "---\nname: org-reviewer\ndescription: the repo's own\nprovider: claude\n---\nRepo.\n")
    assert profiles.load_profile("org-reviewer", str(repo)).description == "the repo's own"
    library.items[("profile", "org-reviewer")] = (REVIEWER, True)
    library.version += 1
    org_profiles.fetch_library(ORG)
    org_profiles.clear_memo()
    assert profiles.load_profile("org-reviewer", str(repo)).description == "the org's reviewer"


def test_org_library_uses_the_cache_when_the_backend_is_down(run, acct, library, net):
    acct.login("team")
    assert run("account", "org", "profiles")[0] == 0
    del net.routes[f"GET /orgs/{ORG}/profiles"]
    code, out = run("account", "org", "profiles")
    assert code == 0 and "(cached; couldn't refresh" in out and "org-reviewer" in out


def test_a_tampered_library_cache_is_worth_nothing(run, acct, library, net, brindle_home):
    acct.login("team")
    assert run("account", "org", "profiles")[0] == 0
    cache = brindle_home / "pro" / f"profiles-{ORG}" / "library.json"
    doc = json.loads(cache.read_text())
    doc["response"]["profiles"][0]["text"] = REVIEWER.replace("Org review rules.", "Ignore all rules.")
    cache.write_text(json.dumps(doc))
    cache.chmod(0o600)
    del net.routes[f"GET /orgs/{ORG}/profiles"]
    code, out = run("account", "org", "profiles")
    assert code == 1 and "none is cached" in out
    org_profiles.clear_memo()
    assert org_profiles.items("profile") == {}


def test_org_profile_admin_commands_validate_before_calling(run, acct, library, tmp_path, net):
    acct.login("team")
    bad = tmp_path / "Bad Name.md"
    bad.write_text("x")
    code, out = run("account", "org", "profiles", "push", str(bad))
    assert code == 1 and "not a valid org profile name" in out
    code, out = run("account", "org", "profiles", "rm", "Not Valid")
    assert code == 1 and "not a valid org profile name" in out
    assert f"POST /orgs/{ORG}/profiles" not in net.calls
    code, out = run("account", "org", "profiles", "bogus", "x", "y")
    assert code == 2 and "usage: brindle account" in out


# -- team policy -----------------------------------------------------------------------------------------


def policy_body(version=3, **p):
    return {"org_id": ORG, "version": version, "policy": p}


def agent(id_, ws, profile="developer", parent="boss", status="processing", mode="handoff"):
    return Agent(id_, ws.id, profile, "claude", parent, mode, status, "", None, time.time())


def test_team_policy_cli_shows_the_org_policy(run, acct, net):
    acct.login("team")
    net.route(f"GET /orgs/{ORG}/policy", 200, {
        **policy_body(allowed_providers=["native"], require_human_review=True, max_parallel_workers=2,
                      roles={"intern": {"allowed_profiles": ["docs"]}}),
        "effective": {"allowed_providers": ["native"], "allowed_profiles": ["docs"],
                      "require_human_review": True, "max_parallel_workers": 2},
        "role": "member", "policy_role": "intern"})
    code, out = run("account", "org", "policy")
    assert code == 0, out
    assert "Policy for org org_acme, version 3" in out
    assert "providers              native" in out and "require human review   yes" in out
    assert "max parallel workers   2" in out
    assert "Role overrides" in out and "intern" in out
    assert "Your effective policy" in out and "profiles               docs" in out


def test_no_team_plan_means_nothing_is_enforced(run, acct, net, repo):
    acct.login("pro")
    net.route(f"GET /orgs/{ORG}/policy", 200, policy_body(allowed_providers=[]))
    code, out = run("account", "org", "policy")
    assert code == 0 and "has no team policy (plan pro)" in out
    cfg = load_repo_config(repo)
    assert policy.check_assign(cfg, str(repo), "developer", "t", "assign").allowed
    assert f"GET /orgs/{ORG}/policy" not in net.calls


def test_team_policy_gates_delegations_and_merges(acct, net, repo, db):
    acct.login("team", role="member")
    net.route(f"GET /orgs/{ORG}/policy", 200, policy_body(
        allowed_providers=["native", "claude"], allowed_profiles=["developer"],
        require_human_review=True, max_parallel_workers=1))
    cfg = load_repo_config(repo)
    ok = policy.check_assign(cfg, str(repo), "developer", "t", "assign", running_workers=0)
    assert ok.allowed, ok.reason
    d = policy.check_assign(cfg, str(repo), "reviewer", "t", "assign", running_workers=0)
    assert not d.allowed and "not allowed" in d.reason and "developer" in d.reason
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign", running_workers=1)
    assert not d.allowed and "at most 1 parallel worker" in d.reason
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign", running_workers=None)
    assert not d.allowed

    ws = workspaces.adopt_root(db, str(repo))
    assert policy.check_merge(cfg, ws, None).allowed                     # a person runs it
    boss = agent("boss", ws, "supervisor", None, mode="interactive")
    d = policy.check_merge(cfg, ws, None, actor=boss)
    assert not d.allowed and "requires a human review" in d.reason


def test_team_policy_roles_use_the_members_effective_policy(acct, net, repo):
    acct.login("team", role="member", policy_role="contractor")
    net.route(f"GET /orgs/{ORG}/policy", 200, {          # the backend sends the member's own policy
        **policy_body(allowed_providers=None, roles={"contractor": {"allowed_providers": ["native"]}}),
        "effective": {"allowed_providers": ["native"]}, "role": "member", "policy_role": "contractor"})
    cfg = load_repo_config(repo)
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign")
    assert not d.allowed and "allows only native" in d.reason


def test_a_policy_never_fetched_denies_everything_with_the_fix(acct, repo, db):
    acct.login("team")                     # the backend is unreachable and nothing is cached
    cfg = load_repo_config(repo)
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign")
    assert not d.allowed and "never been fetched" in d.reason and "brindle account org policy" in d.reason
    ws = workspaces.adopt_root(db, str(repo))
    assert not policy.check_merge(cfg, ws, None).allowed


def test_the_last_good_policy_is_used_when_the_backend_goes_away(acct, net, repo):
    acct.login("team", policy_version=3)
    net.route(f"GET /orgs/{ORG}/policy", 200, policy_body(allowed_providers=["native"]))
    cfg = load_repo_config(repo)
    assert not policy.check_assign(cfg, str(repo), "developer", "t", "assign").allowed
    del net.routes[f"GET /orgs/{ORG}/policy"]
    acct.login("team", policy_version=9)            # a newer policy exists but can't be fetched
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign")
    assert not d.allowed and "allows only native" in d.reason       # still the old rules


def test_a_demotion_does_not_keep_the_old_looser_policy(acct, net, repo):
    acct.login("team", role="admin")
    net.route(f"GET /orgs/{ORG}/policy", 200, policy_body(allowed_providers=None))
    cfg = load_repo_config(repo)
    assert policy.check_assign(cfg, str(repo), "developer", "t", "assign").allowed
    del net.routes[f"GET /orgs/{ORG}/policy"]
    acct.login("team", role="member")               # same version, different role, backend down
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign")
    assert not d.allowed and "never been fetched" in d.reason


def test_org_budgets_and_protected_paths_need_org_budgets(acct, net, repo):
    net.route(f"GET /orgs/{ORG}/policy", 200, policy_body(
        budget={"seat_month_usd": 50, "goal_usd": 10}, protected_paths=["infra/**"]))
    acct.login("team", features=["team"])
    assert team_policy.org_budgets(str(repo)) is None
    acct.login("team")
    p = team_policy.org_budgets(str(repo))
    assert p.budget_seat_month_usd == 50 and p.budget_goal_usd == 10
    assert p.protected_paths == ("infra/**",)


# -- Enterprise: cost centers --------------------------------------------------------------------------


def test_cost_center_request_approval_raises_the_month_limit(run, acct, net, repo):
    acct.login("enterprise")
    month = cost_centers.month_now()
    net.route(f"POST /orgs/{ORG}/cost-approvals", 201,
              {"id": "req_1", "status": "pending", "month": month, "cost_center": "platform"})
    net.route(f"GET /orgs/{ORG}/policy", 200, policy_body())
    cfg = load_repo_config(repo)
    cfg.budget = {"month_usd": 10}
    assert budget.limits(cfg, str(repo)).month_usd == 10

    code, out = run("cost", "request", "--usd", "5", "--reason", "launch week")
    assert code == 0 and "Filed request req_1 for $5.00" in out
    assert budget.limits(cfg, str(repo)).month_usd == 10                 # pending: nothing yet
    net.route(f"GET /orgs/{ORG}/cost-approvals/req_1", 200, {"status": "pending"})
    code, out = run("cost", "requests")
    assert code == 0 and "req_1" in out and "pending" in out

    net.route(f"GET /orgs/{ORG}/cost-approvals/req_1", 200,
              {"status": "approved", "amount_usd": 7.5, "month": month, "decision_note": "ok"})
    code, out = run("cost", "requests")
    assert code == 0 and "approved: +$7.50" in out
    assert budget.limits(cfg, str(repo)).month_usd == 17.5
    assert cost_centers.month_raise() == 7.5


def test_cost_center_approval_ends_with_the_plan_and_the_month(acct, net, repo):
    acct.login("enterprise")
    net.route(f"POST /orgs/{ORG}/cost-approvals", 201,
              {"id": "req_2", "status": "approved", "month": cost_centers.month_now()})
    cost_centers.request(3, "x", str(repo))
    assert cost_centers.month_raise() == 3
    assert cost_centers.month_raise(now=time.time() + 40 * 86400) == 0           # next month
    acct.login("team")
    assert cost_centers.month_raise() == 0                                      # the plan lapsed


def test_cost_center_request_validates(run, acct, net):
    acct.login("enterprise")
    code, out = run("cost", "request", "--usd", "0", "--reason", "x")
    assert code == 1 and "above 0" in out
    code, out = run("cost", "request", "--usd", "500000", "--reason", "x")
    assert code == 1 and "at most" in out
    net.route(f"POST /orgs/{ORG}/cost-approvals", 403, {"error": "forbidden"})
    code, out = run("cost", "request", "--usd", "5", "--reason", "x")
    assert code == 1 and "HTTP 403" in out


def test_a_denied_request_changes_nothing(acct, net, repo):
    acct.login("enterprise")
    month = cost_centers.month_now()
    net.route(f"POST /orgs/{ORG}/cost-approvals", 201, {"id": "req_3", "status": "pending", "month": month})
    cost_centers.request(5, "x", str(repo))
    net.route(f"GET /orgs/{ORG}/cost-approvals/req_3", 200, {"status": "denied", "month": month})
    assert cost_centers.poll() == ["[brindle cost] request req_3 was denied"]
    assert cost_centers.month_raise() == 0


# -- Enterprise: managed models -------------------------------------------------------------------------


def native_profile(name="local", **kw):
    return profiles.Profile(name=name, description="", provider="native", prompt="", api="openai",
                            model="qwen", **kw)


def claude_profile(name="dev", **kw):
    return profiles.Profile(name=name, description="", provider="claude", prompt="", **kw)


def org_policy(net, version=3, **p):
    net.route(f"GET /orgs/{ORG}/policy", 200, policy_body(version, **p))


def test_managed_models_route_every_agent_to_the_orgs_provider(acct, net, repo):
    acct.login("enterprise")
    org_policy(net, provider_config={"provider": "vertex", "region": "europe-west4",
                                     "project": "acme-ml", "model_ids": ["claude-sonnet-4"]},
               deny_personal_keys=True)
    m = agents.managed(str(repo))
    assert m.org_id == ORG and m.deny_personal_keys
    assert managed_models.agent_env(m, "claude") == {
        "CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "europe-west4",
        "ANTHROPIC_VERTEX_PROJECT_ID": "acme-ml", "ANTHROPIC_MODEL": "claude-sonnet-4"}
    assert "ANTHROPIC_API_KEY" in m.denied_keys and "OPENAI_API_KEY" in m.denied_keys
    assert managed_models.refusal(m, claude_profile()) is None
    why = managed_models.refusal(m, claude_profile(env={"ANTHROPIC_BASE_URL": "https://x.example"}))
    assert why and "ANTHROPIC_BASE_URL" in why and "Google Vertex AI" in why
    why = managed_models.refusal(m, claude_profile(env={"ANTHROPIC_API_KEY": "sk-personal"}))
    assert why and "personal API key" in why
    why = managed_models.refusal(m, native_profile(base_url="http://localhost:11434/v1"))
    assert why and "isn't Google Vertex AI" in why
    assert "Google Vertex AI" in managed_models.doctor_line(str(repo))[1]


def test_managed_models_openai_compatible_endpoint_for_native_and_codex(acct, net, repo):
    acct.login("enterprise")
    org_policy(net, provider_config={"provider": "openai-compatible",
                                     "endpoint": "https://llm.acme.example/v1",
                                     "model_ids": ["acme-large"]})
    m = agents.managed(str(repo))
    p = managed_models.apply_profile(m, native_profile())
    assert p.base_url == "https://llm.acme.example/v1" and p.model == "acme-large"
    assert managed_models.refusal(m, native_profile(base_url="https://elsewhere.example/v1"))
    assert managed_models.agent_env(m, "codex") == {"OPENAI_BASE_URL": "https://llm.acme.example/v1"}
    assert managed_models.refusal(m, claude_profile()) is not None            # Claude Code can't use it


def test_managed_models_are_ignored_without_the_feature(acct, net, repo):
    org_policy(net, provider_config={"provider": "bedrock", "region": "us-east-1"},
               deny_personal_keys=True)
    acct.login("team")
    assert agents.managed(str(repo)) is None
    assert managed_models.doctor_line(str(repo)) is None
    acct.login("team", features=plan_features("team") + ["managed_models"])
    assert agents.managed(str(repo)).config.provider == "bedrock"


def test_managed_models_fail_closed_when_the_policy_cannot_be_read(acct, repo):
    acct.login("enterprise")                      # no policy route, nothing cached
    with pytest.raises(agents.AgentError, match="managed models"):
        agents.managed(str(repo))
    ok, detail = managed_models.doctor_line(str(repo))
    assert not ok and "never been fetched" in detail


# -- Enterprise: managed rollout --------------------------------------------------------------------------


def test_rollout_kill_switch_stops_delegations_and_reviewers(acct, net, repo):
    acct.login("enterprise")
    org_policy(net, kill_switch=True)
    cfg = load_repo_config(repo)
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign")
    assert not d.allowed and "kill switch is on" in d.reason
    assert "kill switch" in rollout.kill_switch_reason(str(repo))
    acct.login("team")                                          # not entitled: ignored
    assert rollout.kill_switch_reason(str(repo)) is None
    assert policy.check_assign(cfg, str(repo), "developer", "t", "assign").allowed


def test_rollout_min_version_and_required_items(acct, net, repo, library):
    acct.login("enterprise")
    org_policy(net, min_version="999.0")
    cfg = load_repo_config(repo)
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign")
    assert not d.allowed and "requires brindle 999.0 or newer" in d.reason

    org_policy(net, min_version="0.0.1", required_profiles=["org-reviewer"],
               required_rule_packs=["security/org"])
    acct.login("enterprise", policy_version=4)
    org_profiles.clear_memo()
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign")
    assert d.allowed, d.reason
    # A required pack is applied to every profile, whatever it says.
    packs = profiles.load_rule_packs(claude_profile(), str(repo))
    assert [p.name for p in packs] == ["security/org"] and "eval" in packs[0].deny_patterns[0]

    org_policy(net, required_rule_packs=["not/there"])
    acct.login("enterprise", policy_version=5)
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign")
    assert not d.allowed and "not/there" in d.reason
    with pytest.raises(KeyError):
        profiles.load_rule_packs(claude_profile(), str(repo))


def test_rollout_sweep_stops_running_workers_under_the_kill_switch(acct, net, repo, db, monkeypatch):
    acct.login("enterprise")
    org_policy(net, kill_switch=True)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(agent("boss", ws, "supervisor", None, mode="interactive"))
    db.add_agent(agent("w1", ws))
    did = []
    monkeypatch.setattr(agents, "runs_process", lambda a: True)
    monkeypatch.setattr(agents, "pause_worker", lambda db_, a: did.append(a.id))
    monkeypatch.setattr(agents, "send_message", lambda db_, to, msg, sender_id=None: did.append(("told", to)))
    assert rollout.sweep(db) == ["stopped worker w1: org kill switch"]
    assert did == ["w1", ("told", "boss")]
    acct.login("team")
    assert rollout.sweep(db) == []


# -- the audit chain (Enterprise) ----------------------------------------------------------------------


def make_events(repo, n=3):
    cfg = load_repo_config(repo)
    for i in range(n):
        events.emit_denial(cfg, str(repo), "assign", f"reason {i}", branch=f"feat/{i}",
                           profile="developer", provider="claude", model="m", actor="boss")


def audit_log(repo):
    return audit_chain.log_path(str(repo))


def test_nothing_is_recorded_without_the_audit_feature_not_even_a_key(acct, repo, brindle_home):
    for plan in (None, "pro", "team"):
        if plan:
            acct.login(plan)
        make_events(repo)
    assert not audit_log(repo).exists()
    assert not (brindle_home / "audit" / "signing.key").exists()


def test_audit_chain_records_verifies_and_exports(run, acct, repo, brindle_home):
    acct.login("enterprise")
    make_events(repo, 3)
    path = audit_log(repo)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((brindle_home / "audit" / "signing.key").stat().st_mode) == 0o600

    code, out = run("audit", "verify")
    assert code == 0 and "3 record(s), chain intact, every signature valid" in out

    code, out = run("audit", "export")
    recs = [json.loads(ln) for ln in out.splitlines()]
    assert [r["seq"] for r in recs] == [1, 2, 3]
    assert recs[0]["prev_hash"] == audit_chain.ZERO_HASH
    assert recs[1]["prev_hash"] == audit_chain.record_hash(recs[0])
    assert recs[0]["event"]["kind"] == "deny_assign" and recs[0]["event"]["reason"] == "reason 0"
    assert recs[0]["event"]["actor"] == "boss"

    code, out = run("audit", "export", "--format", "csv")
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0][:2] == ["seq", "ts"] and len(rows) == 4 and rows[1][0] == "1"

    code, out = run("audit", "export", "--since", "2999-01-01T00:00:00Z")
    assert code == 0 and out == ""
    code, out = run("audit", "export", "--since", "last tuesday")
    assert code == 2 and "ISO 8601" in out
    code, out = run("audit", "export", "--format", "xml")
    assert code == 2 and "unknown export format" in out

    code, pub = run("audit", "pubkey")
    assert code == 0 and pub.strip() == audit_chain.public_key_hex()
    assert len(bytes.fromhex(pub.strip())) == 32


def test_the_denial_of_a_policy_check_lands_in_the_chain_with_its_reason(run, acct, net, repo):
    acct.login("enterprise")
    org_policy(net, allowed_providers=["native"])
    cfg = load_repo_config(repo)
    d = policy.check_assign(cfg, str(repo), "developer", "t", "assign")
    assert not d.allowed
    (rec,) = [json.loads(ln) for ln in run("audit", "export")[1].splitlines()]
    assert rec["event"]["kind"] == "deny_assign" and rec["event"]["reason"] == d.reason
    assert rec["event"]["profile"] == "developer" and rec["event"]["provider"] == "claude"


def test_audit_verify_catches_every_kind_of_tampering(run, acct, repo):
    acct.login("enterprise")
    make_events(repo, 4)
    path = audit_log(repo)
    original = path.read_bytes()
    lines = original.split(b"\n")[:-1]

    def broken(new_lines):
        path.write_bytes(b"\n".join(new_lines) + b"\n")
        try:
            return run("audit", "verify")
        finally:
            path.write_bytes(original)

    edited = json.loads(lines[1])
    edited["event"]["reason"] = "forged"
    code, out = broken([lines[0], audit_chain.canonical(edited).encode(), *lines[2:]])
    assert code == 1 and "BROKEN at seq 2" in out and "altered" in out
    code, out = broken([lines[0], *lines[2:]])                                  # one removed
    assert code == 1 and "BROKEN at seq 2" in out
    code, out = broken([lines[0], lines[2], lines[1], lines[3]])                # reordered
    assert code == 1 and "BROKEN at seq 2" in out
    code, out = broken(lines[:3])                                               # truncated tail
    assert code == 1 and "BROKEN at seq 4" in out and "truncated" in out
    resigned = json.loads(lines[2])
    resigned["sig"] = "00" * 64
    code, out = broken([*lines[:2], audit_chain.canonical(resigned).encode(), lines[3]])
    assert code == 1 and "BROKEN at seq 3" in out and "signature" in out
    assert run("audit", "verify")[0] == 0                                       # restored: intact


def test_verifying_and_exporting_outlive_the_plan(run, acct, repo):
    acct.login("enterprise")
    make_events(repo, 2)
    acct.login("pro")                                   # the plan lapsed
    assert run("audit", "verify")[0] == 0
    assert len(run("audit", "export")[1].splitlines()) == 2
    make_events(repo, 1)                                # and nothing more is written
    assert len(audit_chain.read_records(audit_log(repo))) == 2


def test_audit_with_no_log_yet(run, acct):
    code, out = run("audit", "verify")
    assert code == 0 and "no audit log at" in out
    code, out = run("audit", "export")
    assert code == 0 and out == ""


# -- audit export (Enterprise) ---------------------------------------------------------------------------


def configure_sinks(home, retention_days=None):
    cfg = {"sinks": [{"type": "webhook", "url": "https://siem.example.com/brindle",
                      "secret_env": "SIEM_SECRET"}]}
    if retention_days:
        cfg["retention_days"] = retention_days
    (home / "config.json").write_text(json.dumps({"audit_export": cfg}))


def test_audit_ship_posts_signed_batches_and_remembers_the_cursor(run, acct, repo, brindle_home,
                                                                  sinks, monkeypatch):
    monkeypatch.setenv("SIEM_SECRET", "s3cret")
    acct.login("enterprise")
    configure_sinks(brindle_home)
    make_events(repo, 3)
    code, out = run("audit", "ship")
    assert code == 0 and "sent 3" in out, out
    ((url, headers, body),) = sinks.posts
    assert url == "https://siem.example.com/brindle"
    mac = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers["X-Brindle-Signature"] == f"sha256={mac}"
    payload = json.loads(body)
    assert payload["source"] == "brindle" and [r["seq"] for r in payload["records"]] == [1, 2, 3]
    assert all(r["source"] == "audit_chain" and r["hash"] for r in payload["records"])
    code, out = run("audit", "ship")
    assert code == 0 and "sent 0" in out and len(sinks.posts) == 1
    make_events(repo, 1)
    code, out = run("audit", "ship")
    assert "sent 1" in out and len(sinks.posts) == 2


def test_a_failing_sink_exits_1_and_is_retried_from_disk(run, acct, repo, brindle_home, sinks, monkeypatch):
    monkeypatch.setenv("SIEM_SECRET", "s3cret")
    acct.login("enterprise")
    configure_sinks(brindle_home)
    make_events(repo, 2)
    sinks.status = 503
    code, out = run("audit", "ship", "--force")
    assert code == 1 and "FAILED" in out and "503" in out
    sinks.status = 200
    code, out = run("audit", "ship", "--force")
    assert code == 0 and "sent 2" in out


def test_audit_ship_says_when_another_process_is_already_sending(run, acct, repo, brindle_home, sinks,
                                                                 monkeypatch):
    monkeypatch.setenv("SIEM_SECRET", "s3cret")
    acct.login("enterprise")
    configure_sinks(brindle_home)
    make_events(repo, 1)
    with audit_export.AuditExporter()._sender() as mine:
        assert mine
        code, out = run("audit", "ship")
    assert code == 1 and "another brindle process is sending" in out and sinks.posts == []
    code, out = run("audit", "ship")
    assert code == 0 and "sent 1" in out


def test_audit_ship_reports_a_sink_that_cannot_be_set_up(run, acct, brindle_home, monkeypatch):
    monkeypatch.delenv("SIEM_SECRET", raising=False)
    acct.login("enterprise")
    configure_sinks(brindle_home)
    code, out = run("audit", "ship")
    assert code == 1 and "FAILED" in out and "SIEM_SECRET" in out
    (brindle_home / "config.json").write_text(json.dumps({"audit_export": {"sinks": [
        {"type": "webhook", "url": "http://siem.example.com/x", "secret_env": "SIEM_SECRET"}]}}))
    monkeypatch.setenv("SIEM_SECRET", "x")
    code, out = run("audit", "ship")
    assert code == 1 and "FAILED" in out and "https" in out.lower()
    (brindle_home / "config.json").write_text("{}")
    code, out = run("audit", "ship")
    assert code == 2 and "no sinks" in out


def test_prune_keeps_the_chain_verifiable_and_never_drops_unsent_records(run, acct, repo, brindle_home,
                                                                         monkeypatch):
    monkeypatch.setenv("SIEM_SECRET", "s3cret")
    acct.login("enterprise")
    configure_sinks(brindle_home)
    old = time.time() - 100 * 86400
    for i in range(3):
        audit_chain.append(str(repo), {"kind": "deny_assign", "reason": f"old {i}"},
                           ts=audit_chain._iso(old + i))
    make_events(repo, 1)
    code, out = run("audit", "prune", "--days", "30")
    assert code == 0 and "pruned 0 record(s)" in out          # no sink has them yet: all kept
    assert run("audit", "ship")[0] == 0
    code, out = run("audit", "prune", "--days", "30")
    assert code == 0 and "pruned 3 record(s) older than 30 day(s)" in out
    code, out = run("audit", "verify")
    assert code == 0 and "1 record(s)" in out and "seq 1-3 pruned" in out
    code, out = run("audit", "prune")
    assert code == 2 and "--days" in out


def test_retention_days_from_the_config_is_applied_when_shipping(run, acct, repo, brindle_home, monkeypatch):
    monkeypatch.setenv("SIEM_SECRET", "s3cret")
    acct.login("enterprise")
    configure_sinks(brindle_home, retention_days=1)
    old = time.time() - 5 * 86400
    for i in range(2):
        audit_chain.append(str(repo), {"kind": "merge"}, ts=audit_chain._iso(old + i))
    make_events(repo, 1)
    assert run("audit", "ship")[0] == 0
    code, out = run("audit", "verify")
    assert code == 0 and "1 record(s)" in out and "pruned" in out


# -- air-gapped mode (Enterprise) ------------------------------------------------------------------------


LOCAL = ("---\nname: local\nprovider: native\napi: openai\nmodel: qwen\n"
         "base_url: http://localhost:11434/v1\n---\nLocal.\n")
LAN = ("---\nname: lan\nprovider: native\napi: openai\nmodel: qwen\n"
       "base_url: http://192.168.1.20:8080/v1\n---\nLan.\n")
CLOUDY = ("---\nname: cloudy\nprovider: native\napi: openai\nmodel: gpt\n"
          "base_url: https://api.openai.com/v1\n---\nHosted.\n")
NAMED = ("---\nname: named\nprovider: native\napi: openai\nmodel: m\n"
         "base_url: http://llm.corp.internal/v1\nlocal: true\n---\nNamed.\n")
SNEAKY = "---\nname: sneaky\nprovider: claude\nlocal: true\n---\nHosted claude.\n"


def airgap_repo(repo, **cfg):
    d = repo / ".brindle"
    (d / "agents").mkdir(parents=True, exist_ok=True)
    for name, text in (("local", LOCAL), ("lan", LAN), ("cloudy", CLOUDY), ("named", NAMED),
                       ("sneaky", SNEAKY)):
        (d / "agents" / f"{name}.md").write_text(text)
    (d / "config.json").write_text(json.dumps({"airgap": True, "default_agent": "local", **cfg}))


def offline_policy(repo, **rules):
    (repo / ".brindle" / "policy.json").write_text(json.dumps(
        {"org_id": ORG, "version": 3, "policy": rules}))


def test_airgap_blocks_the_backend_and_uses_the_offline_license(run, acct, net, repo):
    acct.login("enterprise")                   # a login from before the machine was cut off
    code, out = run("account", "license", "install", str(acct.license_file("enterprise")))
    assert code == 0
    net.calls.clear()
    airgap_repo(repo)
    code, out = run("account", "status")
    assert code == 0, out
    assert "air-gap mode: showing the offline license" in out
    assert 'air-gap   on ("airgap": true in .brindle/config.json)' in out
    assert "!" not in out
    for sub in (["account", "sync"], ["account", "org", "list"], ["account", "upgrade"],
                ["account", "org", "learning-share"]):
        code, out = run(*sub)
        assert "air-gap" in out.lower(), (sub, code, out)
    assert net.calls == [], "air-gap mode must never reach the backend"


def test_airgap_org_policy_shows_the_offline_file(run, acct, repo, net):
    code, _ = run("account", "license", "install", str(acct.license_file("enterprise", role="member")))
    assert code == 0
    airgap_repo(repo)
    code, out = run("account", "org", "policy")
    assert code == 1 and "no usable offline policy" in out and ".brindle/policy.json" in out
    offline_policy(repo, allowed_providers=["native"], require_human_review=True)
    code, out = run("account", "org", "policy")
    assert code == 0, out
    assert "Policy for org org_acme, version 3 (offline:" in out
    assert "providers              native" in out and "require human review   yes" in out
    assert net.calls == []


def test_airgap_without_the_license_feature_still_blocks_and_warns(run, acct, repo):
    code, out = run("account", "license", "install", str(acct.license_file("team")))
    assert code == 0
    airgap_repo(repo)
    code, out = run("account", "status")
    assert "plan doesn't include it" in out and "feature 'airgap'" in out
    assert not airgap.licensed() and airgap.enabled()
    checks = {c.name: c for c in doctor.airgap_checks(str(repo))}
    assert checks["air-gap"].level == doctor.WARN


def test_airgap_from_the_environment_shows_in_the_license_status(run, repo):
    code, out = run("account", "license", "status", env={airgap.ENV: "1"})
    assert code == 1 and f"air-gap   on ({airgap.ENV}=1)" in out


def test_a_repo_config_cannot_turn_airgap_back_off(repo, tmp_path):
    airgap_repo(repo)
    other = tmp_path / "other"
    (other / ".brindle").mkdir(parents=True)
    (other / ".brindle" / "config.json").write_text(json.dumps({"airgap": False}))
    assert load_repo_config(repo).airgap
    assert not load_repo_config(other).airgap
    assert airgap.enabled()                       # armed for the process: the second repo can't undo it


def test_airgap_delegates_only_to_local_profiles(acct, repo):
    acct.login("enterprise")
    airgap_repo(repo)
    offline_policy(repo)                         # Enterprise includes the Team policy: it needs its file
    cfg = load_repo_config(repo)
    for name in ("local", "lan", "named"):
        assert policy.check_assign(cfg, str(repo), name, "t", "assign").allowed, name
    for name, why in (("cloudy", "not on this machine"), ("developer", "hosted service"),
                      ("sneaky", "`local: true` is ignored"), ("nonexistent", "couldn't be loaded")):
        d = policy.check_assign(cfg, str(repo), name, "t", "assign")
        assert not d.allowed and why in d.reason, (name, d.reason)
    assert airgap.hosted_profiles(str(repo)).count("cloudy") == 1
    # Every launch path passes the same check, the chat included.
    with pytest.raises(agents.AgentError, match="air-gap mode"):
        agents._airgap_check(claude_profile(), str(repo))
    agents._airgap_check(native_profile(base_url="http://127.0.0.1:8000/v1"), str(repo))


def test_launching_a_hosted_agent_in_airgap_mode_is_refused_before_anything_starts(acct, repo, db):
    license.install(acct.license_file("enterprise").read_bytes())
    airgap_repo(repo)
    offline_policy(repo)
    load_repo_config(repo)
    ws = workspaces.adopt_root(db, str(repo))
    for profile in ("developer", "sneaky", "cloudy"):
        a = agent("w-" + profile, ws, profile)
        db.add_agent(a)
        with pytest.raises(agents.AgentError, match="air-gap mode: profile .* is refused"):
            agents._launch(db, a, ws, prompt=None, resume=None, watch_pane=False)


def test_launching_under_managed_models_refuses_a_profile_that_points_elsewhere(acct, net, repo, db):
    acct.login("enterprise")
    org_policy(net, provider_config={"provider": "bedrock", "region": "us-east-1"})
    (repo / ".brindle" / "agents").mkdir(parents=True)
    (repo / ".brindle" / "agents" / "elsewhere.md").write_text(
        "---\nname: elsewhere\nprovider: claude\nenv.ANTHROPIC_BASE_URL: https://other.example\n---\nX.\n")
    ws = workspaces.adopt_root(db, str(repo))
    a = agent("w1", ws, "elsewhere")
    db.add_agent(a)
    with pytest.raises(agents.AgentError, match="managed models: .*ANTHROPIC_BASE_URL.*AWS Bedrock"):
        agents._launch(db, a, ws, prompt=None, resume=None, watch_pane=False)


def test_the_kill_switch_also_stops_reviewers(acct, net, repo, db):
    acct.login("enterprise")
    org_policy(net, kill_switch=True)
    ws = workspaces.adopt_root(db, str(repo))
    with pytest.raises(agents.AgentError, match="No reviewer started: org org_acme: your org's kill switch"):
        agents.request_review(db, None, ws, cfg=load_repo_config(repo))


def test_airgap_denials_are_audited_locally(run, acct, repo):
    code, _ = run("account", "license", "install", str(acct.license_file("enterprise")))
    assert code == 0
    airgap_repo(repo)
    cfg = load_repo_config(repo)
    assert not policy.check_assign(cfg, str(repo), "developer", "t", "assign").allowed
    code, out = run("audit", "export")
    recs = [json.loads(ln) for ln in out.splitlines()]
    assert recs and recs[-1]["event"]["kind"] == "deny_assign"
    assert "air-gap mode" in recs[-1]["event"]["reason"]
    assert run("audit", "verify")[0] == 0


def test_airgap_team_policy_comes_from_the_offline_file(run, acct, repo, net, db):
    code, out = run("account", "license", "install", str(acct.license_file("enterprise", role="member")))
    assert code == 0
    airgap_repo(repo)
    cfg = load_repo_config(repo)
    d = policy.check_assign(cfg, str(repo), "local", "t", "assign")
    assert not d.allowed and ".brindle/policy.json" in d.reason          # no file: refused
    ws = workspaces.adopt_root(db, str(repo))
    assert not policy.check_merge(cfg, ws, None).allowed
    (repo / ".brindle" / "policy.json").write_text(json.dumps(
        {"org_id": ORG, "version": 3, "policy": {"allowed_providers": ["native"], "allowed_models": None,
                                                 "require_human_review": True,
                                                 "max_parallel_workers": 4}}))
    assert policy.check_assign(cfg, str(repo), "local", "t", "assign", running_workers=0).allowed
    assert not policy.check_assign(cfg, str(repo), "local", "t", "assign", running_workers=4).allowed
    assert policy.check_merge(cfg, ws, None).allowed
    (repo / ".brindle" / "policy.json").write_text(json.dumps({"org_id": "org_other", "version": 1,
                                                               "policy": {}}))
    assert not policy.check_assign(cfg, str(repo), "local", "t", "assign").allowed   # another org's
    assert net.calls == []


def test_airgap_never_ships_audit_records_or_fetches_the_org_library(run, acct, net, repo, brindle_home,
                                                                     sinks, monkeypatch, library):
    monkeypatch.setenv("SIEM_SECRET", "s")
    code, _ = run("account", "license", "install", str(acct.license_file("enterprise")))
    assert code == 0
    configure_sinks(brindle_home)
    airgap_repo(repo)
    load_repo_config(repo)
    make_events(repo, 2)                                          # recorded locally...
    code, out = run("audit", "ship")
    assert code == 2 and "air-gap mode is on" in out and "nothing is sent" in out
    assert sinks.posts == [] and net.calls == []                  # ...and never shipped
    org_profiles.clear_memo()
    assert org_profiles.items("profile") == {}                    # no cached copy, nothing fetched
    assert net.calls == []


def test_doctor_reports_airgap_state(acct, repo):
    assert [c.detail for c in doctor.airgap_checks(str(repo))] == ["off"]
    license.install(acct.license_file("enterprise").read_bytes())
    airgap_repo(repo)
    checks = {c.name: c for c in doctor.airgap_checks(str(repo))}
    assert checks["air-gap"].level == doctor.OK and "no outbound traffic" in checks["air-gap"].detail
    assert checks["hosted profiles"].level == doctor.WARN and "cloudy" in checks["hosted profiles"].detail
    assert checks["default agent"].level == doctor.OK
    assert checks["offline license"].level == doctor.OK and "plan enterprise" in checks["offline license"].detail
    assert checks["offline policy"].level == doctor.WARN
    airgap_repo(repo, default_agent="developer")
    assert {c.name: c for c in doctor.airgap_checks(str(repo))}["default agent"].level == doctor.FAIL


def test_airgap_hosts():
    for host in ("localhost", "foo.localhost", "127.0.0.1", "::1", "10.1.2.3", "172.16.0.1",
                 "172.31.255.255", "192.168.0.9"):
        assert airgap.is_local_host(host), host
    for host in ("example.com", "8.8.8.8", "172.32.0.1", "0.0.0.0", "", None, "2001:db8::1"):
        assert not airgap.is_local_host(host), host


# -- several repos in one session (Pro) ----------------------------------------------------------------------


def second_repo(tmp_path, name="web"):
    work = tmp_path / name
    work.mkdir()
    sh("git init -q -b main", work)
    (work / "app.py").write_text("print('web')\n")
    sh("git add -A && git commit -qm init", work)
    return work


@pytest.fixture
def session(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "@0",
                       None, time.time()))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    monkeypatch.setattr(agents, "send_message", lambda *a, **kw: None)
    return "boss"


def test_repo_add_needs_pro_then_attaches_lists_and_detaches(run, acct, db, repo, session, tmp_path):
    web = second_repo(tmp_path)
    code, out = run("repo", "add", str(web), "--name", "web")
    assert code == 1 and "restricted feature of brindle Pro" in out
    assert db.session_repos(session) == []
    code, out = run("repo", "ls")
    assert code == 0 and "No repos attached" in out

    acct.login("pro")
    code, out = run("repo", "add", str(web), "--name", "web")
    assert code == 0 and f"attached {web} as web" in out
    code, out = run("repo", "ls")
    assert code == 0 and "web" in out and str(web) in out
    code, out = run("repo", "add", str(web), "--name", "web2")
    assert code == 1 and "already attached" in out
    code, out = run("repo", "add", str(repo))
    assert code == 1 and "this session's own repo" in out
    code, out = run("repo", "add", str(tmp_path))
    assert code == 1 and "not a git repository" in out
    code, out = run("repo", "rm", "web")
    assert code == 0 and "detached web" in out
    code, out = run("repo", "rm", "web")
    assert code == 1 and "no attached repo" in out
    assert db.session_repos(session) == []


def test_a_lapsed_plan_cannot_use_an_attached_repo(run, acct, db, repo, session, tmp_path):
    web = second_repo(tmp_path)
    acct.login("pro")
    assert run("repo", "add", str(web), "--name", "web")[0] == 0
    assert repos.resolve(db, session, str(repo), "web") == str(web)
    acct.login("team", features=["team"])                        # the plan no longer has multi_repo
    with pytest.raises(repos.RepoError, match="restricted feature of brindle Pro"):
        repos.resolve(db, session, str(repo), "web")
    assert repos.resolve(db, session, str(repo), None) == str(repo)    # the own repo still works


def test_a_repo_held_by_a_running_session_cannot_be_attached(run, acct, db, repo, session, tmp_path,
                                                             monkeypatch):
    web = second_repo(tmp_path)
    other_root = second_repo(tmp_path, "other")
    ows = workspaces.adopt_root(db, str(other_root))
    db.add_agent(Agent("other", ows.id, "supervisor", "claude", None, "interactive", "processing", "@1",
                       None, time.time()))
    acct.login("pro")
    repos.attach(db, "other", str(web), "web")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id in ("boss", "other"))
    code, out = run("repo", "add", str(web))
    assert code == 1 and "another running session" in out
