"""A worker's permission ask goes to its supervisor before the person (brindle.permission_routing)."""

import asyncio
import json
import time

import pytest

from brindle import agents, cull, permission_grants, permission_routing, permissions
from brindle.config import load_repo_config, set_local
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
        self.cfg_supervisor_seconds = load_repo_config(ws.repo_root).permission_supervisor_hold_seconds

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
    monkeypatch.setattr("brindle.daemon.events.connected", 1)      # the app is open to show what reaches the person
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


def ask(db, command, agent_id="w1", inside=None):
    """``inside``: the id of the Agent-tool subagent making the request, as Claude Code's payload names it."""
    out = agents.permission_request_decision(db, db.get_agent(agent_id), {
        "tool_name": "Bash", "tool_input": {"command": command}, "hook_event_name": "PermissionRequest",
        **({"agent_id": inside, "agent_type": "general-purpose"} if inside else {})})
    return out["hookSpecificOutput"]["decision"]["behavior"] if out else None


def person_answers(choice):
    def step(holder):
        assert holds.HOLDS.claim(holder.agent_id, holder.request_id, holder.digest, choice)
        assert holds.HOLDS.release(holder.agent_id, holder.request_id)
    return step


def history(db):
    return [tuple(r) for r in db.conn.execute("SELECT resolved_by, outcome FROM permission_asks ORDER BY id")]


def test_the_supervisor_answers_before_the_person_sees_it(db, team, monkeypatch):
    permissions.apply_preset("autonomous")
    permissions.add_rule("bash", "make", "ask", "prefix")
    for mode, answer in (("default", "allow"), ("dontAsk", None)):
        out = agents.handle_hook(db, "w1", "pre-tool", {
            "tool_name": "Bash", "tool_input": {"command": "terraform plan"}, "permission_mode": mode})
        assert (out and out["hookSpecificOutput"]["permissionDecision"]) == answer
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
    assert history(db) == [("policy", "allow"), ("supervisor", "allow")]


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


SUBAGENT = "a47d9298166766162"
MAKE_BUILD = {"tool_name": "Bash", "tool_input": {"command": "make build"}}


@pytest.mark.variant("Claude Code 2.1.318 double-prompt regression: a busy pane hides subagent requests until the hook returns")
@pytest.mark.parametrize("who, inside, resolved_by", [
    ("w1", SUBAGENT, "supervisor"),     # a worker's subagent, answered by its supervisor
    ("boss", SUBAGENT, "person"),       # a person's own chat, answered from the Inbox
    ("w1", None, "supervisor"),         # the main thread, before any look has caught its prompt
    ("boss", None, "person"),          # a supervisor's own request goes straight to the person
])
def test_allowed_request_does_not_prompt_again(db, team, monkeypatch, who, inside, resolved_by):
    """A pane that has shown no prompt says nothing about the request. Claude
    Code waits for the hook before it draws a subagent's (seen in 2.1.318: the
    pane stays busy, the dialog comes only once the hook returns nothing). The
    hold outlasts any number of looks, and the answer given in the end is what
    the CLI gets: not a prompt in the pane."""
    monkeypatch.setattr(agents, "_pane_state", lambda agent: "busy")
    team.caller = who
    prior = []
    if who == "boss" and inside is None:
        permissions.apply_preset("autonomous")
        permissions.add_rule("bash", "make", "ask", "prefix")
        waiting(monkeypatch)
        until = 'until s=$(gh run view 1 --json status --jq .status); [[ $s == completed* ]]; do echo w; done; echo "$s"'
        assert ask(db, until, agent_id=who) == "allow"
        prior = [("policy", "allow")]
        assert history(db) == prior

    def answered(holder):
        if who == "boss":
            assert team.clock.now == 1000.0
            [shown] = inbox.pending_permissions(db, db.get_agent(who))
            assert shown.dto.request_id == holder.request_id
            if inside:
                assert shown.dto.terminal_prompt_visible is False  # a subagent has no prompt on screen
            person_answers("allow_once")(holder)
        else:
            assert team.says("boss", holder.request_id, "allow")[0] == 200
    waiting(monkeypatch, *[lambda holder: None] * (agents.ANSWERED_LOOKS * 5), answered)
    assert ask(db, "make build", agent_id=who, inside=inside) == "allow"
    assert history(db) == [*prior, (resolved_by, "allow")]
    if who == "boss":
        assert team.sent == []


def test_a_held_subagent_request_outlives_its_workers_turn_and_other_threads(db, team, monkeypatch):
    """The row going is how a hold learns its CLI's prompt took the request. A
    subagent's has no prompt to take it, so neither the worker's turn ending
    around it nor the same command run on another thread may remove it. Its own
    tool, once allowed, still pairs with it."""
    for bookkeeping in ("snapshot_turn", "analysis_docs_turn", "_stop_decision"):     # git and tmux: not what's tested
        monkeypatch.setattr(agents, bookkeeping, lambda *a, **k: None)

    def boss_allows(holder):
        assert team.says("boss", holder.request_id, "allow")[0] == 200
    waiting(monkeypatch,
            lambda holder: agents.handle_hook(db, "w1", "stop", {"stop_hook_active": True}),
            lambda holder: agents.handle_hook(db, "w1", "tool-done", MAKE_BUILD),
            lambda holder: agents.handle_hook(db, "w1", "tool-done", {**MAKE_BUILD, "agent_id": SUBAGENT}),
            boss_allows)
    assert ask(db, "make build", inside=SUBAGENT) == "allow"
    agents.handle_hook(db, "w1", "tool-done", {**MAKE_BUILD, "agent_id": SUBAGENT})
    assert db.permission_requests_of("w1") == []


@pytest.mark.variant("nobody waits on nothing: every way a subagent's request still reaches the pane's dialog")
def test_a_held_subagent_request_goes_to_the_pane_when_the_app_cannot_answer_it(db, team, monkeypatch):
    team.caller = "boss"
    daemons_wait = holds.Holder.wait
    waiting(monkeypatch, person_answers("terminal"))        # the person's explicit choice
    assert ask(db, "make build", agent_id="boss", inside=SUBAGENT) is None

    def app_closes(self, seconds):                          # the daemon's own route lets go of the hold
        monkeypatch.setattr("brindle.daemon.events.connected", 0)
        return daemons_wait(self, seconds)
    monkeypatch.setattr(holds.Holder, "wait", app_closes)
    assert ask(db, "make build", agent_id="boss", inside=SUBAGENT) is None

    waiting(monkeypatch)                                    # no app from the start: never held at all
    assert ask(db, "make build", agent_id="boss", inside=SUBAGENT) is None


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

    # While a supervisor can answer, a second notice saying only the user can
    # would wrongly escalate it. Once its window expires the notice is needed.
    monkeypatch.setattr(agents, "pane_owners", lambda db, panes: {})
    monkeypatch.setattr(agents, "owns_pane", lambda db, a, owners: True)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "send_message", lambda db, to, body, sender_id=None: team.sent.append(body))
    monkeypatch.setattr(cull.tmux, "capture", lambda *a, **k: "Bash command\nmake build\nDo you want to proceed?")
    db.add_permission_request("w1", "r1", json.dumps({"tool": "Bash", "kind": "bash", "provider": "claude",
                                                      "command": "make build"}), "why", None, "supervisor",
                              hold_until=time.time() + 3600)
    db.set_status("w1", "waiting")
    assert cull.note_stuck(db, time.time(), {}) == [] and team.sent == []
    db.conn.execute("UPDATE permission_requests SET created_at=created_at-?",
                    (team.cfg_supervisor_seconds + 1,))
    assert cull.note_stuck(db, time.time(), {}) != [] and len(team.sent) == 1


@pytest.mark.parametrize("who", ["w1", "other"])   # the worker itself; another worker's supervisor
def test_only_the_workers_own_supervisor_answers(db, team, monkeypatch, who):
    def refused_then_boss_denies(holder):
        assert team.says(who, holder.request_id, "allow")[0] == 403
        assert holds.HOLDS.held("w1", holder.request_id).answer is None
        assert team.says("boss", holder.request_id, "deny")[0] == 200
    waiting(monkeypatch, refused_then_boss_denies)
    assert ask(db, "make build") == "deny"
    assert history(db) == [("supervisor", "deny")]


def send_mail(to, body):
    return {"tool_name": "mcp__mail__send", "tool_input": {"to": to, "body": body},
            "hook_event_name": "PermissionRequest"}


MAKE = {"tool_name": "Bash", "tool_input": {"command": "make build"}, "hook_event_name": "PermissionRequest"}


@pytest.mark.variant("race: a second ask arriving while the first waits on the supervisor joins it, never replaces it")
@pytest.mark.parametrize("first_ask, again, answered_first", [
    (MAKE, MAKE, False),                    # joins the first: one message, one answer for both
    (MAKE, MAKE, True),                     # the first answered already: a request of its own
    # The same tool with other arguments: another request, shown in full.
    (send_mail("ops@example.com", "status: green"), send_mail("all@example.com", "wire the funds"), False),
])
def test_a_duplicate_ask_joins_the_first_until_the_supervisor_answers_it(db, team, monkeypatch, first_ask, again,
                                                                         answered_first):
    """The same request asked again while the first waits on the supervisor
    joins it, neither row replaced; anything else is asked about on its own."""
    with permissions.editing() as store:
        store.rules = [*store.rules, Rule("mcp", "mcp__mail__send", "exact", "ask")]
    joins = again is first_ask and not answered_first
    first, second = [], []

    def asked_again(holder):
        first.append(holder.request_id)
        if answered_first:
            assert team.says("boss", holder.request_id, "allow")[0] == 200
        second.append(agents.permission_request_decision(db, db.get_agent("w1"), again))

    def boss_answers(holder):
        rows = db.permission_requests_of("w1")
        assert [r["tool_use_id"] for r in rows] == [first[0], holder.request_id]
        if not answered_first:
            assert team.says("boss", first[0], "allow")[0] == 200
        assert (holds.HOLDS.held("w1", holder.request_id).answer is not None) == joins
        if not joins:
            assert team.says("boss", holder.request_id, "allow")[0] == 200
    waiting(monkeypatch, asked_again, boss_answers)
    out = agents.permission_request_decision(db, db.get_agent("w1"), first_ask)
    assert [o["hookSpecificOutput"]["decision"]["behavior"] for o in (out, *second)] == ["allow", "allow"]
    assert len(team.sent) == (1 if joins else 2)
    for message, asked in zip(team.sent, (first_ask, again)):
        assert all(value in message for value in asked["tool_input"].values())
    assert history(db) == [("supervisor", "allow"), ("supervisor", "allow")]
