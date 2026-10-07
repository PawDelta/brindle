"""The paid-feature table (``brindle account``), and the client-side
entitlement gate on setting up brindle CI (``ci`` / ``ci_fix``)."""
import io
import os
import subprocess
import time

import pytest

from brindle import ci_client
from brindle.ci_client import CIError
from brindle.pro import account, auth, credentials, license, settings_sync
from conftest import sh
from pro_fixtures import (  # noqa: F401 - fixtures
    BASE, FakeTransport, backend, pro_env, signing_key, token,
)

REPO = "acme/widgets"
CI_TOKEN = "cpc_" + "c" * 32
PLANS = ("pro", "team", "enterprise")


def rows():
    return {feature: plan for feature, plan, _what, _how in account.FEATURES}


# -- the FEATURES table -------------------------------------------------------------------------------


@pytest.mark.parametrize("feature,plan", [
    ("learning", "pro"), ("services", "pro"), ("multi_repo", "pro"), ("settings_sync", "pro"),
    ("ci_fix", "pro"), ("learned_rules", "pro"), ("cost", "pro"), ("guardrails", "pro"),
    ("team", "team"), ("ci", "team"), ("org_profiles", "team"), ("org_budgets", "team"),
    ("audit", "enterprise"), ("airgap", "enterprise"), ("managed_models", "enterprise"),
    ("audit_export", "enterprise"), ("ci_enterprise", "enterprise"), ("cost_centers", "enterprise"),
    ("managed_rollout", "enterprise"),
])
def test_feature_row(feature, plan):
    assert rows()[feature] == plan


def test_feature_rows_are_well_formed():
    names = [r[0] for r in account.FEATURES]
    assert len(names) == len(set(names)), "each feature listed once"
    for feature, plan, what, how in account.FEATURES:
        assert plan in PLANS, feature
        assert what and how and len(what) <= 50, feature
        assert "$" not in what + how, "no prices in the client"
    # Cheapest plan first: Pro rows, then Team, then Enterprise.
    order = [PLANS.index(r[1]) for r in account.FEATURES]
    assert order == sorted(order)


def test_gated_features_are_listed():
    """Features the client already gates on are in the table under the name it checks."""
    assert settings_sync.FEATURE in rows()
    assert "fix" in ci_client.SETUP_KINDS and rows()["ci_fix"] == "pro"


def test_bare_account_lists_every_row_when_logged_out(tmp_path):
    out = io.StringIO()
    acct = account.ProAccount("/repo", out=out, err=io.StringIO(),
                              store=credentials.FileStore(tmp_path / "pro"), transport=FakeTransport({}))
    assert acct.run([]) == 0
    text = out.getvalue()
    for feature, plan, _what, _how in account.FEATURES:
        line = next(ln for ln in text.splitlines() if ln.split()[:1] == [feature])
        assert line.endswith(f"needs {plan.capitalize()}"), line


def test_bare_account_marks_the_entitled_rows(tmp_path, backend, token):
    store = logged_in(tmp_path, backend, token(features=["learning", "ci_fix", "settings_sync"]))
    out = io.StringIO()
    acct = account.ProAccount("/repo", out=out, err=io.StringIO(), store=store, transport=backend)
    assert acct.run(["features"]) == 0
    text = out.getvalue()
    assert "✓ ci_fix" in text and "✓ settings_sync" in text
    assert "needs Team" in text and "needs Enterprise" in text


# -- license.require with an explicit store -------------------------------------------------------


def logged_in(tmp_path, backend, entitlement):
    t = backend.issue()
    store = credentials.FileStore(tmp_path / "pro")
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + 900, "entitlement": entitlement, "base_url": BASE})
    return store


def test_require_reads_the_given_store(tmp_path, backend, token):
    store = logged_in(tmp_path, backend, token(features=["ci"]))
    client = auth.Client(BASE, backend)
    assert license.require("ci", store=store, client=client).org_id == "org_1"
    with pytest.raises(license.NotEntitled, match="does not include 'audit'"):
        license.require("audit", store=store, client=client)


# -- the ci gate ----------------------------------------------------------------------------------------


def gate(tmp_path, backend, token, org="org_1", **claims):
    store = logged_in(tmp_path, backend, token(**claims))
    return ci_client.require_ci(store=store, client=auth.Client(BASE, backend), org=org)


def test_ci_gate_team_sets_up_every_workflow(tmp_path, backend, token):
    assert gate(tmp_path, backend, token, plan="team", features=["ci", "team"]) == ci_client.SETUP_KINDS


def test_ci_gate_pro_ci_fix_sets_up_only_the_fix_workflow(tmp_path, backend, token):
    assert gate(tmp_path, backend, token, features=["learning", "ci_fix"]) == ("fix",)


def test_ci_gate_refuses_a_plan_without_ci(tmp_path, backend, token):
    with pytest.raises(CIError, match="needs brindle Team") as e:
        gate(tmp_path, backend, token, features=["learning"])
    assert e.value.code == "not_entitled"


def test_ci_gate_fails_closed_when_logged_out(tmp_path):
    with pytest.raises(CIError, match="needs brindle Team: not logged in") as e:
        ci_client.require_ci(store=credentials.FileStore(tmp_path / "pro"),
                             client=auth.Client(BASE, FakeTransport({})))
    assert e.value.code == "not_entitled"


def test_ci_gate_fails_closed_on_an_expired_entitlement(tmp_path, backend, token):
    backend.routes["POST /token/refresh"] = [(400, {"error": "invalid_grant"})]
    old = int(time.time()) - 30 * 86400
    with pytest.raises(CIError, match="needs brindle Team"):
        gate(tmp_path, backend, token, features=["ci"], iat=old - 3600, exp=old)


def test_ci_gate_fails_closed_on_anything_unexpected(monkeypatch):
    def boom(**kw):
        raise RuntimeError("keychain exploded")
    monkeypatch.setattr(license, "current", boom)
    with pytest.raises(CIError, match="needs brindle Team: could not check") as e:
        ci_client.require_ci()
    assert "keychain" not in str(e.value) and e.value.code == "not_entitled"


def test_ci_gate_refuses_another_org(tmp_path, backend, token):
    with pytest.raises(CIError, match="brindle account org use org_2"):
        gate(tmp_path, backend, token, org="org_2", features=["ci"])


# -- init behind the gate ---------------------------------------------------------------------------


class Plugin:
    def __init__(self, store, transport):
        self.store, self.transport = store, transport

    def _team_org(self, org):
        return org or "org_1"

    def _client(self, base):
        return auth.Client(BASE, self.transport)


class Proc:
    def __init__(self, out=""):
        self.stdout, self.stderr, self.returncode = out, "", 0


@pytest.fixture
def ci_repo(repo, monkeypatch):
    monkeypatch.chdir(repo)
    sh(f"git remote set-url origin https://github.com/{REPO}.git", repo)
    return repo


def test_init_refuses_before_anything_without_the_entitlement(tmp_path, ci_repo, backend, token, monkeypatch):
    store = logged_in(tmp_path, backend, token(features=["learning"]))
    monkeypatch.setattr(auth, "create_ci_token", lambda *a: pytest.fail("no CI token without the entitlement"))
    before = sh("git rev-parse --abbrev-ref HEAD", ci_repo)

    def run(argv, **kw):
        raise AssertionError("gh must not run")
    said = []
    with pytest.raises(CIError, match="needs brindle Team"):
        ci_client.init(repo=REPO, org=None, providers=["claude"], cwd=str(ci_repo), env={}, run=run,
                       open_url=lambda url: pytest.fail("nothing opened"), account=Plugin(store, backend),
                       client=ci_client.Client(BASE, FakeTransport({})), credential="key", say=said.append)
    assert not said and sh("git rev-parse --abbrev-ref HEAD", ci_repo) == before


def test_init_with_ci_fix_fetches_only_the_fix_workflow(tmp_path, ci_repo, backend, token, monkeypatch):
    from brindle import git as git_mod

    store = logged_in(tmp_path, backend, token(features=["learning", "ci_fix"]))
    monkeypatch.setattr(auth, "create_ci_token", lambda client, s, org, name: {
        "token": CI_TOKEN, "token_id": "ct_1", "org_id": org, "name": name})
    real_run = git_mod.run
    pushed = {}

    def fake_git(args, cwd, check=True):
        if args[0] != "push":
            return real_run(args, cwd, check)
        # init deletes the setup branch once pushed: read what it pushed now
        tree = real_run(["ls-tree", "-r", "--name-only", "HEAD"], cwd).stdout.split()
        pushed.update({p: real_run(["show", f"HEAD:{p}"], cwd).stdout for p in tree if p.startswith(".github/")})
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(git_mod, "run", fake_git)

    def run(argv, **kw):
        if argv[:2] == ["gh", "api"] and argv[2].startswith("repos/"):
            return Proc("true\n")
        if argv[:2] == ["gh", "api"]:
            return Proc("Organization\n")
        if argv[:3] == ["gh", "pr", "create"]:
            return Proc("https://gh.test/pr/1\n")
        return Proc()
    t = FakeTransport({"GET /ci/workflow?kind=fix": [(200, {"text": "name: fix\n"})]})
    ci_client.init(repo=REPO, org=None, providers=["claude"], cwd=str(ci_repo),
                   env={"PATH": os.environ["PATH"]}, run=run, open_url=lambda url: None,
                   account=Plugin(store, backend), client=ci_client.Client(BASE, t), credential="key",
                   say=lambda s: None)
    assert t.paths() == ["GET /ci/workflow?kind=fix"]
    # init leaves the person on their own branch; the workflows went out on the setup branch
    assert pushed == {".github/workflows/brindle-ci-fix.yml": "name: fix\n"}


def test_no_paid_feature_is_still_coming_soon():
    from brindle.pro import account
    assert not [f for f, *_, how in account.FEATURES if "coming soon" in how]


def test_enterprise_points_at_the_sales_page():
    from brindle.pro import account
    assert account.ENTERPRISE_URL.endswith("/brindle/enterprise")
