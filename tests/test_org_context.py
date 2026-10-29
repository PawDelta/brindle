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
from test_pro_rollout import worker  # noqa: F401 - database worker with fake process operations

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
        plan_rules=counted(calls, "plan_rules", {"plan_paths": ["infra/**"]})))
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
    assert ctx.source() == ("offline" if airgap else None) and (lim.allow if lim else None) is allowed


@pytest.mark.parametrize("seam", ["policy_source", "managed_stricter", "audit_sinks", "plan_rules"])
def test_governance_wiring(tmp_path, monkeypatch, signing_key, seam):
    import json

    from brindle import airgap
    from brindle.pro import audit_chain, audit_stream, auth, managed
    from pro_fixtures import ISS, TEST_KID, sign
    from test_managed_device import NOW, answer, managed_file

    sink = {"kind": "https", "url": "https://siem.example.test/events"}
    device_sink = {"kind": "syslog", "url": "udp://localhost:514"}
    body = {"version": 3, "policy": {"plan_paths": ["finance/**"], "audit_sink": sink,
            "max_parallel_workers": 1, "permission_grants": {**GRANTS, "controls": [
                {"rule": "bash prefix terraform apply", "decision": "deny"}]}}}
    ent = SimpleNamespace(**{**vars(ENT), "org_id": "org_member", "sub": "user_1", "policy_signed": True,
                             "plan": "enterprise"})
    remote_status = {"policy_version": 3, "paused": False}

    def response(kind):
        data = body if kind == "policy" else remote_status
        claims = {"iss": ISS, "aud": "brindle-pro", "sub": ent.sub, "org_id": ent.org_id,
                  "role": ent.role, "policy_role": None, "policy_version": 3, "iat": NOW - 60,
                  "exp": NOW + 3600, "kid": TEST_KID, "token_use": kind, "doc_type": kind,
                  "document_sha256": managed.document_sha256(data)}
        return json.loads(json.dumps({**data, "token": sign(signing_key, claims, {"typ": f"brindle-{kind}+jwt"})}))

    monkeypatch.setattr(license, "current", lambda **kw: ent)
    monkeypatch.setattr(auth, "authed", lambda client, store, method, path: (200, response(path.rsplit("/", 1)[1])))
    device = managed.verify_file(managed_file(signing_key, answer()), now=NOW)
    device = replace(device, audit_sink=device_sink)
    monkeypatch.setattr(managed, "load", lambda **kw: None if seam in ("policy_source", "plan_rules") else device)
    monkeypatch.setattr(auth.Client, "call", lambda *a, **kw: (503, {}))

    def context(now=NOW):
        return OrgContext.resolve(home=tmp_path, repo_root=str(tmp_path), now=now)

    ctx = context()
    if seam == "policy_source":
        assert ctx.policy.max_parallel_workers == 1 and ctx.source() == "account"
        assert (ctx.org_id, ctx.org_name) == (ent.org_id, "Acme")
        assert ctx.verified and ctx.status.org_id == ent.org_id and ctx.blocked_reason() is None
        # The real verifier rejects a different member's otherwise valid cached copy.
        ent = SimpleNamespace(**{**vars(ent), "sub": "user_2"})
        monkeypatch.setattr(auth, "authed", lambda *a, **kw: (503, {}))
        assert context().blocked_reason()
        monkeypatch.setattr(managed, "load", lambda **kw: device)
        ent = None
        assert context().org_name == device.org_id
        assert context(NOW + 7200).blocked_reason()
        monkeypatch.setenv("BRINDLE_AIRGAP", "1")
        assert context().policy.max_parallel_workers == 2
        monkeypatch.delenv("BRINDLE_AIRGAP")
        airgap.arm()
        assert context().source() == "snapshot"
        device = replace(device, allow_airgap=True)
        exported = answer(max_parallel_workers=4)
        claims = {"iss": ISS, "aud": "brindle-pro", "org_id": device.org_id, "doc_type": "offline-policy",
                  "policy_version": 3, "document_sha256": managed.document_sha256(exported),
                  "iat": NOW - 60, "exp": NOW + 3600, "kid": TEST_KID, "token_use": "offline-policy"}
        path = airgap.policy_path(str(tmp_path))
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"body": exported, "token": sign(
            signing_key, claims, {"typ": "brindle-offline-policy+jwt"})}))
        offline = context()
        assert offline.source() == "offline" and offline.verified and offline.policy.max_parallel_workers == 4
        monkeypatch.setattr(managed, "load", lambda **kw: managed.Invalid("invalid device signature"))
        assert context().blocked_reason() == "invalid device signature"
        airgap.reset()
    elif seam == "managed_stricter":
        req = Request("claude", "bash", "Bash", str(tmp_path), str(tmp_path), str(tmp_path),
                      command="terraform apply")
        assert ctx.org_decision(req).decision == "deny"
        with ctx.activated():
            assert team_policy.access(str(tmp_path)).allowed_models == ("opus",)
            assert team_policy.org_budgets(str(tmp_path)).budget_seat_month_usd == 50
            assert team_policy.managed_models(str(tmp_path)).max_parallel_workers == 1
        # An unsigned local file never substitutes for a managed device's policy.
        path = airgap.policy_path(str(tmp_path))
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"version": 1}))
        with pytest.raises(team_policy.PolicyUnavailable):
            team_policy.load_offline(str(tmp_path), device.org_id)
        ent = SimpleNamespace(**{**vars(ent), "org_id": device.org_id, "policy_signed": False})
        remote_status["control"] = {"id": "c1", "action": "throttle", "throttle": {
            "allowed_models": [], "max_parallel_workers": 1, "seat_month_usd": 10}}
        throttled = context().policy
        assert (throttled.allowed_models, throttled.max_parallel_workers,
                throttled.budget_seat_month_usd) == ((), 1, 10)
    elif seam == "audit_sinks":
        from test_audit_chain import ev

        assert ctx.audit_sinks() == [sink, device_sink]
        requests, refreshes = [], []
        monkeypatch.setattr("time.time", lambda: NOW)
        monkeypatch.setattr(license, "current", lambda refresh=True, **kw: refreshes.append(refresh) or ent)
        monkeypatch.setattr(auth, "authed", lambda *a, **kw: requests.append("account") or (503, {}))
        monkeypatch.setattr(auth.Client, "call", lambda *a, **kw: requests.append("device") or (503, {}))
        monkeypatch.setattr(audit_stream, "_targets", [])
        with at_home(tmp_path):
            chain = audit_chain.make(str(tmp_path))
            chain.home = tmp_path
            monkeypatch.setattr(audit_stream, "_spawn", lambda fn: None)
            chain.emit(ev(repo_root=str(tmp_path)))
        assert requests == [] and not any(refreshes)
        assert all(audit_stream.queue_path(s, tmp_path).exists() for s in (sink, device_sink))
        monkeypatch.setenv("BRINDLE_AIRGAP", "1")
        monkeypatch.setattr(audit_stream._opener, "open", lambda *a, **kw: _unreadable())
        with pytest.raises(airgap.AirGapError):
            audit_stream.deliver(sink, {"event": {"kind": "test"}})
    else:
        from brindle import plan_policy

        assert ctx.needs_plan(PlanTask(files=["finance/report.py"]), config=SimpleNamespace(plan="never"))
        with ctx.activated():
            assert plan_policy.load_org_rules(str(tmp_path)).paths == ("finance/**",)


@pytest.mark.parametrize("unavailable", ["policy", "status"])
def test_unavailable_signed_governance_blocks_launch_and_preserves_running_worker(
        db, repo, worker, monkeypatch, signing_key, unavailable):
    import time

    from brindle import agents, workspaces
    from brindle.pro import auth, rollout
    from test_policy_cache import NOW, answer

    ent = SimpleNamespace(**{**vars(ENT), "org_id": "org_1", "sub": "user_1", "policy_signed": True})
    monkeypatch.setattr(time, "time", lambda: NOW)
    monkeypatch.setattr(license, "current", lambda **kw: ent)
    monkeypatch.setattr(license, "has", lambda feature: True)
    monkeypatch.setattr(auth, "authed", lambda client, store, method, path:
                        (200, answer(signing_key, "policy")) if unavailable == "status" and path.endswith("/policy")
                        else (503, {}))
    status.save(status.Saved(org_id=ent.org_id, status=status.Status(paused=True)))
    why = OrgContext.resolve().blocked_reason()
    assert why
    ws = workspaces.adopt_root(db, str(repo))
    with pytest.raises(agents.AgentError) as exc:
        agents.request_review(db, None, ws)
    assert why in str(exc.value)
    assert rollout.sweep(db) == [] and worker[0] == []
    assert db.get_agent("w1").status == "processing"


@pytest.mark.parametrize("path", ["read", "poll", "managed_poll"])
def test_signed_status_uses_verified_cache(tmp_path, monkeypatch, signing_key, path):
    from brindle.pro import auth, managed, policy_cache
    from test_policy_cache import BODIES, ME, NOW, answer

    ent = SimpleNamespace(**vars(ME), features={"team"}, policy_signed=path != "managed_poll")
    if path == "managed_poll":
        device = managed.Managed("org_device", "d1", "t", False, None, None, False, NOW + 86400)
        monkeypatch.setattr(managed, "load", lambda **kw: device)
        monkeypatch.setattr(managed, "device_policy", lambda *a, **kw: team_policy.parse_policy(
            device.org_id, {"version": 1, "policy": {}}))
    monkeypatch.setattr("time.time", lambda: NOW)
    monkeypatch.setattr(license, "current", lambda **kw: ent)
    with at_home(tmp_path):
        monkeypatch.setitem(BODIES, "status", {**BODIES["status"], "notices": [
            {"id": "n1", "text": "notice", "from_role": "admin"}]})
        policy_cache.get_verified("status", binding=ME, fetch=lambda kind: answer(signing_key, kind), now=NOW)
        status.save(status.Saved(org_id=ent.org_id, status=status.Status(paused=False), delivered=["n1"]))
        if path == "read":
            saved = status.load()
            assert saved.status.paused and saved.delivered == ["n1"]
            assert not status.load("another_org").status.paused
            monkeypatch.setattr(auth, "authed", lambda *a, **kw: (204, {}))
            assert status.mark_read(["n1"]) == ["n1"]
            assert status.load().status.notices == []
        else:
            policy_cache.get_verified("policy", binding=ME, fetch=lambda kind: answer(signing_key, kind), now=NOW)
            team_policy.save_cached(team_policy.parse_policy(ent.org_id, {"version": 4, "policy": {}}))
            monkeypatch.setitem(BODIES, "status", {**BODIES["status"], "paused": False, "policy_version": 4})
            monkeypatch.setitem(BODIES, "policy", {**BODIES["policy"], "version": 4})
            response = answer(signing_key, "status", policy_version=4, iat=NOW)
            policy_response = answer(signing_key, "policy", policy_version=4, iat=NOW)
            monkeypatch.setattr(auth, "authed", lambda client, store, method, url:
                                (200, response if url.endswith("/status") else policy_response))
            poller = status.Poller(entitlement=lambda: ent, clock=lambda: NOW)
            assert not poller.poll_once().paused
            copy = policy_cache.get_verified("status", binding=ME, fetch=lambda kind: None, now=NOW)
            assert copy.claims["policy_version"] == 4 and not copy.body["paused"]
            policy = policy_cache.get_verified("policy", binding=ME, fetch=lambda kind: None, now=NOW)
            assert policy.claims["policy_version"] == 4
            assert OrgContext.resolve().policy.version == 4
            response = {"policy_version": 5, "paused": True}
            assert poller.poll_once() is None
            assert not status.load().status.paused
