"""A worker's permission ask goes to its supervisor before the person (brindle.permission_routing)."""

import asyncio
import json
import time

import pytest

from brindle import agents, permission_grants, permission_routing, permissions
from brindle.config import set_local
from brindle.daemon import holds, peer
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.permissions import Rule
from brindle.service import inbox

TOKEN = "t" * 43
PIDS = {"w1": 101, "boss": 102, "other": 103}     # each agent's process, as the daemon's peer check finds it


class Clock:
    """The time module, with time() and monotonic() at ``now``."""
    now = 1000.0

    def time(self):
        return self.now
    monotonic = time

    def __getattr__(self, name):
        return getattr(time, name)


class Team:
    """Worker w1 under supervisor boss, and other's worker w2. Calls to the
    daemon's hold routes reach the real app, from the process of ``caller``."""

    def __init__(self, db, ws):
        self.db, self.ws, self.app = db, ws, build_app(TOKEN)
        self.caller, self.sent, self.clock = "w1", [], Clock()

    def post(self, path, body, timeout):
        raw, out = json.dumps(body).encode(), []

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        async def send(message):
            out.append(message)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
                 "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
                 "server": ("127.0.0.1", 8123), "client": ("127.0.0.1", 50000),
                 "headers": [(b"content-type", b"application/json")], peer.PEER_PID: PIDS[self.caller]}
        asyncio.run(self.app(scope, receive, send))
        return out[0]["status"], json.loads(b"".join(m.get("body", b"") for m in out[1:]))

    def says(self, who, request_id, choice, reason=""):
        """``who`` answers w1's request, as the answer_permission tool does: (status, body)."""
        self.caller = who
        try:
            return self.post(f"{holds.PREFIX}/{request_id}/supervise",
                             {"agent_id": "w1", "answer": choice, "reason": reason}, None)
        finally:
            self.caller = "w1"


@pytest.fixture
def team(db, tmp_path, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())
    ws = Workspace("ws1", str(tmp_path), "feature", "worktree", "feature", "main", str(tmp_path), None,
                   "brindle-ws1", time.time())
    db.add_workspace(ws)
    set_local(ws.repo_root, "permission_policy", "on")
    with permissions.editing() as store:
        store.rules = [Rule("bash", "make", "prefix", "ask"),
                       Rule("bash", "deploy", "prefix", "ask", person_only=True)]
    for id_, parent in (("boss", None), ("other", None), ("w1", "boss"), ("w2", "other")):
        db.add_agent(Agent(id_, ws.id, "developer", "claude", parent, "assign" if parent else "interactive",
                           "processing", f"%{id_}", None, time.time()))
    t = Team(db, ws)
    monkeypatch.setattr(agents, "time", t.clock)
    monkeypatch.setattr(holds.HOLDS, "clock", t.clock.time)
    monkeypatch.setattr(holds.HOLDS, "_holds", {})
    monkeypatch.setattr(holds.HOLDS, "_outcomes", {})
    monkeypatch.setattr(holds, "post", t.post)
    monkeypatch.setattr(peer, "caller_agents", lambda pid: {a for a, p in PIDS.items() if p == pid})
    monkeypatch.setattr(agents, "_pane_state", lambda agent: None)
    # Delivery into the supervisor's pane or inbox: kept here instead.
    monkeypatch.setattr(agents, "_send_message", lambda db, agent, body, sender_id, **kw: t.sent.append(body))
    return t


def waiting(monkeypatch, *steps):
    """The hook's waits on the daemon: each takes the next step (given the
    holder), then whatever answer the daemon has for it. Out of steps with no
    answer, the hook would wait on: the ask fails instead (the hook gives it
    to the CLI's prompt)."""
    left = list(steps)

    def wait(self, seconds):
        stepped = bool(left)
        if left:
            left.pop(0)(self)
        held, answer = holds.HOLDS.take(self.request_id, self.nonce)
        assert stepped or answer is not None, "nobody answered"
        return held, answer
    monkeypatch.setattr(holds.Holder, "wait", wait)


def ask(db, command, agent_id="w1"):
    out = agents.permission_request_decision(db, db.get_agent(agent_id), {
        "tool_name": "Bash", "tool_input": {"command": command}, "hook_event_name": "PermissionRequest"})
    return out["hookSpecificOutput"]["decision"]["behavior"] if out else None


def person_answers(choice):
    def step(holder):
        assert holds.HOLDS.claim(holder.agent_id, holder.request_id, holder.digest, choice)
        assert holds.HOLDS.release(holder.agent_id, holder.request_id)
    return step


def history(db):
    return [tuple(r) for r in db.conn.execute("SELECT resolved_by, outcome FROM permission_asks ORDER BY id")]


def test_the_supervisor_answers_before_the_person_sees_it(db, team, monkeypatch):
    # Long and on two lines: the supervisor is shown all of what an allow allows.
    command = "make build " + " ".join(f"TARGET{i}=x" for i in range(60)) + "\nmake install"

    def boss_allows(holder):
        assert inbox.pending_permissions(db, db.get_agent("w1")) == []      # not the person's
        team.caller = "boss"
        try:
            permission_routing.answer_via_daemon("w1", holder.request_id, "allow")
        finally:
            team.caller = "w1"
    waiting(monkeypatch, boss_allows)
    assert ask(db, command) == "allow"
    [message] = team.sent
    assert command in message
    assert history(db) == [("supervisor", "allow")]


@pytest.mark.parametrize("escalated", [True, False])     # False: the boss doesn't answer in time
def test_the_person_gets_what_the_supervisor_escalates_or_leaves(db, team, monkeypatch, escalated):
    set_local(team.ws.repo_root, "permission_supervisor_hold_seconds", 30)

    def handed_over(holder):
        if escalated:
            assert team.says("boss", holder.request_id, "escalate", "touches prod")[0] == 200
        else:
            team.clock.now += 31

    def person_sees_it(holder):
        [shown] = permission_grants.pending_asks(db)
        assert shown.request_id == holder.request_id and shown.escalation
        person_answers("deny")(holder)
    waiting(monkeypatch, handed_over, person_sees_it)
    assert ask(db, "make build") == "deny"
    assert history(db) == [("person", "deny")]


def test_a_person_only_ask_skips_the_supervisor(db, team, monkeypatch):
    def boss_cannot(holder):
        assert team.says("boss", holder.request_id, "allow")[0] == 409
        [shown] = inbox.pending_permissions(db, db.get_agent("w1"))
        assert shown.dto.request_id == holder.request_id
        person_answers("allow_once")(holder)
    waiting(monkeypatch, boss_cannot)
    assert ask(db, "deploy prod") == "allow"
    assert team.sent == []
    assert history(db) == [("person", "allow")]


@pytest.mark.parametrize("who", ["w1", "other"])   # the worker itself; another worker's supervisor
def test_only_the_workers_own_supervisor_answers(db, team, monkeypatch, who):
    def refused_then_boss_denies(holder):
        assert team.says(who, holder.request_id, "allow")[0] == 403
        assert holds.HOLDS.held("w1", holder.request_id).answer is None
        assert team.says("boss", holder.request_id, "deny")[0] == 200
    waiting(monkeypatch, refused_then_boss_denies)
    assert ask(db, "make build") == "deny"
    assert history(db) == [("supervisor", "deny")]


@pytest.mark.parametrize("answered_first", [False, True])
def test_a_duplicate_ask_joins_the_first_until_the_supervisor_answers_it(db, team, monkeypatch, answered_first):
    """The same request asked again while the first waits on the supervisor
    joins it: one message, one answer for both, neither row replaced. Asked
    once the supervisor has answered the first (before its hook took the
    answer), it is a request of its own."""
    first, second = [], []

    def asked_again(holder):
        first.append(holder.request_id)
        if answered_first:
            assert team.says("boss", holder.request_id, "allow")[0] == 200
        second.append(ask(db, "make build"))

    def boss_answers(holder):
        rows = db.permission_requests_of("w1")
        assert [r["tool_use_id"] for r in rows] == [first[0], holder.request_id]
        assert team.says("boss", holder.request_id if answered_first else first[0], "allow")[0] == 200
    waiting(monkeypatch, asked_again, boss_answers)
    assert ask(db, "make build") == "allow"
    assert second == ["allow"]
    assert len(team.sent) == (2 if answered_first else 1)
    assert history(db) == [("supervisor", "allow"), ("supervisor", "allow")]
