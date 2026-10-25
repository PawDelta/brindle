"""Permission grants (brindle.permission_grants): what may be offered, the
push adapter's checks, the daemon's grants, the org's block, and the whole
way through the real hook, daemon and inbox."""

import json
import os
import subprocess
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import sh
from test_daemon_actions import _HookConnection, _send, _verdict, answering, world  # noqa: F401 - fixtures

from brindle import agents, airgap, bus, config, permission_grants, permissions
from brindle.daemon import grants, holds
from brindle.db import DB, Agent
from brindle.permissions import Request, Rule
from brindle.pro import team_policy
from brindle.service import inbox

LOCAL = permission_grants.Limits(True, permissions.KINDS, ("git-push", "env"), 60, 20)
ORG_DENY = Rule("bash", "git push", "prefix", "deny", "org")


@pytest.fixture(autouse=True)
def fresh_grants(monkeypatch):
    store = grants.Grants()
    monkeypatch.setattr(grants, "GRANTS", store)
    monkeypatch.setattr(inbox, "GRANTS", store)


@pytest.fixture
def pushable(repo, monkeypatch):
    """``repo`` pushing to one https URL, read with no global git config; its HEAD."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    sh("git remote set-url --push origin https://example.com/o/r.git", repo)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


def request(repo, kind, target):
    target = os.path.expanduser(target.format(repo=repo))
    return Request("claude", kind, "Bash" if kind == "bash" else "Read", cwd=str(repo), worktree=str(repo),
                   repo_root=str(repo), **({"command": target} if kind == "bash" else {"path": target}))


PUSH = "git push origin {sha}:refs/heads/feat"


@pytest.mark.parametrize("kind,target,extra,lim,offered", [
    ("bash", "npm test", [], LOCAL, "allow_for"),
    ("bash", "npm test && npm run lint", [], LOCAL, None),       # no exact scope for a compound command
    ("bash", PUSH, [], LOCAL, "override"),
    ("bash", PUSH, [ORG_DENY], LOCAL, None),                      # another deny matches: hard
    pytest.param("bash", "git push --force origin {sha}:refs/heads/feat", [], LOCAL, None,
                 marks=pytest.mark.variant("d-git-push answers first, but d-git-force still holds")),
    ("bash", PUSH, [], replace(LOCAL, protected=True), None),
    ("bash", PUSH, [], replace(LOCAL, overridable=("env",)), None),
    ("bash", PUSH, [], replace(LOCAL, allow=False), None),
    ("read", "{repo}/.env", [], LOCAL, "override"),
    ("read", "~/.ssh/id_ed25519", [], LOCAL, None),               # a credential deny is never lifted
])
def test_only_asks_and_adapted_built_in_denies_are_offered(repo, pushable, kind, target, extra, lim, offered):
    req = request(repo, kind, target.replace("{sha}", pushable))
    rules = [*permissions.DEFAULT_RULES, *extra]
    decision = permissions.decide(req, rules=rules)
    lifted, why = permission_grants.override(req, decision, rules, [], lim)
    assert (permission_grants.offer(req, decision, lim) or lifted or {}).get("type") == offered
    # A deny an adapter knows says why it can't be lifted here; any other deny just stands.
    assert bool(why) == (offered is None and decision.decision == "deny"
                         and decision.rule.id in permission_grants.OVERRIDABLE)


@pytest.mark.parametrize("setup,command", [
    ("git config --add remote.origin.pushurl https://example.com/o/other.git", PUSH),
    ("git config url.ext::sh.insteadOf https://", PUSH),     # rewrites the push URL as git push would
    ("git config push.followTags true", PUSH),
    ("printf '#!/bin/sh\\n' > .git/hooks/pre-push", PUSH),
    ("true", "git push origin abc123:refs/heads/feat"),
])
def test_push_scope_refuses_what_could_push_elsewhere_or_more(repo, pushable, setup, command):
    sh(setup, repo)
    scope, why = permission_grants.push_scope(request(repo, "bash", command.replace("{sha}", pushable)), LOCAL)
    assert scope is None and why


def test_grants_expire_run_out_are_revoked_and_race_for_the_last_use(monkeypatch):
    now = [1000.0]
    store = grants.Grants(clock=lambda: now[0])
    g = store.add("a1", "/r", "bash", "npm test", 1, 2)
    assert store.add("a1", "/r", "bash", "npm test", 60, 99) == g          # never extended
    assert store.use("a2", "/r", "bash", "npm test") is None               # another agent's
    assert store.use("a1", "/r", "bash", "npm test").uses_left == 1
    now[0] += 61
    assert store.use("a1", "/r", "bash", "npm test") is None               # expired
    revoked = store.add("a1", "/r", "fetch", "https://x.example/", 5, 3)
    assert store.revoke(revoked.id) == revoked and store.live() == []
    store.add("a1", "/r", "bash", "make", 5, 1)
    start, won = threading.Barrier(8), []

    def race():
        start.wait()
        won.append(store.use("a1", "/r", "bash", "make"))

    threads = [threading.Thread(target=race) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(w is not None for w in won) == 1
    monkeypatch.setattr(grants, "MAX_GRANTS", 1)
    occupying = store.add("a1", "/r", "fetch", "https://x.example/", 5, 1)
    asked = store.ask("a1", "/r", "bash", "git push", "scope", "git-push", "push the selected commit")
    assert store.answer("a1", asked.id, asked.digest, True) is None
    assert store.asked("a1", asked.id) is asked
    store.revoke(occupying.id)
    approved = store.answer("a1", asked.id, asked.digest, True)
    assert approved.uses_left == 1
    assert store.asked("a1", asked.id) is None
    store.revoke(approved.id)
    store.add("a1", "/r", "bash", "make", 5, 3, uses_spent=1)
    assert store.use("a1", "/r", "bash", "make", max_uses=1) is None
    assert store.live() == []


GOOD = {"allow": True, "kinds": ["bash"], "overridable": ["git-push"], "max_minutes": 5, "max_uses": 2,
        "deny": ["bash prefix terraform apply"]}


@pytest.mark.parametrize("block,ok", [
    (GOOD, True),
    ({**GOOD, "expires": 1}, False),
    pytest.param({k: v for k, v in GOOD.items() if k != "max_uses"}, False,
                 marks=pytest.mark.variant("a block an older server sends, missing a cap: never unbounded")),
    ({**GOOD, "kinds": ["shell"]}, False),
    ({**GOOD, "max_minutes": 0}, False),
    ({**GOOD, "deny": ["bash regex x"]}, False),
    ({**GOOD, "allow": "yes"}, False),
    ({**GOOD, "allow_persistent": "yes"}, False),
])
def test_permission_grants_block_is_strict_and_survives_cache_and_roles(block, ok):
    body = {"version": 2, "policy": {"permission_grants": block, "roles": {"member": {"max_parallel_workers": 2}}}}
    if not ok:
        with pytest.raises(team_policy.PolicyUnavailable):
            team_policy.parse_policy("org_g1", body)
        return
    p = team_policy.parse_policy("org_g1", body)
    assert team_policy.parse_policy("org_g1", p.to_json()).permission_grants == p.permission_grants
    assert team_policy.with_role_overrides(p, "member", None).enforced.permission_grants == p.permission_grants


class Hook:
    """One hook call on its own thread, held for the app's answer."""

    def __init__(self, db, monkeypatch, agent_id, payload, provider="claude"):
        self.db, self.agent_id, self.out, self.asked = db, agent_id, [], threading.Event()
        publish = bus.publish_agent
        monkeypatch.setattr(bus, "publish_agent", lambda d, kind, *a, **k: (
            kind == "permission.pending" and self.asked.set(), publish(d, kind, *a, **k))[1])
        agent = db.get_agent(agent_id)
        self.thread = threading.Thread(target=lambda: self.out.append(
            agents.permission_request_decision(DB(), agent, payload, provider)))
        self.thread.start()
        assert self.asked.wait(5)
        [self.shown] = [p.dto for p in inbox.pending_permissions(db, agent)]

    def answer(self, choice):
        return _send("POST", f"/v1/inbox/{self.agent_id}/permission",
                     {"request": self.shown.request_id, "digest": self.shown.digest, "choice": choice})

    def verdict(self):
        self.thread.join(5)
        return _verdict(self.out[0])


@pytest.mark.parametrize("provider,agent_id,payload", [
    ("claude", "wk", {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com/docs", "prompt": "read"}}),
    ("codex", "co", {"tool_name": "Bash", "tool_input": {"command": "terraform plan"}}),
])
def test_allow_for_lets_one_agent_repeat_one_request_until_revoked(db, repo, world, answering, monkeypatch,
                                                                    provider, agent_id, payload):
    config.set_local(world.repo_root, "permission_policy", "on")
    monkeypatch.setattr(permission_grants, "LOCAL_USES", 3)
    monkeypatch.setattr(permission_grants, "LOCAL_MINUTES", 7)
    codex_worker(db)
    payload = {**payload, "cwd": str(repo)}
    hook = Hook(db, monkeypatch, agent_id, payload, provider)
    assert "allow_for" in [c.choice for c in hook.shown.choices]
    code, reply = hook.answer("allow_for")
    assert code == 200 and reply["grant"] and hook.verdict() == "allow"
    again = agents.permission_request_decision(db, db.get_agent(agent_id), payload, provider)
    assert _verdict(again) == "allow"                    # the grant: no hold this time
    [live] = _send("GET", "/v1/grants")[1]
    assert (live["uses_left"], live["expires_at"] - live["created"]) == (1, 7 * 60)    # the policy's caps
    assert _send("POST", f"/v1/grants/{reply['grant']}/revoke", peer_pid=310)[0] == 403   # never an agent
    assert _send("POST", f"/v1/grants/{reply['grant']}/revoke")[0] == 200
    assert _send("GET", "/v1/grants")[1] == []


class _Unreachable(_HookConnection):
    def request(self, method, url, body, headers):
        raise OSError("no daemon")


def codex_worker(db):
    db.add_agent(Agent("co", "w-1", "developer", "codex", "sup", "assign", "processing", "%3", None, 1.0))


def pusher(db, agent_id, provider, sha):
    agent = db.get_agent(agent_id)

    def push(command=PUSH.format(sha=sha), cwd=None, d=None):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or agent_cwd(d or db))}
        return _verdict(agents.permission_request_decision(d or db, agent, payload, provider))

    def agent_cwd(d):
        return d.get_workspace(agent.workspace_id).path

    def answer(asked, choice, digest=None):
        return _send("POST", f"/v1/inbox/{agent_id}/permission",
                     {"request": asked.id, "digest": digest or asked.digest, "choice": choice})

    return push, answer


def _remembered_override_case(db, repo, pushable, world, monkeypatch, push, answer, provider, agent_id):
    assert push() == "deny"
    [asked] = grants.GRANTS.asked_of(agent_id)
    assert _send("POST", f"/v1/inbox/{agent_id}/permission",
                 {"request": asked.id, "digest": asked.digest, "choice": "allow_always"}, peer_pid=310)[0] == 403
    assert answer(asked, "allow_always")[0] == 200
    [saved] = [g for g in _send("GET", "/v1/grants")[1] if g.get("persistent")]
    fresh = grants.Grants()
    monkeypatch.setattr(grants, "GRANTS", fresh)
    monkeypatch.setattr(inbox, "GRANTS", fresh)
    other, _ = pusher(db, "co" if provider == "claude" else "wk",
                      "codex" if provider == "claude" else "claude", "1" * 40)
    assert other() == "allow" and push() == "allow"
    assert other(f"git push origin {pushable}:refs/heads/elsewhere") == "deny"
    current = permission_grants.limits
    with monkeypatch.context() as policy:
        policy.setattr(permission_grants, "limits", lambda root: replace(current(root), allow_persistent=False))
        assert push() == "deny"
    permissions.add_rule("bash", "git push", "deny", "prefix")
    assert push() == "deny"
    deny = next(r for r in permissions.load_store().rules if r.source == "user" and r.decision == "deny")
    permissions.forget(deny.id)
    assert push() == "allow"
    assert _send("POST", f"/v1/grants/{saved['id']}/revoke", peer_pid=310)[0] == 403
    assert _send("POST", f"/v1/grants/{saved['id']}/revoke")[0] == 200
    assert push() == "deny"


@pytest.mark.parametrize("provider,agent_id,persistent", [
    ("claude", "wk", False), ("codex", "co", False),
    ("claude", "wk", True), ("codex", "co", True),
])
def test_a_push_is_denied_at_once_then_allowed_once_for_exactly_its_scope(db, repo, pushable, world, answering,
                                                                            monkeypatch, tmp_path, provider, agent_id, persistent):
    config.set_local(world.repo_root, "permission_policy", "on")
    codex_worker(db)
    push, answer = pusher(db, agent_id, provider, pushable)
    if persistent:
        _remembered_override_case(db, repo, pushable, world, monkeypatch, push, answer, provider, agent_id)
        return
    other = tmp_path / "other"      # another clone, pushing to the same URL
    sh(f"git clone -q {repo} {other} && git -C {other} remote set-url --push origin https://example.com/o/r.git",
       tmp_path)
    sh("git remote add mirror https://example.com/o/mirror.git", repo)
    monkeypatch.setattr(holds, "_UnixConnection", _Unreachable)
    assert push() == "deny" and grants.GRANTS.asked_of(agent_id) == []     # no daemon: nothing asked
    monkeypatch.setattr(holds, "_UnixConnection", _HookConnection)
    forged = {"agent_id": agent_id, "describe": "a trusted push", "scope": json.dumps({"url": "https://evil.example/r"}),
              "request": {"provider": provider, "kind": "bash", "tool": "Bash", "cwd": str(repo),
                          "command": PUSH.format(sha=pushable)}}
    assert _send("POST", "/v1/holds/grants/ask", forged, peer_pid=310, token=None)[0] == 200
    assert push() == "deny"                                                 # never held: answered at once
    [asked] = grants.GRANTS.asked_of(agent_id)                              # the same one, asked once
    assert "evil" not in asked.scope and "trusted" not in asked.describe   # worked out by the daemon
    assert json.loads(asked.scope)["git_dir"] == os.path.realpath(repo / ".git")
    shown = next(i.permission for i in inbox.items(db, None) if i.permission and i.permission.request_id == asked.id)
    assert [c.choice for c in shown.choices] == ["allow_once", "allow_always", "deny"] and shown.grant == asked.describe
    assert answer(asked, "allow_for")[0] == 409 and answer(asked, "allow_once", "0" * 64)[0] == 409
    with monkeypatch.context() as capacity:
        capacity.setattr(grants, "MAX_GRANTS", 0)
        assert answer(asked, "allow_once")[0] == 409
        assert grants.GRANTS.asked_of(agent_id) == [asked]
    assert answer(asked, "allow_once")[0] == 200 and answer(asked, "allow_once")[0] == 409
    for command, cwd in [(PUSH.format(sha="0" * 40), None), (f"git push origin {pushable}:refs/heads/main", None),
                         (f"git push mirror {pushable}:refs/heads/feat", None), (PUSH.format(sha=pushable), other)]:
        assert push(command, cwd) == "deny"             # another commit, branch, remote or repo: not this grant
    assert grants.GRANTS.live()[0].uses_left == 1
    start, verdicts = threading.Barrier(2), []

    def race():
        start.wait()
        verdicts.append(push(d=DB()))

    racers = [threading.Thread(target=race) for _ in range(2)]
    for t in racers:
        t.start()
    for t in racers:
        t.join(10)
    assert sorted(verdicts) == ["allow", "deny"] and push() == "deny"      # one use between the two, then none


def test_org_policy_denies_and_bounds_grants_on_the_real_path(db, repo, pushable, world, answering, monkeypatch):
    config.set_local(world.repo_root, "permission_policy", "on")
    monkeypatch.setenv(airgap.ENV, "1")
    ent = SimpleNamespace(org_id="org_g1", features=["team"], role="member", policy_role=None, policy_version=0)
    monkeypatch.setattr(permission_grants, "_plugin",
                        lambda root: team_policy.ProPolicy(root, entitlement=lambda: ent))
    path = airgap.policy_path(world.repo_root)
    path.parent.mkdir(exist_ok=True)

    def org(**over):
        path.write_text(json.dumps({"version": 1, "policy": {"permission_grants": {
            **GOOD, "deny": ["bash exact terraform plan"], **over}}}))

    def ask(command):
        return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}

    org()
    wk = db.get_agent("wk")
    assert _verdict(agents.permission_request_decision(db, wk, ask("terraform plan"))) == "deny"
    hook = Hook(db, monkeypatch, "wk", ask("terraform validate"))
    assert "allow_for" in [c.choice for c in hook.shown.choices]
    org(allow=False)                                     # the org withdraws grants while it is held
    code, reply = hook.answer("allow_for")
    assert code == 200 and reply["grant"] is None and hook.verdict() == "deny"
    g = grants.GRANTS.add("wk", os.path.realpath(world.repo_root), "bash", "terraform validate", 5, 2)
    hook = Hook(db, monkeypatch, "wk", ask("terraform validate"))   # the grant is no use now: held again
    assert hook.answer("deny")[0] == 200 and hook.verdict() == "deny"
    assert grants.GRANTS.live()[0].uses_left == g.uses_left == 2   # and never spent
    grants.GRANTS.revoke(g.id)
    org(max_uses=1)
    grants.GRANTS.add("wk", os.path.realpath(world.repo_root), "bash", "terraform validate", 5, 2)
    assert _verdict(agents.permission_request_decision(db, wk, ask("terraform validate"))) == "allow"
    assert grants.GRANTS.live() == []
    org()                                                # a one-time push lasts only as long as the org allows
    push, answer = pusher(db, "wk", "claude", pushable)
    assert push() == "deny"
    [asked] = grants.GRANTS.asked_of("wk")
    assert answer(asked, "allow_once")[0] == 200
    [g] = grants.GRANTS.live()
    assert g.expires_at - g.created == GOOD["max_minutes"] * 60
    org(max_minutes=1)                                   # and not past a limit the org has shortened since
    monkeypatch.setattr(grants.GRANTS, "clock", lambda: g.created + 90)
    assert push() == "deny" and grants.GRANTS.live() == []
    [asked] = grants.GRANTS.asked_of("wk")               # asked again: a new answer is a new grant
    assert answer(asked, "allow_once")[0] == 200
    assert grants.GRANTS.live()[0].id != g.id and push() == "allow" and push() == "deny"
    from brindle.pro import license
    ent.sub = "member-1"
    monkeypatch.setattr(license, "current", lambda **kw: ent)
    org(allow_persistent=True)
    [asked] = grants.GRANTS.asked_of("wk")
    assert answer(asked, "allow_always")[0] == 200 and push() == "allow"
    org(allow_persistent=False)
    assert push() == "deny"
    [asked] = grants.GRANTS.asked_of("wk")
    assert answer(asked, "allow_always")[0] == 409
    org(allow_persistent=True, kinds=["read"])
    assert push() == "deny" and answer(asked, "allow_once")[0] == 409
