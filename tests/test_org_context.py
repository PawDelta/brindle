"""The org context: each read happens once per check, from the providers and
the home it was given."""

from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest

from brindle import permission_grants, permissions
from brindle.permissions import Request
from brindle.plan_policy import PlanTask
from brindle.policy import allow
from brindle.pro import credentials, license, status, team_policy
from brindle.pro._files import at_home
from brindle.pro.org_context import OrgContext, Providers
from brindle.pro.team_policy import GrantPolicy, OrgControl, OrgPolicy
from pro_fixtures import BASE, pro_env, signing_key, token  # noqa: F401 - fixtures

GRANTS = dict(allow=True, kinds=("bash",), overridable=(), max_minutes=60, max_uses=100, allow_persistent=True,
              lifetimes=("session", "time", "forever"), scopes=("branch",), org_name="Acme",
              controls=(OrgControl(rule="bash prefix terraform apply", decision="deny"),))
POLICY = OrgPolicy("org_acme", 1, permission_grants=GrantPolicy(**GRANTS))
ENT = SimpleNamespace(org_id="org_acme", features=frozenset({team_policy.FEATURE, "managed_rollout"}),
                      policy_version=1, role="member", policy_role=None)


def counted(calls, name, value):
    def read(*args, **kwargs):
        calls[name] += 1
        return value
    return read


def test_each_provider_is_read_once_per_check(tmp_path):
    calls = Counter()
    root = str(tmp_path)
    ctx = OrgContext.resolve(home=tmp_path, now=0.0, providers=Providers(
        policy=counted(calls, "policy", POLICY), status=counted(calls, "status", status.Saved(org_id="org_acme")),
        managed=counted(calls, "managed", {"device": "d1"}),
        plan_rules=counted(calls, "plan_rules", {"paths": ["infra/**"]})))
    req = Request("claude", "bash", "Bash", root, root, root, command="terraform apply")
    for _ in range(2):
        assert ctx.org_decision(req).decision == "deny"
        assert ctx.limits().allow and ctx.blocked_reason() is None and ctx.source() == "account"
        assert ctx.needs_plan(PlanTask(files=["infra/main.py"]), config=SimpleNamespace(plan="auto"))
        assert ctx.managed == {"device": "d1"}
    assert calls == {"policy": 1, "status": 1, "managed": 1, "plan_rules": 1}


@pytest.mark.parametrize("injected", [False, True])
def test_the_default_reads_happen_once_and_an_injected_status_replaces_the_file(tmp_path, monkeypatch, injected):
    calls = Counter()
    throttled = status.Saved(org_id="org_acme", status=status.Status(control={
        "id": "c1", "action": "throttle", "reason": "", "until": None, "throttle": {"max_parallel_workers": 1}}))
    read = counted(calls, "entitlement", ENT)
    monkeypatch.setattr(license, "current", license.request_cached(lambda *, refresh=True, store=None: read()))
    monkeypatch.setattr(team_policy, "load_cached", counted(calls, "cache", replace(POLICY, cached_for=("member", None))))
    monkeypatch.setattr(status, "load", counted(calls, "status", throttled))
    ctx = OrgContext.resolve(repo_root=str(tmp_path), providers=Providers(status=(lambda: throttled) if injected else None))
    assert ctx.policy.max_parallel_workers == 1          # narrowed by the context's own status
    assert ctx.limits().allow and ctx.blocked_reason() is None and ctx.source() == "account"
    assert calls == Counter(entitlement=1, cache=1, **({} if injected else {"status": 1}))


@pytest.mark.parametrize("decide", [
    lambda req, root: permissions.decide(req),
    lambda req, root: permission_grants._policy(req, root),     # the Inbox's (pending_asks, unblock_denied)
])
def test_one_permission_decision_reads_one_policy(tmp_path, monkeypatch, decide):
    """Two policies that each block ``terraform apply`` (a control, a deny):
    a decision that read one for its denies and the other for its controls
    would let it through."""
    by_control = POLICY
    by_deny = replace(POLICY, permission_grants=replace(POLICY.permission_grants, controls=(),
                                                        deny=("bash prefix terraform apply",)))
    reads = iter([by_control, by_deny, by_control, by_deny])
    monkeypatch.setattr(permission_grants, "_plugin", lambda repo: SimpleNamespace(policy=lambda: next(reads)))
    root = str(tmp_path)
    req = Request("claude", "bash", "Bash", root, root, root, command="terraform apply")
    assert decide(req, root).decision == "deny"
    assert next(reads) is by_deny                       # the decision read the policy once


def test_the_entitlement_is_checked_at_the_contexts_time(token):
    credentials.default_store().save({"entitlement": token(iat=1000, exp=3000, features=["managed_rollout"]),
                                      "base_url": BASE})
    shutdown = status.Saved(org_id="org_1", status=status.Status(control={
        "id": "c1", "action": "shutdown", "reason": "incident", "until": None}))
    ctx = OrgContext.resolve(now=2000.0, providers=Providers(policy=allow, status=lambda: shutdown))
    assert ctx.entitlement.org_id == "org_1" and "incident" in ctx.blocked_reason()


def test_a_context_reads_the_status_of_the_home_it_was_given(tmp_path, monkeypatch):
    monkeypatch.setattr(license, "current", lambda **kw: SimpleNamespace(org_id="org_acme", features=frozenset()))
    paused, other = tmp_path / "paused", tmp_path / "other"
    with at_home(paused):
        status.save(status.Saved(org_id="org_acme", status=status.Status(paused=True, paused_reason="audit")))
    assert "audit" in OrgContext.resolve(home=paused).blocked_reason()
    assert OrgContext.resolve(home=other).blocked_reason() is None


def _unreadable():
    raise OSError("disk gone")


@pytest.mark.parametrize("policy, airgap, allowed", [
    (lambda: allow(), "", None),              # no team org: no limits
    (lambda: POLICY, "1", True),              # air-gap mode's offline file is not the account's
    (_unreadable, "", False),                 # a policy that can't be read: no saved approval applies
])
def test_source_is_the_account_only_for_an_org_policy_read_online(tmp_path, monkeypatch, policy, airgap, allowed):
    monkeypatch.setenv("BRINDLE_AIRGAP", airgap)
    ctx = OrgContext.resolve(repo_root=str(tmp_path), providers=Providers(
        policy=policy, status=lambda: status.Saved(org_id="org_acme")))
    lim = ctx.limits()
    assert ctx.source() is None and (lim.allow if lim else None) is allowed
