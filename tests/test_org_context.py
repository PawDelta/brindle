"""The org context: each injected provider is what a check reads, and only once."""

from collections import Counter
from types import SimpleNamespace

import pytest

from brindle.permissions import Request
from brindle.plan_policy import PlanTask
from brindle.policy import allow
from brindle.pro import status
from brindle.pro.org_context import OrgContext, Providers
from brindle.pro.team_policy import GrantPolicy, OrgControl, OrgPolicy

GRANTS = dict(allow=True, kinds=("bash",), overridable=(), max_minutes=60, max_uses=100, allow_persistent=True,
              lifetimes=("session", "time", "forever"), scopes=("branch",), org_name="Acme",
              controls=(OrgControl(rule="bash prefix terraform apply", decision="deny"),))
POLICY = OrgPolicy("org_acme", 1, permission_grants=GrantPolicy(**GRANTS))


def test_each_provider_is_read_once_per_check(tmp_path):
    calls = Counter()

    def read(name, value):
        def provider():
            calls[name] += 1
            return value
        return provider

    root = str(tmp_path)
    ctx = OrgContext.resolve(home=tmp_path, now=0.0, repo_root=root, providers=Providers(
        policy=read("policy", POLICY), status=read("status", status.Saved(org_id="org_acme")),
        managed=read("managed", {"device": "d1"}), plan_rules=read("plan_rules", {"paths": ["infra/**"]})))
    req = Request("claude", "bash", "Bash", root, root, root, command="terraform apply")
    for _ in range(2):
        assert ctx.org_decision(req).decision == "deny"
        assert ctx.limits().allow and ctx.blocked_reason() is None and ctx.source() == "account"
        assert ctx.needs_plan(PlanTask(files=["infra/main.py"]), config=SimpleNamespace(plan="auto"))
        assert ctx.managed == {"device": "d1"}
    assert calls == {"policy": 1, "status": 1, "managed": 1, "plan_rules": 1}


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
