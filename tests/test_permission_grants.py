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
from brindle.db import DB, Agent, Workspace
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


@pytest.mark.parametrize("kind,target,extra,lim,offered,setup", [
    ("bash", PUSH, [], LOCAL, None, "git config --add remote.origin.pushurl https://example.com/o/other.git"),
    ("bash", PUSH, [], LOCAL, None, "git config url.ext::sh.insteadOf https://"),
    ("bash", PUSH, [], LOCAL, None, "git config push.followTags true"),
    ("bash", PUSH, [], LOCAL, None, "printf '#!/bin/sh\\n' > .git/hooks/pre-push"),
    ("bash", "git push origin abc123:refs/heads/feat", [], LOCAL, None, "true"),
    ("bash", "npm test", [], LOCAL, "allow_for", ""),
    ("bash", "npm test && npm run lint", [], LOCAL, None, ""),
    ("bash", PUSH, [], LOCAL, "override", ""),
    ("bash", PUSH, [ORG_DENY], LOCAL, None, ""),
    pytest.param("bash", "git push --force origin {sha}:refs/heads/feat", [], LOCAL, None, "",
                 marks=pytest.mark.variant("d-git-push answers first, but d-git-force still holds")),
    ("bash", PUSH, [], replace(LOCAL, protected=True), None, ""),
    ("bash", PUSH, [], replace(LOCAL, overridable=("env",)), None, ""),
    ("bash", PUSH, [], replace(LOCAL, allow=False), None, ""),
    ("read", "{repo}/.env", [], LOCAL, "override", ""),
    ("read", "~/.ssh/id_ed25519", [], LOCAL, None, ""),
])
def test_only_asks_and_adapted_built_in_denies_are_offered(repo, pushable, kind, target, extra, lim, offered, setup):
    if setup:
        sh(setup, repo)
    req = request(repo, kind, target.replace("{sha}", pushable))
    if setup:
        scope, why = permission_grants.push_scope(req, lim)
        assert scope is None and why
    rules = [*permissions.DEFAULT_RULES, *extra]
    decision = permissions.decide(req, rules=rules)
    lifted, why = permission_grants.override(req, decision, rules, [], lim)
    assert (permission_grants.offer(req, decision, lim) or lifted or {}).get("type") == offered
    # A deny an adapter knows says why it can't be lifted here; any other deny just stands.
    assert bool(why) == (offered is None and decision.decision == "deny"
                         and decision.rule.id in permission_grants.OVERRIDABLE)


def test_grants_expire_run_out_are_revoked_and_race_for_the_last_use(monkeypatch, db, repo, world, answering):
    config.set_local(world.repo_root, "permission_policy", "on")
    monkeypatch.setattr(permission_grants, "LOCAL_USES", 3)
    monkeypatch.setattr(permission_grants, "LOCAL_MINUTES", 7)
    codex_worker(db)
    for provider, agent_id, payload in (
        ("claude", "wk", {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com/docs", "prompt": "read"}}),
        ("codex", "co", {"tool_name": "Bash", "tool_input": {"command": "terraform plan"}}),
    ):
        payload = {**payload, "cwd": str(repo)}
        hook = Hook(db, monkeypatch, agent_id, payload, provider)
        assert "allow_for" in [c.choice for c in hook.shown.choices]
        assert hook.answer("allow_for", minutes=3, scope="remote")[0] == 409
        code, reply = hook.answer("allow_for", minutes=3)
        assert code == 200 and reply["grant"] and hook.verdict() == "allow"
        again = agents.permission_request_decision(db, db.get_agent(agent_id), payload, provider)
        assert _verdict(again) == "allow"
        [live] = _send("GET", "/v1/grants")[1]
        assert (live["uses_left"], live["expires_at"] - live["created"]) == (1, 3 * 60)
        assert _send("POST", f"/v1/grants/{reply['grant']}/revoke", peer_pid=310)[0] == 403
        assert _send("POST", f"/v1/grants/{reply['grant']}/revoke")[0] == 200
        assert _send("GET", "/v1/grants")[1] == []
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

    def answer(self, choice, **extra):
        return _send("POST", f"/v1/inbox/{self.agent_id}/permission",
                     {"request": self.shown.request_id, "digest": self.shown.digest, "choice": choice, **extra})

    def verdict(self):
        self.thread.join(5)
        return _verdict(self.out[0])


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
    changed = permissions.add_rule("bash", "git push", "deny", "prefix")
    assert answer(asked, "allow_always")[0] == 409
    assert permissions.load_store().remembered == []
    permissions.forget(changed.id)
    assert answer(asked, "allow_always")[0] == 200
    [saved] = [g for g in _send("GET", "/v1/grants")[1] if g.get("persistent")]
    with permissions.editing() as store:
        store.remembered[0].pop("scope_level", None)  # replay the durable baseline's branch-bound records
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
    assert {c.choice for c in shown.choices} == {"allow_once", "allow_session", "allow_for", "allow_always", "deny"}
    assert answer(asked, "allow_once", "0" * 64)[0] == 409
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
    assert "allow_always" not in [c.choice for c in hook.shown.choices]
    org(scopes=[])
    assert "allow_for" not in [c.choice for c in inbox.pending_permissions(db, wk)[0].dto.choices]
    assert hook.answer("allow_for")[0] == 409
    org(allow=False)                                     # the org withdraws grants while it is held
    code, reply = hook.answer("allow_for")
    assert code == 409
    assert hook.answer("deny")[0] == 200 and hook.verdict() == "deny"
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
    assert "allow_for" not in [c.choice for c in inbox._asked_dto(asked).choices]
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
    assert answer(asked, "allow_always")[0] == 409
    org(allow_persistent=True, lifetimes=["forever"])
    assert answer(asked, "allow_always")[0] == 200 and push() == "allow"
    org(allow_persistent=False)
    assert push() == "deny"
    [asked] = grants.GRANTS.asked_of("wk")
    assert answer(asked, "allow_always")[0] == 409
    org(allow_persistent=True, kinds=["read"])
    assert push() == "deny" and answer(asked, "allow_once")[0] == 409
    assert all(c.choice == "deny" for c in inbox._asked_dto(asked).choices)


@pytest.mark.parametrize("action", ["git-push", pytest.param("env", marks=pytest.mark.variant(
    "a moved .env file must invalidate a pending approval just as a changed push destination does"))])
def test_timed_adapter_approval_is_durable_bounded_and_revalidated(db, repo, pushable, world, answering,
                                                                  monkeypatch, action):
    from brindle.pro import license

    config.set_local(world.repo_root, "permission_policy", "on")
    now = [1000.0]
    monkeypatch.setattr(permission_grants.time, "time", lambda: now[0])
    monkeypatch.setenv(airgap.ENV, "1")
    ent = SimpleNamespace(org_id="org_g1", features=["team"], role="member", policy_role=None,
                          policy_version=0, sub="member-1")
    monkeypatch.setattr(license, "current", lambda **kw: ent)
    monkeypatch.setattr(permission_grants, "_plugin",
                        lambda root: team_policy.ProPolicy(root, entitlement=lambda: ent))
    path = airgap.policy_path(world.repo_root)
    path.parent.mkdir(exist_ok=True)

    def org(**over):
        path.write_text(json.dumps({"version": 1, "policy": {"permission_grants": {
            **GOOD, "kinds": ["bash", "read"], "overridable": ["git-push", "env"],
            "max_minutes": 120, "max_uses": 3, "allow_persistent": True, **over}}}))

    org()
    payload = ({"tool_name": "Bash", "tool_input": {"command": PUSH.format(sha=pushable)}} if action == "git-push"
               else {"tool_name": "Read", "tool_input": {"file_path": str(repo / ".env")}})
    def call(d=None):
        d = d or db
        return _verdict(agents.permission_request_decision(d, d.get_agent("wk"), {**payload, "cwd": str(repo)}))
    assert call() == "deny"
    [asked] = grants.GRANTS.asked_of("wk")
    panes = inbox.view.tmux.PaneSnapshot()
    panes.update({"%1": True, "%2": True})
    monkeypatch.setattr(inbox.view.tmux, "list_panes", lambda: panes)
    db.update_autopilot("sup", proposal=json.dumps({"checks": []}))
    def shown():
        code, items = _send("GET", "/v1/inbox")
        assert code == 200
        return next(i["permission"] for i in items if (i.get("permission") or {}).get("request_id") == asked.id)
    assert {c["choice"] for c in shown()["choices"]} >= {"allow_session", "allow_for"}
    assert _send("POST", "/v1/inbox/wk/permission", {"request": asked.id, "digest": asked.digest,
                 "choice": "allow_for", "minutes": 60}, peer_pid=310)[0] == 403
    def answer(minutes, **extra):
        return _send("POST", "/v1/inbox/wk/permission", {"request": asked.id, "digest": asked.digest,
                     "choice": "allow_for", "minutes": minutes, **extra})[0]
    org(allow_persistent=False)
    assert {c["choice"] for c in shown()["choices"]} == {"allow_once", "deny"}
    assert answer(60) == 409
    assert _send("POST", "/v1/inbox/wk/permission", {"request": asked.id, "digest": asked.digest,
                 "choice": "allow_session"})[0] == 409
    assert permissions.load_store().remembered == []
    org()
    assert _send("POST", "/v1/inbox/wk/permission", {"request": asked.id, "digest": asked.digest,
                 "choice": "allow_once"})[0] == 200
    org(scopes=[])
    assert call() == "deny"
    org(lifetimes=[])
    assert call() == "deny"
    org()
    assert call() == "allow" and call() == "deny"
    [asked] = grants.GRANTS.asked_of("wk")
    org(scopes=[])
    assert [c["choice"] for c in shown()["choices"]] == ["deny"]
    assert _send("POST", "/v1/inbox/wk/permission", {"request": asked.id, "digest": asked.digest,
                 "choice": "allow_once"})[0] == 409
    assert answer(60) == 409
    org(lifetimes=[])
    assert [c["choice"] for c in shown()["choices"]] == ["deny"]
    assert answer(60) == 409
    org()
    assert answer(121) == 409 and answer(True) == 400
    assert answer(60, scope="all") == 409
    if action == "env":
        assert answer(60, scope="remote") == 409
    if action == "git-push":
        sh("git remote set-url --push origin https://example.com/o/changed.git", repo)
    else:
        (repo / ".env").rename(repo / ".env.moved")
    assert answer(60) == 409 and permissions.load_store().remembered == []
    if action == "git-push":
        sh("git remote set-url --push origin https://example.com/o/r.git", repo)
    else:
        (repo / ".env.moved").rename(repo / ".env")
    assert answer(60) == 200 and answer(60) == 409
    [saved] = permissions.load_store().remembered
    assert saved["expires_at"] == now[0] + 3600
    [listed] = _send("GET", "/v1/grants")[1]
    assert listed["expires_at"] == saved["expires_at"] and listed["uses_left"] == 3
    assert listed["persistent"] is False
    fresh = grants.Grants()
    monkeypatch.setattr(grants, "GRANTS", fresh)
    monkeypatch.setattr(inbox, "GRANTS", fresh)
    org(allow_persistent=False)
    assert call() == "deny"
    org(scopes=[])
    assert call() == "deny"
    org(lifetimes=[])
    assert call() == "deny"
    org()
    assert call() == "allow"
    org(max_uses=1)
    assert call() == "deny"
    org()
    now[0] += 3601
    assert call() == "deny"
    assert _send("GET", "/v1/grants")[1] == []
    [asked] = fresh.asked_of("wk")
    assert answer(60) == 200 and call() == "allow"
    org(max_minutes=1)
    now[0] += 61
    assert call() == "deny"
    [asked] = fresh.asked_of("wk")
    org(max_uses=1)
    assert answer(2) == 200
    start, verdicts = threading.Barrier(2), []

    def race():
        start.wait()
        d = DB()
        try:
            verdicts.append(call(d))
        finally:
            d.conn.close()

    racers = [threading.Thread(target=race) for _ in range(2)]
    for t in racers:
        t.start()
    for t in racers:
        t.join()
    assert sorted(verdicts) == ["allow", "deny"]


@pytest.mark.parametrize("scope", ["remote", "repo", "all"])
def test_remembered_push_scope_is_durable_and_obeys_current_enterprise_bounds(
        db, repo, pushable, world, answering, monkeypatch, scope, tmp_path):
    from brindle.pro import license

    config.set_local(world.repo_root, "permission_policy", "on")
    monkeypatch.setenv(airgap.ENV, "1")
    ent = SimpleNamespace(org_id="org_g1", features=["team"], role="member", policy_role=None,
                          policy_version=0, sub="member-1")
    monkeypatch.setattr(license, "current", lambda **kw: ent)
    monkeypatch.setattr(permission_grants, "_plugin",
                        lambda root: team_policy.ProPolicy(root, entitlement=lambda: ent))
    path = airgap.policy_path(world.repo_root)
    path.parent.mkdir(exist_ok=True)
    url, other_url = "https://example.com/o/r.git", "https://example.com/o/mirror.git"

    def org(**over):
        path.write_text(json.dumps({"version": 1, "policy": {"permission_grants": {
            **GOOD, "allow_persistent": True, "lifetimes": ["forever"], **over}}}))

    org()
    codex_worker(db)
    push, _ = pusher(db, "wk", "claude", pushable)
    assert push() == "deny"
    [asked] = grants.GRANTS.asked_of("wk")

    def answer(peer_pid=None):
        return _send("POST", "/v1/inbox/wk/permission", {"request": asked.id, "digest": asked.digest,
                     "choice": "allow_always", "scope": scope}, peer_pid=peer_pid)[0]

    assert answer() == 409 and permissions.load_store().remembered == []
    org(scopes=[scope])
    assert scope in inbox._asked_dto(asked).approval_scopes
    assert answer(310) == 403
    assert answer() == 200 and answer() == 409
    [saved] = permissions.load_store().remembered
    assert saved["scope_level"] == scope
    fresh = grants.Grants()
    monkeypatch.setattr(grants, "GRANTS", fresh)
    monkeypatch.setattr(inbox, "GRANTS", fresh)
    push, _ = pusher(db, "co", "codex", "1" * 40)
    other_branch = "git push origin " + "1" * 40 + ":refs/heads/another"
    assert push(other_branch) == "allow"
    org(scopes=["branch"])
    assert push(other_branch) == "deny"
    org(scopes=[scope], allow_persistent=False)
    assert push(other_branch) == "deny"
    org(scopes=[scope], lifetimes=[])
    assert push(other_branch) == "deny"
    org(scopes=[scope])
    sh(f"git remote add alias {url}", repo)
    sh(f"git remote add mirror {other_url}", repo)
    assert push("git push alias " + "1" * 40 + ":refs/heads/another") == (
        "deny" if scope == "remote" else "allow")
    mirror = "git push mirror " + "1" * 40 + ":refs/heads/another"
    assert push(mirror) == ("deny" if scope == "remote" else "allow")
    other_repo = tmp_path / "another-repo"
    other_repo.mkdir()
    sh("git init", other_repo)
    sh(f"git remote add origin {url}", other_repo)
    config.set_local(str(other_repo), "permission_policy", "on")
    other_policy = airgap.policy_path(str(other_repo))
    other_policy.parent.mkdir(exist_ok=True)
    other_policy.write_text(path.read_text())
    db.add_workspace(Workspace("other", str(other_repo), "other", "worktree", "other", "main",
                               str(other_repo), None, "brindle-other", 1.0))
    db.update_agent("co", workspace_id="other")
    other_push, _ = pusher(db, "co", "codex", "1" * 40)
    assert other_push() == ("allow" if scope == "all" else "deny")
    other_policy.write_text(json.dumps({"version": 1, "policy": {"permission_grants": {
        **GOOD, "allow_persistent": True, "lifetimes": [], "scopes": []}}}))
    assert other_push() == "deny"
    other_policy.write_text(path.read_text())
    ent.sub = "another-member"
    assert other_push() == "deny"
    ent.sub = "member-1"
    assert other_push() == ("allow" if scope == "all" else "deny")
    db.update_agent("co", workspace_id="w-1")
    assert push(other_branch.replace("git push", "git push --force")) == "deny"
    deny = permissions.add_rule("bash", "git push", "deny", "prefix")
    assert push(other_branch) == "deny"
    permissions.forget(deny.id)
    assert push(other_branch) == "allow"
    assert _send("POST", f"/v1/grants/{saved['id']}/revoke", peer_pid=310)[0] == 403
    assert _send("POST", f"/v1/grants/{saved['id']}/revoke")[0] == 200
    assert push(other_branch) == "deny"


def test_session_push_approval_tracks_the_supervisor_session_not_worker_or_clock(
        db, repo, pushable, world, answering, monkeypatch):
    from brindle.pro import license

    config.set_local(world.repo_root, "permission_policy", "on")
    monkeypatch.setenv(airgap.ENV, "1")
    ent = SimpleNamespace(org_id="org_g1", features=["team"], role="member", policy_role=None,
                          policy_version=0, sub="member-1")
    monkeypatch.setattr(license, "current", lambda **kw: ent)
    monkeypatch.setattr(permission_grants, "_plugin",
                        lambda root: team_policy.ProPolicy(root, entitlement=lambda: ent))
    path = airgap.policy_path(world.repo_root)
    path.parent.mkdir(exist_ok=True)

    def org(lifetimes, allow_persistent=True):
        path.write_text(json.dumps({"version": 1, "policy": {"permission_grants": {
            **GOOD, "max_uses": 10, "max_minutes": 1, "allow_persistent": allow_persistent,
            "scopes": ["remote"], "lifetimes": lifetimes}}}))

    org(["session"])
    push, _ = pusher(db, "wk", "claude", pushable)
    assert push() == "deny"
    [asked] = grants.GRANTS.asked_of("wk")
    choices = {c.choice for c in inbox._asked_dto(asked).choices}
    assert "allow_session" in choices and "allow_for" not in choices and "allow_always" not in choices
    body = {"request": asked.id, "digest": asked.digest, "choice": "allow_session", "scope": "remote"}
    assert _send("POST", "/v1/inbox/wk/permission", body, peer_pid=310)[0] == 403
    assert _send("POST", "/v1/inbox/wk/permission", {**body, "choice": "allow_always"})[0] == 409
    assert _send("POST", "/v1/inbox/wk/permission", body)[0] == 200
    [listed] = _send("GET", "/v1/grants")[1]
    assert listed["expires_at"] is None and not listed["persistent"] and listed["agent_id"] == "sup"
    fresh = grants.Grants()
    monkeypatch.setattr(grants, "GRANTS", fresh)
    monkeypatch.setattr(inbox, "GRANTS", fresh)
    org(["session"], allow_persistent=False)
    assert push() == "deny"
    org(["session"])
    codex_worker(db)
    db.update_agent("wk", status="done")
    push, _ = pusher(db, "co", "codex", "1" * 40)
    assert push() == "allow"
    now = [permission_grants.time.time() + 3600]
    monkeypatch.setattr(permission_grants.time, "time", lambda: now[0])
    assert push() == "allow"
    db.add_agent(Agent("other", "w-1", "supervisor", "claude", None, "interactive", "idle", "%9", None, now[0]))
    db.update_agent("co", parent_id="other")
    assert push() == "deny"
    db.update_agent("co", parent_id="sup")
    org(["time"])
    assert push() == "deny"
    org(["session"])
    assert push() == "allow"
    db.update_agent("sup", dismissed_at=now[0])
    assert push() == "deny" and _send("GET", "/v1/grants")[1] == []
    with permissions.editing() as store:
        store.remembered[0].pop("session")
    assert push() == "deny"  # a missing saved identity must not match a missing live session
