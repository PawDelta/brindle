"""A worker's permission ask goes to its supervisor before the person (brindle.permission_routing)."""

import time

import pytest

from brindle import agents, permission_grants, permissions
from brindle.config import set_local
from brindle.daemon import holds
from brindle.db import Agent, Workspace
from brindle.permission_routing import RoutingError, answer
from brindle.permissions import Rule
from brindle.service import inbox


class Clock:
    """The time module, with time() and monotonic() at ``now``."""
    now = 1000.0

    def time(self):
        return self.now
    monotonic = time

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def team(db, tmp_path, monkeypatch):
    """Worker w1 under supervisor boss, and other's worker w2; the daemon's holds
    in this process, and messages to an agent kept instead of typed into a pane."""
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
    clock = Clock()
    monkeypatch.setattr(agents, "time", clock)
    monkeypatch.setattr(holds.HOLDS, "clock", clock.time)
    monkeypatch.setattr(holds.HOLDS, "_holds", {})
    monkeypatch.setattr(holds.HOLDS, "_outcomes", {})
    monkeypatch.setattr(holds.Holder, "register", lambda self, until, supervise_until=None: holds.HOLDS.register(
        self.agent_id, self.request_id, self.digest, self.nonce, until, supervise_until))
    monkeypatch.setattr(holds.Holder, "end", lambda self: holds.HOLDS.end(self.request_id, self.nonce))
    monkeypatch.setattr(agents, "_pane_state", lambda agent: None)
    sent = []
    monkeypatch.setattr(agents, "_send_message", lambda db, agent, body, sender_id, **kw: sent.append(agent.id))
    return ws, sent, clock


def waiting(monkeypatch, *steps):
    """The hook's waits on the daemon: each takes the next step (given the
    holder), then whatever answer the daemon has for it."""
    left = list(steps)

    def wait(self, seconds):
        if left:
            left.pop(0)(self)
        return holds.HOLDS.take(self.request_id, self.nonce)
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
    ws, sent, _ = team

    def boss_allows(holder):
        assert inbox.pending_permissions(db, db.get_agent("w1")) == []      # not the person's
        answer(db, {"boss"}, "w1", holder.request_id, "allow")
    waiting(monkeypatch, boss_allows)
    assert ask(db, "make build") == "allow"
    assert sent == ["boss"]
    assert history(db) == [("supervisor", "allow")]


@pytest.mark.parametrize("escalated", [True, False])     # False: the boss doesn't answer in time
def test_the_person_gets_what_the_supervisor_escalates_or_leaves(db, team, monkeypatch, escalated):
    ws, sent, clock = team
    set_local(ws.repo_root, "permission_supervisor_hold_seconds", 30)

    def handed_over(holder):
        if escalated:
            answer(db, {"boss"}, "w1", holder.request_id, "escalate", "touches prod")
        else:
            clock.now += 31

    def person_sees_it(holder):
        [shown] = permission_grants.pending_asks(db)
        assert shown.request_id == holder.request_id and shown.escalation
        person_answers("deny")(holder)
    waiting(monkeypatch, handed_over, person_sees_it)
    assert ask(db, "make build") == "deny"
    assert history(db) == [("person", "deny")]


def test_a_person_only_ask_skips_the_supervisor(db, team, monkeypatch):
    ws, sent, _ = team

    def boss_cannot(holder):
        with pytest.raises(RoutingError):
            answer(db, {"boss"}, "w1", holder.request_id, "allow")
        [shown] = inbox.pending_permissions(db, db.get_agent("w1"))
        assert shown.dto.request_id == holder.request_id
        person_answers("allow_once")(holder)
    waiting(monkeypatch, boss_cannot)
    assert ask(db, "deploy prod") == "allow"
    assert sent == []
    assert history(db) == [("person", "allow")]


@pytest.mark.parametrize("callers", [{"w1"}, {"other"}])   # the worker itself; another worker's supervisor
def test_only_the_workers_own_supervisor_answers(db, team, monkeypatch, callers):
    def refused_then_boss_denies(holder):
        with pytest.raises(RoutingError) as e:
            answer(db, callers, "w1", holder.request_id, "allow")
        assert e.value.status == 403
        assert holds.HOLDS.held("w1", holder.request_id).answer is None
        answer(db, {"boss"}, "w1", holder.request_id, "deny")
    waiting(monkeypatch, refused_then_boss_denies)
    assert ask(db, "make build") == "deny"
    assert history(db) == [("supervisor", "deny")]


def test_a_duplicate_ask_waits_with_the_first_and_one_answer_settles_both(db, team, monkeypatch):
    ws, sent, _ = team
    first, second = [], []

    def asked_again(holder):
        first.append(holder.request_id)
        second.append(ask(db, "make build"))         # the same request, while the first waits

    def boss_answers_the_first(holder):
        rows = db.permission_requests_of("w1")
        assert [r["tool_use_id"] for r in rows] == [first[0], holder.request_id]
        assert {r["channel"] for r in rows} == {"supervisor"}
        answer(db, {"boss"}, "w1", first[0], "allow")
    waiting(monkeypatch, asked_again, boss_answers_the_first)
    assert ask(db, "make build") == "allow"
    assert second == ["allow"]
    assert sent == ["boss"]                          # asked once
    assert history(db) == [("supervisor", "allow"), ("supervisor", "allow")]
